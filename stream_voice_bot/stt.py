from __future__ import annotations

import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import ctranslate2
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from faster_whisper.utils import _MODELS
from huggingface_hub import snapshot_download
from scipy.signal import resample_poly
from tqdm.auto import tqdm


_HALLUCINATION_CREDIT_RE = re.compile(
    r"(?:\b(?:subtitles?|captions?)\s+(?:made|created|provided)\s+by\b|"
    r"\b(?:субтитры|субтитров)\s+(?:сделан|создан|предоставлен)(?:ы|о)?\s+(?:кем|автором)?\b)",
    re.IGNORECASE,
)


def _should_skip_segment(segment, text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    if not normalized or _HALLUCINATION_CREDIT_RE.search(normalized):
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
    # STT v2 deliberately starts from a light model. Heavy Whisper models are
    # still selectable manually, but the default is designed for live captions.
    model_name: str = "small"
    language: str = "ru"
    input_device: int | None = None
    sample_rate: int = 16000
    min_speech_seconds: float = 0.30
    silence_seconds: float = 0.65
    max_utterance_seconds: float = 7.0
    vad_threshold: float = 0.008
    beam_size: int = 1
    compute_type: str = "float16"
    device_mode: str = "auto"


class STTService:
    """Low-overhead live STT pipeline.

    v2 separates audio capture/VAD from Whisper inference. Whisper is invoked
    only after a real speech phrase is detected, rather than on a fixed timer
    while the microphone is silent.
    """

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

        saved_model = db.get_setting("stt_model", "small")
        self.config = STTConfig(
            model_name=saved_model or "small",
            language=db.get_setting("stt_language", "ru"),
            input_device=int(db.get_setting("stt_input_device")) if db.get_setting("stt_input_device") else None,
            sample_rate=int(db.get_setting("stt_sample_rate", "16000")),
            min_speech_seconds=float(db.get_setting("stt_min_speech_seconds", "0.30")),
            silence_seconds=float(db.get_setting("stt_silence_seconds", "0.65")),
            max_utterance_seconds=float(db.get_setting("stt_max_utterance_seconds", "7.0")),
            vad_threshold=float(db.get_setting("stt_vad_threshold", "0.008")),
            beam_size=int(db.get_setting("stt_beam_size", "1")),
            compute_type=db.get_setting("stt_compute_type", "float16"),
            device_mode=db.get_setting("stt_device", "auto"),
        )

        self.model = None
        self.runtime_device: str | None = None
        self.runtime_compute_type: str | None = None
        self.loaded_model_key: tuple[str, str] | None = None

        self.thread: threading.Thread | None = None
        self.transcribe_thread: threading.Thread | None = None
        self.start_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        self.audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=120)
        self.utterance_q: queue.Queue[tuple[np.ndarray, float, float]] = queue.Queue(maxsize=2)
        self.input_stream: sd.InputStream | None = None
        self.lock = threading.RLock()

        self.last_text = ""
        self.input_gain = 1.0
        self.model_loading = False
        self.model_loading_started_at: float | None = None
        self.message = "STT остановлен"
        self.last_error = ""
        self.input_stream_sample_rate: int | None = None
        self.loading_phase = "idle"
        self.loading_progress: float | None = None
        self.loading_rate_mbps: float | None = None
        self.audio_rms = 0.0
        self.audio_peak = 0.0
        self.audio_blocks_received = 0
        self.audio_last_callback_at: float | None = None
        self.transcribe_attempts = 0
        self.last_transcribe_duration = 0.0
        self.last_transcribe_at: float | None = None
        self.last_transcribe_result = "ещё не запускалось"
        self.gpu_runtime_dir: Path | None = None
        self.gpu_runtime_ready = False

        self.noise_rms = 0.002
        self.vad_state = "silence"
        self.vad_speech_seconds = 0.0
        self.vad_silence_seconds = 0.0
        self.vad_last_utterance_seconds = 0.0
        self.dropped_utterances = 0

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
        running = bool(
            self.thread and self.thread.is_alive()
        ) or bool(
            self.transcribe_thread and self.transcribe_thread.is_alive()
        )
        return {
            "running": running,
            "model_loading": bool(self.model_loading),
            "model_loaded": self.model is not None,
            "device": self.runtime_device if running else None,
            "configured_device": self.config.device_mode,
            "runtime_compute_type": self.runtime_compute_type,
            "device_mode": self.config.device_mode,
            "loading_seconds": round(
                max(0.0, time.monotonic() - self.model_loading_started_at), 1
            ) if self.model_loading and self.model_loading_started_at else 0.0,
            "loading_phase": self.loading_phase,
            "loading_progress": self.loading_progress,
            "loading_rate_mbps": self.loading_rate_mbps,
            "audio_rms": round(float(self.audio_rms), 5),
            "audio_peak": round(float(self.audio_peak), 5),
            "input_gain": round(float(self.input_gain), 2),
            "audio_blocks_received": int(self.audio_blocks_received),
            "audio_last_callback_at": self.audio_last_callback_at,
            "transcribe_attempts": int(self.transcribe_attempts),
            "last_transcribe_duration": round(float(self.last_transcribe_duration), 2),
            "last_transcribe_at": self.last_transcribe_at,
            "last_transcribe_result": self.last_transcribe_result,
            "gpu_runtime_ready": bool(self.gpu_runtime_ready),
            "model": self.config.model_name,
            "language": self.config.language,
            "input_device": self.config.input_device,
            "sample_rate": self.config.sample_rate,
            "min_speech_seconds": self.config.min_speech_seconds,
            "silence_seconds": self.config.silence_seconds,
            "max_utterance_seconds": self.config.max_utterance_seconds,
            "vad_threshold": self.config.vad_threshold,
            "vad_state": self.vad_state,
            "vad_speech_seconds": round(float(self.vad_speech_seconds), 2),
            "vad_silence_seconds": round(float(self.vad_silence_seconds), 2),
            "vad_last_utterance_seconds": round(float(self.vad_last_utterance_seconds), 2),
            "dropped_utterances": int(self.dropped_utterances),
            "audio_queue": self.audio_q.qsize(),
            "utterance_queue": self.utterance_q.qsize(),
            "beam_size": self.config.beam_size,
            "compute_type": self.config.compute_type,
            "last_text": self.last_text,
            "message": self.message,
            "last_error": self.last_error,
        }

    def save_config(self, **kwargs):
        allowed = {
            "model_name": "stt_model",
            "language": "stt_language",
            "input_device": "stt_input_device",
            "sample_rate": "stt_sample_rate",
            "min_speech_seconds": "stt_min_speech_seconds",
            "silence_seconds": "stt_silence_seconds",
            "max_utterance_seconds": "stt_max_utterance_seconds",
            "vad_threshold": "stt_vad_threshold",
            "beam_size": "stt_beam_size",
            "compute_type": "stt_compute_type",
            "device_mode": "stt_device",
        }
        restart_required = False
        restart_reasons = []
        for key, value in kwargs.items():
            if key not in allowed or value is None:
                continue
            if key in {"model_name", "device_mode", "input_device", "sample_rate"} and value != getattr(self.config, key):
                restart_required = True
                restart_reasons.append(
                    "модель" if key == "model_name"
                    else "устройство" if key == "device_mode"
                    else "аудиовход"
                )
            setattr(self.config, key, value)
            self.db.set_setting(allowed[key], str(value))

        if self.config.min_speech_seconds <= 0:
            raise ValueError("min_speech_seconds must be > 0")
        if self.config.silence_seconds <= 0:
            raise ValueError("silence_seconds must be > 0")
        if self.config.max_utterance_seconds <= self.config.min_speech_seconds:
            raise ValueError("max_utterance_seconds must be greater than min_speech_seconds")
        if self.config.vad_threshold <= 0:
            raise ValueError("vad_threshold must be > 0")

        if restart_required:
            reason_text = " и ".join(restart_reasons)
            self._emit(
                running=bool(self.thread and self.thread.is_alive()),
                message=f"Изменение ({reason_text}) STT применится при следующем запуске STT.",
            )

    def _gpu_runtime_path(self) -> Path:
        data_root = self.db.path.parent if hasattr(self.db, "path") else Path.cwd() / "data"
        runtime_dir = Path(data_root) / "gpu_runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.gpu_runtime_dir = runtime_dir
        return runtime_dir

    def _nvidia_gpu_present(self) -> bool:
        candidates = []
        command = shutil.which("nvidia-smi")
        if command:
            candidates.append(command)
        if sys.platform == "win32":
            system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
            if system32.is_file():
                candidates.append(str(system32))
        for command in candidates:
            try:
                proc = subprocess.run(
                    [command, "-L"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode == 0 and any(line.strip() for line in proc.stdout.splitlines()):
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
        return False

    @staticmethod
    def _gpu_runtime_expected() -> tuple[str, int]:
        return (
            "https://github.com/Purfview/whisper-standalone-win/releases/download/libs/cuBLAS.and.cuDNN_CUDA12_win_v3.7z",
            849_141_159,
        )

    def _gpu_runtime_is_ready(self) -> bool:
        runtime_dir = self._gpu_runtime_path()
        required = (
            runtime_dir / "cublasLt64_12.dll",
            runtime_dir / "cublas64_12.dll",
            runtime_dir / "cudart64_12.dll",
            runtime_dir / "cudnn64_9.dll",
        )
        ready = all(path.is_file() and path.stat().st_size > 64_000 for path in required)
        self.gpu_runtime_ready = ready
        if ready:
            self._prepare_windows_cuda_dll_search()
        return ready

    def _install_gpu_runtime(self) -> bool:
        runtime_dir = self._gpu_runtime_path()
        if self._gpu_runtime_is_ready():
            return True

        url, expected_size = self._gpu_runtime_expected()
        archive_path = runtime_dir.parent / ".gpu_runtime_cuda12.7z"
        extract_dir = runtime_dir.parent / ".gpu_runtime_extract"

        self._set_loading_phase("gpu-runtime", "Скачиваю GPU runtime для STT… 0%", progress=0.0)
        started = time.monotonic()
        downloaded = 0

        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "StreamVoiceBot/1.1.14"},
            )
            with urllib.request.urlopen(request, timeout=30) as response, archive_path.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.monotonic() - started, 0.001)
                    progress = min(100.0, downloaded / expected_size * 100.0)
                    rate_mbps = downloaded / elapsed / 1_000_000.0
                    self._set_loading_phase(
                        "gpu-runtime",
                        f"Скачиваю GPU runtime для STT… {progress:.0f}% "
                        f"({self._format_mb(downloaded)} / {self._format_mb(expected_size)})",
                        progress=progress,
                        rate_mbps=rate_mbps,
                    )

            actual_size = archive_path.stat().st_size
            if actual_size != expected_size:
                raise RuntimeError(
                    f"GPU runtime download size mismatch: {actual_size} != {expected_size} bytes"
                )

            if shutil.which("tar") is None:
                raise RuntimeError("Windows tar.exe не найден. Он нужен для распаковки GPU runtime.")

            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            self._set_loading_phase("gpu-runtime-extract", "Распаковываю GPU runtime для STT…")

            tar_kwargs = {"capture_output": True, "text": True, "timeout": 300}
            if sys.platform == "win32":
                tar_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.run(
                ["tar", "-xf", str(archive_path), "-C", str(extract_dir)],
                **tar_kwargs,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"Не удалось распаковать GPU runtime: {detail}")

            for dll in extract_dir.rglob("*.dll"):
                shutil.copy2(dll, runtime_dir / dll.name)

            if not self._gpu_runtime_is_ready():
                raise RuntimeError("GPU runtime распакован, но обязательные NVIDIA DLL не найдены.")
            return True
        finally:
            try:
                archive_path.unlink(missing_ok=True)
            except TypeError:
                if archive_path.exists():
                    archive_path.unlink()
            shutil.rmtree(extract_dir, ignore_errors=True)

    def _ensure_gpu_runtime(self) -> bool:
        return self._gpu_runtime_is_ready() or self._install_gpu_runtime()

    def _check_cuda_runtime(self):
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
        try:
            local_runtime = self._gpu_runtime_path()
            if local_runtime.is_dir():
                candidates.append(local_runtime)
        except Exception:
            pass
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

    def _set_loading_phase(self, phase: str, message: str, progress: float | None = None, rate_mbps: float | None = None):
        self.loading_phase = phase
        self.loading_progress = progress
        self.loading_rate_mbps = rate_mbps
        self._emit(
            running=False,
            model_loading=True,
            loading_phase=phase,
            loading_progress=progress,
            loading_rate_mbps=rate_mbps,
            message=message,
        )

    def _download_model(self, model_name: str):
        if "/" in str(model_name):
            repo_id = str(model_name)
        else:
            repo_id = _MODELS.get(str(model_name))
        if not repo_id:
            raise RuntimeError(f"Неизвестная STT модель: {model_name}")

        self._set_loading_phase(
            "model-download",
            f"Подготовка модели STT: {model_name}…",
            progress=0.0,
        )
        service = self
        started_at = time.monotonic()
        last_emit = [started_at]
        last_n = [0]

        class ProgressTqdm(tqdm):
            def update(self, n=1):
                result = super().update(n)
                now = time.monotonic()
                if now - last_emit[0] >= 0.5 or (self.total and self.n >= self.total):
                    total = float(self.total or 0.0)
                    progress = (float(self.n) / total * 100.0) if total > 0 else None
                    elapsed = max(now - started_at, 1e-6)
                    delta_n = max(0, int(self.n) - last_n[0])
                    rate_mbps = (
                        delta_n / (now - last_emit[0]) / 1_000_000.0
                        if now - last_emit[0] > 0 else None
                    )
                    if progress is None:
                        rate_mbps = float(self.n) / elapsed / 1_000_000.0
                    service._set_loading_phase(
                        "model-download",
                        (
                            f"Скачивание модели STT: {model_name} — "
                            f"{progress:.1f}% ({service._format_mb(self.n)} / {service._format_mb(self.total)})"
                            if progress is not None
                            else f"Скачивание модели STT: {model_name} — {service._format_mb(self.n)}"
                        ),
                        progress=progress,
                        rate_mbps=rate_mbps,
                    )
                    last_emit[0] = now
                    last_n[0] = int(self.n)
                return result

        return snapshot_download(
            repo_id=repo_id,
            allow_patterns=[
                "config.json",
                "preprocessor_config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
            ],
            tqdm_class=ProgressTqdm,
        )

    @staticmethod
    def _format_mb(value) -> str:
        try:
            return f"{float(value) / 1_000_000.0:.0f} МБ"
        except (TypeError, ValueError):
            return "0 МБ"

    def _load_model(self):
        self.model_loading_started_at = self.model_loading_started_at or time.monotonic()
        requested_mode = str(self.config.device_mode or "auto").strip().lower()
        if requested_mode not in {"auto", "cuda", "cpu"}:
            requested_mode = "auto"
            self.config.device_mode = requested_mode

        desired_model = str(self.config.model_name)
        if self.model is not None and self.loaded_model_key:
            loaded_model, loaded_backend = self.loaded_model_key
            if loaded_model == desired_model and (
                requested_mode == "auto" or requested_mode == loaded_backend
            ):
                self.runtime_device = loaded_backend
                self.runtime_compute_type = "float16" if loaded_backend == "cuda" else "int8"
                return

        self._release_model()
        mode = requested_mode

        if mode in {"auto", "cuda"} and not self._gpu_runtime_is_ready():
            self._set_loading_phase(
                "cuda-runtime",
                "GPU runtime отсутствует — он будет подготовлен только при доступной NVIDIA GPU…",
            )

        self._emit(
            running=False,
            model_loading=True,
            message=(
                f"Загрузка STT: {self.config.model_name}… "
                "Лёгкий режим STT v2: распознавание запускается только на фразах."
            ),
            last_error="",
        )

        def load_cpu():
            self._emit(
                running=False,
                model_loading=True,
                message=f"Загрузка STT: {self.config.model_name} на CPU int8…",
            )
            model_path = self._download_model(self.config.model_name)
            self._set_loading_phase("model-init", f"Загрузка модели STT в CPU: {self.config.model_name}…")
            self.model = WhisperModel(model_path, device="cpu", compute_type="int8")
            self.runtime_device = "cpu"
            self.runtime_compute_type = "int8"
            self.loaded_model_key = (str(self.config.model_name), "cpu")

        def load_cuda():
            self._emit(
                running=False,
                model_loading=True,
                message="Проверяю NVIDIA GPU/драйвер перед загрузкой STT…",
            )
            if not self._nvidia_gpu_present():
                raise RuntimeError("NVIDIA GPU/драйвер не обнаружен через nvidia-smi; GPU runtime не скачивается.")
            if not self._gpu_runtime_is_ready():
                self._set_loading_phase("cuda-runtime", "NVIDIA GPU найдена; подготавливаю GPU runtime для STT…")
                self._ensure_gpu_runtime()
            self._check_cuda_runtime()
            model_path = self._download_model(self.config.model_name)
            self._set_loading_phase("model-init", f"Загрузка модели STT в GPU: {self.config.model_name}…")
            self.model = WhisperModel(model_path, device="cuda", compute_type="float16")
            self.runtime_device = "cuda"
            self.runtime_compute_type = "float16"
            self.loaded_model_key = (str(self.config.model_name), "cuda")

        try:
            if mode == "cpu":
                load_cpu()
            elif mode == "cuda":
                try:
                    load_cuda()
                except Exception as e:
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
                    self._release_model()
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
            self._release_model()
            self.model_loading_started_at = None
            self.loading_phase = "error"
            self.loading_progress = None
            self.loading_rate_mbps = None
            self._emit(
                running=False,
                model_loading=False,
                message=f"Ошибка загрузки STT: {type(e).__name__}: {e}",
                last_error=f"{type(e).__name__}: {e}",
            )
            raise

        self.model_loading_started_at = None
        self.loading_phase = "idle"
        self.loading_progress = None
        self.loading_rate_mbps = None
        self._emit(
            running=False,
            model_loading=False,
            model_loaded=True,
            message=(
                "STT v2 готов ✓ "
                f"(устройство: {self.runtime_device}, режим: {self.runtime_compute_type}, "
                f"модель: {self.config.model_name})"
            ),
            last_error="",
        )

    def _release_model(self):
        self.model = None
        self.runtime_device = None
        self.runtime_compute_type = None
        self.loaded_model_key = None
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self._emit(running=True, message="STT уже работает")
                return
            if self.start_thread and self.start_thread.is_alive():
                self._emit(model_loading=True, message="STT уже запускается…")
                return

            self.stop_event.clear()
            self._drain_queue(self.audio_q)
            self._drain_queue(self.utterance_q)

            self.last_text = ""
            self.input_gain = 1.0
            self.audio_rms = 0.0
            self.audio_peak = 0.0
            self.audio_blocks_received = 0
            self.audio_last_callback_at = None
            self.transcribe_attempts = 0
            self.last_transcribe_duration = 0.0
            self.last_transcribe_at = None
            self.last_transcribe_result = "ещё не запускалось"
            self.vad_state = "silence"
            self.vad_speech_seconds = 0.0
            self.vad_silence_seconds = 0.0
            self.vad_last_utterance_seconds = 0.0
            self.dropped_utterances = 0
            self.noise_rms = 0.002

            self.model_loading_started_at = time.monotonic()
            self.loading_phase = "starting"
            self.loading_progress = None
            self.loading_rate_mbps = None
            self._emit(
                running=False,
                model_loading=True,
                loading_phase="starting",
                message=f"Запуск STT v2: {self.config.model_name}…",
                last_error="",
            )
            self.start_thread = threading.Thread(
                target=self._start_worker,
                name="stt-v2-start",
                daemon=True,
            )
            self.start_thread.start()

    def _start_worker(self):
        try:
            self._load_model()
            if self.stop_event.is_set():
                self._release_model()
                self.model_loading = False
                self._emit(running=False, model_loading=False, message="STT остановлен")
                return

            stream, actual_rate = self._open_input_stream()
            self.input_stream = stream
            self.input_stream_sample_rate = actual_rate

            worker = threading.Thread(target=self._vad_worker, name="stt-v2-vad", daemon=True)
            transcriber = threading.Thread(
                target=self._transcription_worker,
                name="stt-v2-transcribe",
                daemon=True,
            )
            self.thread = worker
            self.transcribe_thread = transcriber
            worker.start()
            transcriber.start()
            self._emit(
                running=True,
                model_loading=False,
                message=(
                    "STT v2 запущен ✓ · лёгкий VAD включён · "
                    "Whisper работает только на распознанных фразах."
                ),
                last_error="",
            )
        except Exception:
            self._close_input_stream()
            self._release_model()
            if self.last_error:
                return

    def stop(self):
        self.stop_event.set()
        self._close_input_stream()

        # Stop should free the Whisper model instead of retaining hundreds of MB
        # in RAM after the captions were switched off.
        self._release_model()
        self.runtime_device = None
        self.runtime_compute_type = None
        self._drain_queue(self.audio_q)
        self._drain_queue(self.utterance_q)
        self.model_loading = False
        self.thread = None
        self.transcribe_thread = None
        self._emit(running=False, model_loading=False, message="STT остановлен")

    @staticmethod
    def _drain_queue(q):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def _callback(self, indata, frames, time_info, status):
        if status:
            self._emit(running=True, message=f"Audio input: {status}")

        matrix = np.asarray(indata, dtype=np.float32)
        if matrix.ndim > 1:
            if matrix.shape[1] > 1:
                matrix = matrix[:, int(np.argmax(np.mean(np.square(matrix), axis=0)))]
            else:
                matrix = matrix[:, 0]

        data = np.ascontiguousarray(matrix, dtype=np.float32)
        try:
            self.audio_q.put_nowait(data)
        except queue.Full:
            try:
                self.audio_q.get_nowait()
                self.audio_q.put_nowait(data)
            except queue.Empty:
                pass

        try:
            self.audio_blocks_received += 1
            self.audio_last_callback_at = time.time()
            self.audio_rms = float(np.sqrt(np.mean(np.square(data), dtype=np.float64))) if data.size else 0.0
            self.audio_peak = float(np.max(np.abs(data))) if data.size else 0.0
        except (TypeError, ValueError):
            pass

    def _candidate_input_rates(self, device_override=None) -> tuple[int, list[int], int, str, object]:
        requested = max(1, int(self.config.sample_rate))
        device = self.config.input_device if device_override is None else device_override
        try:
            info = sd.query_devices(device, kind="input")
        except TypeError:
            info = sd.query_devices(device)
        except Exception as e:
            raise RuntimeError(f"Не удалось получить сведения о микрофоне: {type(e).__name__}: {e}") from e

        max_input_channels = int(info.get("max_input_channels", 0) or 0)
        if max_input_channels <= 0:
            name = str(info.get("name") or f"device {device}")
            raise RuntimeError(f"У выбранного аудиоустройства нет входных каналов: {name}")

        try:
            default_rate = int(round(float(info["default_samplerate"])))
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError("Не удалось определить стандартную частоту микрофона") from e

        candidates = []
        for rate in (requested, default_rate, 48000, 44100, 32000, 16000):
            rate = int(rate)
            if rate > 0 and rate not in candidates:
                candidates.append(rate)
        hostapi_index = int(info.get("hostapi", -1) or -1)
        try:
            hostapi = sd.query_hostapis(hostapi_index) if hostapi_index >= 0 else {}
            hostapi_name = str(hostapi.get("name") or "")
        except Exception:
            hostapi_name = ""
        return requested, candidates, max_input_channels, hostapi_name, device

    def _open_input_stream(self):
        requested_device = self.config.input_device
        device_candidates = [requested_device]
        if requested_device is not None:
            device_candidates.append(None)

        errors = []
        for device_override in device_candidates:
            requested, rates, max_input_channels, hostapi_name, actual_device = self._candidate_input_rates(device_override)
            is_wasapi = "WASAPI" in hostapi_name.upper()
            channel_candidates = [1, 2] if max_input_channels >= 2 else [1]
            settings_candidates = [None]
            if is_wasapi and hasattr(sd, "WasapiSettings"):
                try:
                    settings_candidates = [sd.WasapiSettings(auto_convert=True), None]
                except Exception:
                    settings_candidates = [None]

            attempts = []
            for extra_settings in settings_candidates:
                for channels in channel_candidates:
                    attempts.append((None, channels, extra_settings))
                for channels in channel_candidates:
                    for rate in rates:
                        attempts.append((rate, channels, extra_settings))

            for rate, channels, extra_settings in attempts:
                stream = None
                try:
                    kwargs = {
                        "device": actual_device,
                        "channels": channels,
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
                    actual_rate = int(round(float(getattr(stream, "samplerate", 0.0) or (rate or 0))))
                    if actual_rate <= 0:
                        raise RuntimeError("PortAudio did not report an input sample rate")
                    self._emit(
                        running=True,
                        message=f"Микрофон открыт: {actual_rate} Hz · {channels}ch",
                    )
                    return stream, actual_rate
                except Exception as e:
                    label_rate = f"{rate} Hz" if rate is not None else "device default"
                    mode = "WASAPI auto-convert" if extra_settings is not None else "default"
                    device_label = "default input" if actual_device is None else f"device {actual_device}"
                    errors.append(
                        f"{device_label}: {label_rate}/{channels}ch [{mode}]: "
                        f"{type(e).__name__}: {e}"
                    )
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

        raise RuntimeError(
            f"Не удалось открыть микрофон. Запрошено {requested_device!r}. "
            + " | ".join(errors)
        )

    def _close_input_stream(self):
        stream = self.input_stream
        self.input_stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.input_stream_sample_rate = None

    def _vad_worker(self):
        pre_roll: deque[np.ndarray] = deque(maxlen=5)
        speech_blocks: list[np.ndarray] = []
        speech_samples = 0
        speech_hit_blocks = 0
        silence_samples = 0
        current_started_at = 0.0

        while not self.stop_event.is_set():
            try:
                block = self.audio_q.get(timeout=0.15)
            except queue.Empty:
                continue
            if block.size == 0:
                continue

            duration = float(block.size) / max(1, int(self.input_stream_sample_rate or self.config.sample_rate))
            rms = float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))
            peak = float(np.max(np.abs(block))) if block.size else 0.0
            floor = max(1e-6, self.noise_rms)
            threshold = max(self.config.vad_threshold, floor * 2.6)
            speech_now = rms >= threshold or peak >= threshold * 3.0

            if self.vad_state == "silence":
                if not speech_now:
                    self.noise_rms = min(
                        0.05,
                        (self.noise_rms * 0.97) + (rms * 0.03),
                    )
                    pre_roll.append(block)
                    self.vad_speech_seconds = 0.0
                    self.vad_silence_seconds += duration
                    continue

                speech_hit_blocks += 1
                pre_roll.append(block)
                if speech_hit_blocks < 2:
                    continue

                self.vad_state = "speech"
                current_started_at = time.monotonic()
                silence_samples = 0
                speech_blocks = list(pre_roll)
                speech_samples = sum(x.size for x in speech_blocks)
                self.vad_speech_seconds = speech_samples / max(1, int(self.input_stream_sample_rate or self.config.sample_rate))
                self.vad_silence_seconds = 0.0
                speech_hit_blocks = 0
                continue

            # speech state
            speech_blocks.append(block)
            speech_samples += block.size
            self.vad_speech_seconds = speech_samples / max(1, int(self.input_stream_sample_rate or self.config.sample_rate))

            if speech_now:
                silence_samples = 0
                self.vad_silence_seconds = 0.0
            else:
                silence_samples += block.size
                self.vad_silence_seconds = silence_samples / max(1, int(self.input_stream_sample_rate or self.config.sample_rate))

            hard_limit = self.vad_speech_seconds >= self.config.max_utterance_seconds
            end_by_silence = (
                self.vad_silence_seconds >= self.config.silence_seconds
                and self.vad_speech_seconds >= self.config.min_speech_seconds
            )
            if not hard_limit and not end_by_silence:
                continue

            utterance = np.concatenate(speech_blocks).astype(np.float32, copy=False)
            input_rate = int(self.input_stream_sample_rate or self.config.sample_rate)
            self.vad_last_utterance_seconds = float(utterance.size) / max(1, input_rate)
            timestamp = time.time()
            started = current_started_at or time.monotonic()
            age = max(0.0, time.monotonic() - started)

            if input_rate != 16000:
                utterance = resample_poly(utterance, 16000, input_rate).astype(np.float32)

            peak_value = float(np.max(np.abs(utterance))) if utterance.size else 0.0
            if peak_value > 0.0005 and peak_value < 0.08:
                gain = min(8.0, max(1.0, 0.18 / peak_value))
                self.input_gain = gain
                utterance = np.clip(utterance * gain, -1.0, 1.0).astype(np.float32)

            self._enqueue_utterance(utterance, timestamp, age)

            pre_roll.clear()
            speech_blocks = []
            speech_samples = 0
            silence_samples = 0
            self.vad_state = "silence"
            self.vad_speech_seconds = 0.0
            self.vad_silence_seconds = 0.0
            speech_hit_blocks = 0

    def _enqueue_utterance(self, audio: np.ndarray, timestamp: float, age: float):
        payload = (audio, timestamp, age)
        try:
            self.utterance_q.put_nowait(payload)
            return
        except queue.Full:
            try:
                self.utterance_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.utterance_q.put_nowait(payload)
            except queue.Full:
                self.dropped_utterances += 1
                return
            self.dropped_utterances += 1

    def _transcription_worker(self):
        while not self.stop_event.is_set():
            try:
                audio, timestamp, age = self.utterance_q.get(timeout=0.15)
            except queue.Empty:
                continue

            if self.model is None:
                continue

            try:
                started = time.monotonic()
                self.transcribe_attempts += 1
                segments, info = self.model.transcribe(
                    audio,
                    language=self.config.language or None,
                    beam_size=1,
                    vad_filter=False,
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
                self.last_transcribe_duration = time.monotonic() - started
                self.last_transcribe_at = time.time()
                self.last_transcribe_result = "текст получен" if text else "текста нет"

                if not text:
                    continue

                detected_language = getattr(info, "language", self.config.language) or self.config.language
                self.last_text = text
                self._emit(running=True, last_error="", message=f"STT: «{text[:120]}»")
                self.on_subtitle({
                    "source": True,
                    "text": text,
                    "start": start,
                    "end": end,
                    "language": detected_language,
                    "timestamp": timestamp,
                })

                tracks = self.get_subtitle_tracks()
                if self.translator:
                    for track in tracks:
                        if not track.get("enabled", True) or str(track.get("mode", "source")) != "translate":
                            continue
                        target_language = str(track.get("language", "")).strip().lower().split("-")[0]
                        if not target_language or target_language == detected_language.lower().split("-")[0]:
                            continue
                        threading.Thread(
                            target=self._translate_one,
                            kwargs={
                                "track_id": track.get("id", target_language),
                                "target": target_language,
                                "source_text": text,
                                "source_language": detected_language,
                                "segment_start": start,
                                "segment_end": end,
                                "timestamp": timestamp,
                            },
                            name=f"stt-translate-{target_language}",
                            daemon=True,
                        ).start()
            except Exception as e:
                self.last_transcribe_duration = max(0.0, time.monotonic() - started)
                self.last_transcribe_at = time.time()
                self.last_transcribe_result = f"ошибка: {type(e).__name__}"
                self._emit(
                    running=True,
                    message=f"STT error: {type(e).__name__}: {e}",
                    last_error=f"{type(e).__name__}: {e}",
                )

    def _translate_one(self, track_id, target, source_text, source_language, segment_start, segment_end, timestamp):
        try:
            translated = self.translator.translate(source_text, source_language, target)
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

