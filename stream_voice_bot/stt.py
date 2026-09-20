from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from scipy.signal import resample_poly


@dataclass
class STTConfig:
    model_name: str = "large-v3-turbo"
    language: str = "ru"
    input_device: int | None = None
    sample_rate: int = 16000
    chunk_seconds: float = 4.0
    overlap_seconds: float = 0.5
    beam_size: int = 1
    compute_type: str = "float16"


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
            chunk_seconds=float(db.get_setting("stt_chunk_seconds", "4.0")),
            overlap_seconds=float(db.get_setting("stt_overlap_seconds", "0.5")),
            beam_size=int(db.get_setting("stt_beam_size", "1")),
            compute_type=db.get_setting("stt_compute_type", "float16"),
        )
        self.model = None
        self.thread: threading.Thread | None = None
        self.start_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        # ~20 seconds of 1600-frame callback blocks at the current default.
        # Oldest audio is dropped instead of allowing an unbounded memory backlog.
        self.audio_q: deque[np.ndarray] = deque(maxlen=200)
        self.last_text = ""
        self.model_loading = False
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
        }
        model_changed = False
        for key, value in kwargs.items():
            if key in allowed and value is not None:
                if key == "model_name" and value != self.config.model_name:
                    model_changed = True
                setattr(self.config, key, value)
                self.db.set_setting(allowed[key], str(value))
        if model_changed:
            self.model = None
            self._emit(
                running=bool(self.thread and self.thread.is_alive()),
                message="Модель STT изменена; новая модель загрузится при следующем запуске STT.",
            )

    def _load_model(self):
        if self.model is not None:
            return
        self._emit(
            running=False,
            model_loading=True,
            message=f"Загрузка STT: {self.config.model_name}…",
            last_error="",
        )
        try:
            self.model = WhisperModel(
                self.config.model_name,
                device="cuda",
                compute_type=self.config.compute_type,
            )
        except Exception as gpu_error:
            self._emit(
                running=False,
                model_loading=True,
                message=f"GPU STT не запустился: {type(gpu_error).__name__}: {gpu_error}. Пробую CPU int8…",
            )
            try:
                self.model = WhisperModel(
                    self.config.model_name,
                    device="cpu",
                    compute_type="int8",
                )
            except Exception as cpu_error:
                self.model = None
                self._emit(
                    running=False,
                    model_loading=False,
                    message=f"Ошибка загрузки STT: {type(cpu_error).__name__}: {cpu_error}",
                    last_error=f"{type(cpu_error).__name__}: {cpu_error}",
                )
                raise
        self._emit(
            running=False,
            model_loading=False,
            model_loaded=True,
            message="STT модель готова ✓",
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
            self.start_thread = threading.Thread(target=self._start_worker, name="stt-start", daemon=True)
            self.start_thread.start()
        self._emit(running=False, model_loading=True, message=f"Запуск STT: загрузка {self.config.model_name}…", last_error="")

    def _start_worker(self):
        try:
            self._load_model()
            if self.stop_event.is_set():
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

    def _candidate_input_rates(self) -> tuple[int, list[int]]:
        """Build a deterministic list of rates to try for a Windows input device."""
        requested = max(1, int(self.config.sample_rate))
        device = self.config.input_device
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
        return requested, candidates, max_input_channels, hostapi_name

    def _open_input_stream(self):
        """
        Open the real PortAudio stream, trying practical Windows/WASAPI
        format combinations. On WASAPI shared mode, auto_convert lets the
        system audio mixer convert sample rates/channel layouts when the
        requested format doesn't exactly match the device mix format.
        """
        requested, rates, max_input_channels, hostapi_name = self._candidate_input_rates()
        errors = []

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

        # Prefer WASAPI shared-mode auto-conversion, then fall back to plain
        # PortAudio formats. Try stereo as well because many Windows capture
        # endpoints expose a 2-channel system mix format even when STT needs
        # only one channel.
        attempts = []
        for extra_settings in extra_settings_variants:
            for channel_count in channels:
                for rate in rates:
                    attempts.append((rate, channel_count, extra_settings))

        for rate, channel_count, extra_settings in attempts:
            stream = None
            try:
                kwargs = {
                    "device": self.config.input_device,
                    "channels": channel_count,
                    "samplerate": rate,
                    "dtype": "float32",
                    "callback": self._callback,
                    "blocksize": 0,
                }
                if extra_settings is not None:
                    kwargs["extra_settings"] = extra_settings
                stream = sd.InputStream(**kwargs)
                stream.start()
                self.input_stream_sample_rate = rate
                return stream, rate
            except Exception as e:
                mode = "WASAPI auto-convert" if extra_settings is not None else "default"
                errors.append(
                    f"{rate} Hz/{channel_count}ch [{mode}]: "
                    f"{type(e).__name__}: {e}"
                )
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

        raise RuntimeError(
            "Не удалось открыть микрофон. "
            f"Запрошено {requested} Hz; устройство сообщает {max_input_channels} входных каналов "
            f"через {hostapi_name or 'PortAudio'}. "
            + " | ".join(errors)
        )

    def _run(self):
        target_rate = max(1, int(self.config.sample_rate))
        stream = None
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
                    buf = np.concatenate([buf, self.audio_q.popleft()])
                else:
                    time.sleep(0.03)
                    continue

                if len(buf) < chunk_samples:
                    continue

                source_audio = buf[:chunk_samples]
                buf = buf[chunk_samples - overlap_samples:]

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
                        condition_on_previous_text=False,
                        temperature=0,
                    )
                    texts = []
                    start = None
                    end = None
                    for seg in segments:
                        t = (seg.text or "").strip()
                        if not t:
                            continue
                        texts.append(t)
                        start = seg.start if start is None else min(start, seg.start)
                        end = seg.end if end is None else max(end, seg.end)

                    text = " ".join(texts).strip()
                    if text:
                        detected_language = getattr(info, "language", self.config.language) or self.config.language
                        ts = time.time()
                        self.last_text = text
                        self.on_subtitle({
                            "track_id": "ru" if detected_language.startswith("ru") else detected_language.lower(),
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
                                try:
                                    translated = self.translator.translate(text, detected_language, target_language)
                                    if translated:
                                        self.on_subtitle({
                                            "track_id": track.get("id", target_language),
                                            "text": translated,
                                            "start": start,
                                            "end": end,
                                            "language": target_language,
                                            "timestamp": ts,
                                        })
                                except Exception as e:
                                    self._emit(
                                        running=True,
                                        message=f"Перевод {detected_language} → {target_language}: {type(e).__name__}: {e}",
                                    )
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
            if self.stop_event.is_set():
                self._emit(running=False, model_loading=False, message="STT остановлен")
