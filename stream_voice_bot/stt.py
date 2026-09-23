from __future__ import annotations

import ctypes
import os
import sys
import threading
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import ctranslate2
from scipy.signal import resample_poly


_HALLUCINATION_CREDIT_RE = re.compile(
    r"(?:\b(?:subtitles?|captions?)\s+(?:made|created|provided)\s+by\b|"
    r"\b(?:субтитры|субтитров)\s+(?:сделан|создан|предоставлен)(?:ы|о)?\s+(?:кем|автором)?\b)",
    re.IGNORECASE,
)


def _should_skip_segment(segment, text: str) -> bool:
    """Reject common Whisper hallucinations before they reach subtitles/translation."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return True
    if _HALLUCINATION_CREDIT_RE.search(normalized):
        return True

    avg_logprob = getattr(segment, "avg_logprob", None)
    no_speech_prob = getattr(segment, "no_speech_prob", None)
    compression_ratio = getattr(segment, "compression_ratio", None)
    try:
        if (
            no_speech_prob is not None
            and avg_logprob is not None
            and float(no_speech_prob) >= 0.90
            and float(avg_logprob) < -0.40
        ):
            return True
    except (TypeError, ValueError):
        pass
    try:
        if compression_ratio is not None and float(compression_ratio) > 3.2:
            return True
    except (TypeError, ValueError):
        pass
    return False


@dataclass
class STTConfig:
    model_name: str = "large-v3-turbo"
    language: str = "ru"
    input_device: int | None = None
    sample_rate: int = 16000
    chunk_seconds: float = 2.5
    overlap_seconds: float = 0.25
    beam_size: int = 1
    compute_type: str = "float16"
    device_mode: str = "auto"


class STTService:
    def __init__(
        self,
        db,
        on_subtitle: Callable[[dict], None],
        on_status: Callable[[dict], None],
        get_subtitle_tracks: Callable[[], list[dict]] | None = None,
        translator=None,
    ):
        self.db = db
        self.on_subtitle = on_subtitle
        self.on_status = on_status
        self.get_subtitle_tracks = get_subtitle_tracks or (lambda: [])
        self.translator = translator
        self.config = STTConfig(
            model_name=db.get_setting("stt_model", "large-v3-turbo"),
            language=db.get_setting("stt_language", "ru"),
            input_device=int(db.get_setting("stt_input_device")) if db.get_setting("stt_input_device") else None,
            sample_rate=int(db.get_setting("stt_sample_rate", "16000")),
            chunk_seconds=float(db.get_setting("stt_chunk_seconds", "2.5")),
            overlap_seconds=float(db.get_setting("stt_overlap_seconds", "0.25")),
            beam_size=int(db.get_setting("stt_beam_size", "1")),
            compute_type=db.get_setting("stt_compute_type", "float16"),
            device_mode=db.get_setting("stt_device", "auto"),
        )
        self.model = None
        self.runtime_device: str | None = None
        self.runtime_compute_type: str | None = None
        self.loaded_model_key: tuple[str, str] | None = None
        self.thread: threading.Thread | None = None
        self.start_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        # ~20 seconds of 1600-frame callback blocks at the current default.
        # Oldest audio is dropped instead of allowing an unbounded memory backlog.
        self.audio_q: deque[np.ndarray] = deque(maxlen=200)
        self.last_text = ""
        self.model_loading = False
        self.model_loading_started_at: float | None = None
        self.message = "STT остановлен"
        self.last_error = ""
        self.input_stream_sample_rate: int | None = None

    def _emit(self, **data):
        with self.lock:
            if "message" in data and data["message"] is not None:
                self.message = str(data["message"])
            if data.get("model_loading") is not None:
                self.model_loading = bool(data["model_loading"])
            if "last_error" in data:
                self.last_error = str(data.get("last_error") or "")
        self.on_status(data)

    def state(self):
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "model_loading": bool(self.model_loading),
            "model_loaded": self.model is not None,
            "device": self.runtime_device,
            "runtime_compute_type": self.runtime_compute_type,
            "device_mode": self.config.device_mode,
            "loading_seconds": round(max(0.0, time.monotonic() - self.model_loading_started_at), 1) if self.model_loading and self.model_loading_started_at else 0.0,
            "model": self.config.model_name,
            "language": self.config.language,
            "input_device": self.config.input_device,
            "sample_rate": self.config.sample_rate,
            "input_stream_sample_rate": self.input_stream_sample_rate,
            "chunk_seconds": self.config.chunk_seconds,
            "overlap_seconds": self.config.overlap_seconds,
            "beam_size": self.config.beam_size,
            "compute_type": self.config.compute_type,
            "last_text": self.last_text,
            "message": self.message,
            "last_error": self.last_error,
        }

    def save_config(self, **kwargs):
        next_chunk = kwargs.get("chunk_seconds", self.config.chunk_seconds)
        next_overlap = kwargs.get("overlap_seconds", self.config.overlap_seconds)
        if float(next_overlap) >= float(next_chunk):
            raise ValueError("overlap_seconds must be smaller than chunk_seconds")
        allowed = {
            "model_name": "stt_model",
            "language": "stt_language",
            "input_device": "stt_input_device",
            "sample_rate": "stt_sample_rate",
            "chunk_seconds": "stt_chunk_seconds",
            "overlap_seconds": "stt_overlap_seconds",
            "beam_size": "stt_beam_size",
            "compute_type": "stt_compute_type",
            "device_mode": "stt_device",
        }
        restart_required = False
        restart_reasons = []
        for key, value in kwargs.items():
            if key in allowed and value is not None:
                if key in {"model_name", "device_mode"} and value != getattr(self.config, key):
                    restart_required = True
                    restart_reasons.append("модель" if key == "model_name" else "устройство")
                setattr(self.config, key, value)
                self.db.set_setting(allowed[key], str(value))
        if restart_required:
            reason_text = " и ".join(restart_reasons)
            self._emit(
                running=bool(self.thread and self.thread.is_alive()),
                message=f"Изменение ({reason_text}) STT применится при следующем запуске STT.",
            )

    def _check_cuda_runtime(self):
        """Fail fast when the NVIDIA runtime is not usable."""
        self._prepare_windows_cuda_dll_search()
        count = int(ctranslate2.get_cuda_device_count())
        if count < 1:
            raise RuntimeError("NVIDIA CUDA не обнаружена через CTranslate2.")
        supported = ctranslate2.get_supported_compute_types("cuda", 0)
        if "float16" not in supported:
            raise RuntimeError(
                "NVIDIA CUDA обнаружена, но FP16 не поддерживается CTranslate2 на GPU 0."
            )

    def _prepare_windows_cuda_dll_search(self):
        if sys.platform != "win32":
            return
        candidates = []
        for env_name in ("CUDA_PATH", "CUDA_PATH_V12_8", "CUDA_PATH_V12_6", "CUDA_PATH_V12_4"):
            value = os.environ.get(env_name)
            if value:
                candidates.append(Path(value) / "bin")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        cuda_root = Path(program_files) / "NVIDIA GPU Computing Toolkit" / "CUDA"
        if cuda_root.is_dir():
            candidates.extend(sorted(cuda_root.glob("v12.*\\bin"), reverse=True))
        for dll_dir in candidates:
            if not dll_dir.is_dir():
                continue
            dll_dir_text = str(dll_dir)
            path_value = os.environ.get("PATH", "")
            if dll_dir_text.casefold() not in {p.casefold() for p in path_value.split(os.pathsep) if p}:
                os.environ["PATH"] = dll_dir_text + os.pathsep + path_value
            try:
                os.add_dll_directory(dll_dir_text)
            except (AttributeError, OSError):
                pass

    def _load_model(self):
        self.model_loading_started_at = self.model_loading_started_at or time.monotonic()
        requested_mode = str(self.config.device_mode or "auto").strip().lower()
        if requested_mode not in {"auto", "cuda", "cpu"}:
            requested_mode = "auto"
            self.config.device_mode = requested_mode
        desired_key = (str(self.config.model_name), requested_mode)
        if self.model is not None and self.loaded_model_key == desired_key:
            return
        if self.model is not None and self.loaded_model_key != desired_key:
            self.model = None
            self.runtime_device = None
            self.runtime_compute_type = None
            self.loaded_model_key = None

        mode = requested_mode
        if mode in {"auto", "cuda"}:
            self._prepare_windows_cuda_dll_search()

        self._emit(
            running=False,
            model_loading=True,
            message=f"Загрузка STT: {self.config.model_name}… Первый запуск может занять несколько минут.",
            last_error="",
        )

        def load_cpu():
            self._emit(
                running=False,
                model_loading=True,
                message=f"Загрузка STT: {self.config.model_name} на CPU int8…",
            )
            self.model = WhisperModel(
                self.config.model_name,
                device="cpu",
                compute_type="int8",
            )
            self.runtime_device = "cpu"
            self.runtime_compute_type = "int8"
            self.loaded_model_key = (str(self.config.model_name), mode)

        def load_cuda():
            self._emit(
                running=False,
                model_loading=True,
                message="Проверяю NVIDIA CUDA перед загрузкой STT модели…",
            )
            self._check_cuda_runtime()
            self._emit(
                running=False,
                model_loading=True,
                message=f"Загрузка STT: {self.config.model_name} на NVIDIA CUDA…",
            )
            self.model = WhisperModel(
                self.config.model_name,
                device="cuda",
                compute_type="float16",
            )
            self.runtime_device = "cuda"
            self.runtime_compute_type = "float16"
            self.loaded_model_key = (str(self.config.model_name), mode)

        try:
            if mode == "cpu":
                load_cpu()
            elif mode == "cuda":
                try:
                    load_cuda()
                except Exception as e:
                    self.model = None
                    self.runtime_device = None
                    self.runtime_compute_type = None
                    detail = f"{type(e).__name__}: {e}"
                    if "cublas64_12.dll" in detail.lower():
                        detail += (
                            " | Не найден NVIDIA cuBLAS для CUDA 12. "
                            "Установите CUDA 12.x или используйте режим «Авто (CUDA → CPU)»/«CPU int8»."
                        )
                    raise RuntimeError(
                        f"CUDA выбрана, но STT не удалось запустить: {detail}"
                    ) from e
            else:
                try:
                    load_cuda()
                except Exception as gpu_error:
                    self.model = None
                    self.runtime_device = None
                    self.runtime_compute_type = None
                    self._emit(
                        running=False,
                        model_loading=True,
                        message=(
                            f"CUDA недоступна ({type(gpu_error).__name__}); "
                            "автоматически перехожу на CPU int8…"
                        ),
                    )
                    load_cpu()
        except Exception as e:
            self.model = None
            self.runtime_device = None
            self.runtime_compute_type = None
            self.loaded_model_key = None
            self.model_loading_started_at = None
            self._emit(
                running=False,
                model_loading=False,
                message=f"Ошибка загрузки STT: {type(e).__name__}: {e}",
                last_error=f"{type(e).__name__}: {e}",
            )
            raise

        self.model_loading_started_at = None
        self._emit(
            running=False,
            model_loading=False,
            model_loaded=True,
            message=(
                "STT модель готова ✓ "
                f"(устройство: {self.runtime_device}, режим: {self.runtime_compute_type})"
            ),
            last_error="",
        )

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self._emit(running=True, message="STT уже работает")
                return
            if self.start_thread and self.start_thread.is_alive():
                self._emit(model_loading=True, message="STT уже запускается…")
                return
            self.stop_event.clear()
            self.audio_q.clear()
            self.model_loading_started_at = time.monotonic()
            self.start_thread = threading.Thread(target=self._start_worker, name="stt-start", daemon=True)
            self.start_thread.start()
        self._emit(running=False, model_loading=True, message=f"Запуск STT: загрузка {self.config.model_name}…", last_error="")

    def _start_worker(self):
        try:
            self._load_model()
            if self.stop_event.is_set():
                self.model_loading_started_at = None
                self._emit(running=False, model_loading=False, message="STT остановлен")
                return
            worker = threading.Thread(target=self._run, name="stt-worker", daemon=True)
            with self.lock:
                self.thread = worker
            worker.start()
            self._emit(running=True, model_loading=False, message="STT запущен ✓", last_error="")
        except Exception:
            # _load_model already reported the concrete error.
            pass

    def stop(self):
        self.stop_event.set()
        self._emit(running=False, message="STT остановлен")

    def _callback(self, indata, frames, time_info, status):
        if status:
            self._emit(running=True, message=f"Audio input: {status}")
        if getattr(indata, "ndim", 1) > 1:
            self.audio_q.append(indata[:, 0].copy())
        else:
            self.audio_q.append(np.asarray(indata, dtype=np.float32).copy())

    def _candidate_input_rates(self, device_override=None) -> tuple[int, list[int], int, str, object]:
        """Build a deterministic list of rates to try for a Windows input device."""
        requested = max(1, int(self.config.sample_rate))
        device = self.config.input_device if device_override is None else device_override
        try:
            info = sd.query_devices(device, kind="input")
        except TypeError:
            info = sd.query_devices(device)
        except Exception as e:
            raise RuntimeError(f"Не удалось получить сведения о микрофоне: {type(e).__name__}: {e}") from e

        try:
            max_input_channels = int(info.get("max_input_channels", 0))
        except (TypeError, ValueError):
            max_input_channels = 0
        if max_input_channels <= 0:
            name = str(info.get("name") or f"device {device}")
            raise RuntimeError(f"У выбранного аудиоустройства нет входных каналов: {name}")

        try:
            default_rate = int(round(float(info["default_samplerate"])))
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError("Не удалось определить стандартную частоту микрофона") from e

        try:
            hostapi_index = int(info.get("hostapi", -1))
            hostapi = sd.query_hostapis(hostapi_index) if hostapi_index >= 0 else {}
            hostapi_name = str(hostapi.get("name") or "")
        except Exception:
            hostapi_name = ""

        candidates = []
        for rate in (requested, default_rate, 48000, 44100, 32000, 16000):
            rate = int(rate)
            if rate > 0 and rate not in candidates:
                candidates.append(rate)
        return requested, candidates, max_input_channels, hostapi_name, device

    def _open_input_stream(self):
        """
        Open the real PortAudio stream, trying practical Windows/WASAPI
        format combinations. On WASAPI shared mode, auto_convert lets the
        system audio mixer convert sample rates/channel layouts when the
        requested format doesn't exactly match the device mix format.
        """
        requested_device = self.config.input_device
        device_candidates = [requested_device]
        if requested_device is not None:
            device_candidates.append(None)

        errors = []

        for device_override in device_candidates:
            requested, rates, max_input_channels, hostapi_name, actual_device = self._candidate_input_rates(device_override)
            is_wasapi = "WASAPI" in hostapi_name.upper()
            channels = [1]
            if max_input_channels >= 2:
                channels.append(2)

            extra_settings_variants = [None]
            if is_wasapi and hasattr(sd, "WasapiSettings"):
                try:
                    extra_settings_variants = [
                        sd.WasapiSettings(auto_convert=True),
                        None,
                    ]
                except Exception:
                    extra_settings_variants = [None]

            # First try the device's native/default sample rate by leaving
            # samplerate unspecified. Fixed-rate attempts remain as fallbacks.
            attempts = []
            for extra_settings in extra_settings_variants:
                for channel_count in channels:
                    attempts.append((None, channel_count, extra_settings))
                for channel_count in channels:
                    for rate in rates:
                        attempts.append((rate, channel_count, extra_settings))

            for rate, channel_count, extra_settings in attempts:
                stream = None
                try:
                    kwargs = {
                        "device": actual_device,
                        "channels": channel_count,
                        "dtype": "float32",
                        "callback": self._callback,
                        "blocksize": 0,
                    }
                    if rate is not None:
                        kwargs["samplerate"] = rate
                    if extra_settings is not None:
                        kwargs["extra_settings"] = extra_settings
                    stream = sd.InputStream(**kwargs)
                    stream.start()
                    actual_rate = float(getattr(stream, "samplerate", 0.0) or 0.0)
                    if actual_rate <= 0:
                        actual_rate = float(rate or 0.0)
                    if actual_rate <= 0:
                        raise RuntimeError("PortAudio did not report an input sample rate")
                    actual_rate_int = int(round(actual_rate))
                    self.input_stream_sample_rate = actual_rate_int
                    if requested_device is not None and actual_device is None:
                        self._emit(
                            running=True,
                            message="Выбранный STT input не открылся; использую системный микрофон по умолчанию."
                        )
                    return stream, actual_rate_int
                except Exception as e:
                    label_rate = f"{rate} Hz" if rate is not None else "device default"
                    mode = "WASAPI auto-convert" if extra_settings is not None else "default"
                    device_label = "default input" if actual_device is None else f"device {actual_device}"
                    errors.append(
                        f"{device_label}: {label_rate}/{channel_count}ch [{mode}]: "
                        f"{type(e).__name__}: {e}"
                    )
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

        raise RuntimeError(
            "Не удалось открыть микрофон. "
            f"Запрошено {requested} Hz; выбранный input={requested_device!r}. "
            + " | ".join(errors)
        )

    def _run(self):
        target_rate = max(1, int(self.config.sample_rate))
        stream = None
        com_initialized = False

        # PortAudio's Windows WASAPI/WDM-KS path can be opened from a
        # dedicated Python thread only when that thread has initialized COM.
        # Without this, some Windows devices fail with:
        # "WdmSyncIoctl: DeviceIoControl GLE = 0x00000490".
        if sys.platform == "win32":
            try:
                hr = int(ctypes.windll.ole32.CoInitialize(None))
                # S_OK (0) and S_FALSE (1) both require a matching CoUninitialize.
                com_initialized = hr >= 0
            except Exception as e:
                self._emit(
                    running=True,
                    message=f"Windows audio COM initialization warning: {type(e).__name__}: {e}",
                )
        try:
            stream, input_rate = self._open_input_stream()
            chunk_samples = int(round(input_rate * self.config.chunk_seconds))
            overlap_samples = int(round(input_rate * self.config.overlap_seconds))
            if input_rate != target_rate:
                self._emit(
                    running=True,
                    message=f"Микрофон работает на {input_rate} Hz; для Whisper пересэмплирую в {target_rate} Hz.",
                )

            buf = np.zeros(0, dtype=np.float32)
            while not self.stop_event.is_set():
                if self.audio_q:
                    # Live captions must follow the newest audio, not replay
                    # a backlog that accumulated while Whisper was working.
                    queued = []
                    while self.audio_q:
                        queued.append(self.audio_q.popleft())
                    if queued:
                        buf = np.concatenate([buf, *queued])
                else:
                    time.sleep(0.03)
                    continue

                if len(buf) < chunk_samples:
                    continue

                # Transcribe the newest complete window and retain only a small
                # overlap for continuity. This bounds live-caption latency even
                # when CPU inference takes longer than realtime.
                source_audio = buf[-chunk_samples:]
                buf = buf[-overlap_samples:] if overlap_samples else np.zeros(0, dtype=np.float32)

                if input_rate != target_rate:
                    audio = resample_poly(
                        source_audio,
                        target_rate,
                        input_rate,
                    ).astype(np.float32)
                else:
                    audio = source_audio

                try:
                    segments, info = self.model.transcribe(
                        audio,
                        language=self.config.language or None,
                        beam_size=self.config.beam_size,
                        vad_filter=True,
                        vad_parameters={
                            "min_silence_duration_ms": 500,
                            "speech_pad_ms": 150,
                        },
                        condition_on_previous_text=False,
                        temperature=0,
                    )
                    texts = []
                    start = None
                    end = None
                    for seg in segments:
                        t = (seg.text or "").strip()
                        if not t or _should_skip_segment(seg, t):
                            continue
                        texts.append(t)
                        start = seg.start if start is None else min(start, seg.start)
                        end = seg.end if end is None else max(end, seg.end)

                    text = " ".join(texts).strip()
                    if text:
                        detected_language = getattr(info, "language", self.config.language) or self.config.language
                        ts = time.time()
                        self.last_text = text
                        # The detected language is metadata only. Do not route
                        # source STT text by language: Whisper can mis-detect the
                        # language, and a non-source track such as "en" may be disabled.
                        self.on_subtitle({
                            "source": True,
                            "text": text,
                            "start": start,
                            "end": end,
                            "language": detected_language,
                            "timestamp": ts,
                        })

                        tracks = self.get_subtitle_tracks()
                        if self.translator:
                            for track in tracks:
                                if not track.get("enabled", True) or str(track.get("mode", "source")) != "translate":
                                    continue
                                target_language = str(track.get("language", "")).strip().lower().split("-")[0]
                                if not target_language or target_language == detected_language.lower().split("-")[0]:
                                    continue
                                def translate_one(
                                    track_id=track.get("id", target_language),
                                    target=target_language,
                                    source_text=text,
                                    source_language=detected_language,
                                    segment_start=start,
                                    segment_end=end,
                                    timestamp=ts,
                                ):
                                    try:
                                        translated = self.translator.translate(
                                            source_text, source_language, target
                                        )
                                        if translated:
                                            self.on_subtitle({
                                                "source": False,
                                                "track_id": track_id,
                                                "text": translated,
                                                "start": segment_start,
                                                "end": segment_end,
                                                "language": target,
                                                "timestamp": timestamp,
                                            })
                                    except Exception as e:
                                        self._emit(
                                            running=True,
                                            message=f"Перевод {source_language} → {target}: {type(e).__name__}: {e}",
                                        )

                                threading.Thread(
                                    target=translate_one,
                                    name=f"stt-translate-{target_language}",
                                    daemon=True,
                                ).start()
                except Exception as e:
                    self._emit(
                        running=True,
                        message=f"STT error: {type(e).__name__}: {e}",
                    )
        except Exception as e:
            self._emit(
                running=False,
                model_loading=False,
                message=f"Ошибка аудиовхода STT: {type(e).__name__}: {e}",
                last_error=f"{type(e).__name__}: {e}",
            )
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            with self.lock:
                self.thread = None
            self.input_stream_sample_rate = None
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
            if self.stop_event.is_set():
                self._emit(running=False, model_loading=False, message="STT остановлен")
