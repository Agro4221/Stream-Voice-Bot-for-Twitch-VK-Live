from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

try:
    import sherpa_onnx
except ImportError as exc:
    sherpa_onnx = None
    _SHERPA_IMPORT_ERROR = exc
else:
    _SHERPA_IMPORT_ERROR = None


log = logging.getLogger(__name__)\n\n_HALLUCINATION_CREDIT_RE = re.compile(
    r"(?:\b(?:subtitles?|captions?)\s+(?:made|created|provided)\s+by\b|"
    r"\b(?:субтитры|субтитров)\s+(?:сделан|создан|предоставлен)(?:ы|о)?\s+(?:кем|автором)?\b)",
    re.IGNORECASE,
)

T_ONE_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-streaming-t-one-russian-2025-09-08.tar.bz2"
)
T_ONE_MODEL_DIRNAME = "t-one-russian-2025-09-08"
T_ONE_MODEL_RATE = 8000


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _should_skip_segment(segment, text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return True
    return bool(_HALLUCINATION_CREDIT_RE.search(normalized))


@dataclass
class STTConfig:
    # Kept for DB/API compatibility with older releases. T-one is the only
    # active STT engine in the new pipeline.
    model_name: str = "t-one-russian"
    language: str = "ru"
    input_device: int | None = None
    sample_rate: int = 16000
    chunk_seconds: float = 2.5
    overlap_seconds: float = 0.25
    beam_size: int = 1
    compute_type: str = "auto"
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
            model_name="t-one-russian",
            language=db.get_setting("stt_language", "ru"),
            input_device=int(db.get_setting("stt_input_device"))
            if db.get_setting("stt_input_device")
            else None,
            sample_rate=int(db.get_setting("stt_sample_rate", "16000")),
            chunk_seconds=float(db.get_setting("stt_chunk_seconds", "2.5")),
            overlap_seconds=float(db.get_setting("stt_overlap_seconds", "0.25")),
            beam_size=1,
            compute_type="auto",
            device_mode="auto",
        )
        self.model = None
        self.recognizer = None
        self.stream = None
        self.runtime_device: str | None = None
        self.runtime_compute_type: str | None = None
        self.loaded_model_key: tuple[str, str] | None = None
        self.thread: threading.Thread | None = None
        self.start_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()

        self.audio_q: deque[np.ndarray] = deque(maxlen=200)
        self.last_text = ""
        self.last_published_text = ""
        self.last_publish_at = 0.0
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
        self.model_dir = self._model_root()

    def _model_root(self) -> Path:
        base = self.db.path.parent if hasattr(self.db, "path") else Path.cwd() / "data"
        path = Path(base).parent / "models" / "stt"
        path.mkdir(parents=True, exist_ok=True)
        return path / T_ONE_MODEL_DIRNAME

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
            "model_loaded": self.recognizer is not None,
            "device": self.runtime_device if (self.thread and self.thread.is_alive()) else None,
            "configured_device": "auto",
            "device_mode": "auto",
            "provider": self.runtime_device,
            "engine": "T-one / sherpa-onnx",
            "model": "t-one-russian",
            "language": self.config.language,
            "model_sample_rate": T_ONE_MODEL_RATE,
            "input_device": self.config.input_device,
            "sample_rate": self.config.sample_rate,
            "input_stream_sample_rate": self.input_stream_sample_rate,
            "chunk_seconds": 0.1,
            "overlap_seconds": 0.0,
            "beam_size": 1,
            "compute_type": self.runtime_compute_type or "auto",
            "loading_seconds": round(
                max(0.0, time.monotonic() - self.model_loading_started_at), 1
            )
            if self.model_loading and self.model_loading_started_at
            else 0.0,
            "loading_phase": self.loading_phase,
            "loading_progress": self.loading_progress,
            "loading_rate_mbps": self.loading_rate_mbps,
            "audio_rms": round(float(self.audio_rms), 5),
            "audio_peak": round(float(self.audio_peak), 5),
            "input_gain": round(float(self.input_gain), 2),
            "audio_blocks_received": int(self.audio_blocks_received),
            "audio_last_callback_at": self.audio_last_callback_at,
            "transcribe_attempts": int(self.transcribe_attempts),
            "last_transcribe_duration": round(float(self.last_transcribe_duration), 3),
            "last_transcribe_at": self.last_transcribe_at,
            "last_transcribe_result": self.last_transcribe_result,
            "gpu_runtime_ready": bool(self.gpu_runtime_ready),
            "model_dir": str(self.model_dir),
            "last_text": self.last_text,
            "message": self.message,
            "last_error": self.last_error,
        }

    def save_config(self, **kwargs):
        if "language" in kwargs and kwargs["language"]:
            self.config.language = str(kwargs["language"]).strip() or "ru"
            self.db.set_setting("stt_language", self.config.language)
        if "input_device" in kwargs:
            value = kwargs["input_device"]
            self.config.input_device = None if value in (None, "") else int(value)
            self.db.set_setting("stt_input_device", str(self.config.input_device))
        if "sample_rate" in kwargs and kwargs["sample_rate"] is not None:
            self.config.sample_rate = int(kwargs["sample_rate"])
            self.db.set_setting("stt_sample_rate", str(self.config.sample_rate))
        # Keep legacy config fields persisted so old installs do not break.
        if "model_name" in kwargs and kwargs["model_name"]:
            self.db.set_setting("stt_model", "t-one-russian")
        if "device_mode" in kwargs:
            self.db.set_setting("stt_device", "auto")
        if "compute_type" in kwargs:
            self.db.set_setting("stt_compute_type", "auto")

    @staticmethod
    def _nvidia_gpu_present() -> bool:
        candidates: list[str] = []
        command = shutil.which("nvidia-smi")
        if command:
            candidates.append(command)
        if sys.platform == "win32":
            system32 = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "nvidia-smi.exe"
            )
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
                if proc.returncode == 0 and any(
                    line.strip() for line in proc.stdout.splitlines()
                ):
                    return True
            except (OSError, __import__("subprocess").SubprocessError):
                pass
        return False

    def _gpu_runtime_path(self) -> Path:
        data_root = self.db.path.parent if hasattr(self.db, "path") else Path.cwd() / "data"
        runtime_dir = Path(data_root) / "gpu_runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.gpu_runtime_dir = runtime_dir
        return runtime_dir

    @staticmethod
    def _gpu_runtime_expected() -> tuple[str, int]:
        return (
            "https://github.com/Purfview/whisper-standalone-win/releases/download/libs/"
            "cuBLAS.and.cuDNN_CUDA12_win_v3.7z",
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
        ready = all(
            path.is_file() and path.stat().st_size > 1_000_000 for path in required
        )
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
        self._set_loading_phase(
            "gpu-runtime",
            "Подготавливаю NVIDIA runtime для STT… 0%",
            progress=0.0,
        )
        started = time.monotonic()
        downloaded = 0
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "StreamVoiceBot/2.0"})
            with urllib.request.urlopen(request, timeout=30) as response, archive_path.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    progress = min(100.0, downloaded / expected_size * 100.0)
                    elapsed = max(time.monotonic() - started, 0.001)
                    self._set_loading_phase(
                        "gpu-runtime",
                        f"Подготавливаю NVIDIA runtime для STT… {progress:.0f}%",
                        progress=progress,
                        rate_mbps=downloaded / elapsed / 1_000_000.0,
                    )
            if archive_path.stat().st_size != expected_size:
                raise RuntimeError("NVIDIA runtime download size mismatch")
            tar_exe = shutil.which("tar")
            if not tar_exe:
                raise RuntimeError("Windows tar.exe не найден; не могу распаковать локальный CUDA runtime.")
            extract_dir = runtime_dir.parent / ".gpu_runtime_extract"
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [tar_exe, "-xf", str(archive_path), "-C", str(extract_dir)],
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"Не удалось распаковать NVIDIA runtime: {detail}")
            for dll in extract_dir.rglob("*.dll"):
                shutil.copy2(dll, runtime_dir / dll.name)
            shutil.rmtree(extract_dir, ignore_errors=True)
            if not self._gpu_runtime_is_ready():
                raise RuntimeError("NVIDIA runtime unpacked but required DLLs are missing")
            return True
        finally:
            archive_path.unlink(missing_ok=True)

    def _ensure_gpu_runtime(self) -> bool:
        if self._gpu_runtime_is_ready():
            return True
        return self._install_gpu_runtime()

    def _prepare_windows_cuda_dll_search(self):
        if sys.platform != "win32":
            return
        candidates: list[Path] = []
        try:
            local_runtime = self._gpu_runtime_path()
            if local_runtime.is_dir():
                candidates.append(local_runtime)
        except Exception:
            pass
        for env_name in (
            "CUDA_PATH",
            "CUDA_PATH_V12_8",
            "CUDA_PATH_V12_6",
            "CUDA_PATH_V12_4",
        ):
            value = os.environ.get(env_name)
            if value:
                candidates.append(Path(value) / "bin")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        cuda_root = Path(program_files) / "NVIDIA GPU Computing Toolkit" / "CUDA"
        if cuda_root.is_dir():
            candidates.extend(sorted(cuda_root.glob(r"v12.*\bin"), reverse=True))
        for dll_dir in candidates:
            if not dll_dir.is_dir():
                continue
            text = str(dll_dir)
            path_value = os.environ.get("PATH", "")
            if text.casefold() not in {p.casefold() for p in path_value.split(os.pathsep) if p}:
                os.environ["PATH"] = text + os.pathsep + path_value
            try:
                os.add_dll_directory(text)
            except (AttributeError, OSError):
                pass

    def _model_files(self) -> tuple[Path, Path]:
        return self.model_dir / "model.onnx", self.model_dir / "tokens.txt"

    def _download_model(self):
        model_file, tokens_file = self._model_files()
        if model_file.is_file() and model_file.stat().st_size > 50_000_000 and tokens_file.is_file():
            return
        self.model_dir.mkdir(parents=True, exist_ok=True)
        archive_path = Path(tempfile.gettempdir()) / "svb_t_one_russian.tar.bz2"
        self._set_loading_phase(
            "model-download",
            "Скачивание T-one для субтитров… 0%",
            progress=0.0,
        )
        started = time.monotonic()
        downloaded = 0
        try:
            request = urllib.request.Request(
                T_ONE_MODEL_URL,
                headers={"User-Agent": "StreamVoiceBot/2.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response, archive_path.open("wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.monotonic() - started, 0.001)
                    progress = downloaded / total * 100.0 if total else None
                    self._set_loading_phase(
                        "model-download",
                        f"Скачивание T-one для субтитров… {progress:.0f}%" if progress is not None else "Скачивание T-one для субтитров…",
                        progress=progress,
                        rate_mbps=downloaded / elapsed / 1_000_000.0,
                    )
            self._set_loading_phase("model-extract", "Распаковываю T-one для субтитров…")
            with tarfile.open(archive_path, "r:bz2") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    name = Path(member.name).name
                    if name not in {"model.onnx", "tokens.txt", "LICENSE", "README.md"}:
                        continue
                    target = self.model_dir / name
                    with archive.extractfile(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
            if not (model_file.is_file() and tokens_file.is_file()):
                raise RuntimeError("T-one model archive did not contain model.onnx/tokens.txt")
        finally:
            archive_path.unlink(missing_ok=True)

    def _build_recognizer(self, provider: str):
        if sherpa_onnx is None:
            raise RuntimeError(
                "sherpa-onnx не установлен. Запустите install_windows.ps1 ещё раз."
            ) from _SHERPA_IMPORT_ERROR
        model_file, tokens_file = self._model_files()
        self._prepare_windows_cuda_dll_search()
        return sherpa_onnx.OnlineRecognizer.from_t_one_ctc(
            tokens=str(tokens_file),
            model=str(model_file),
            num_threads=1,
            sample_rate=T_ONE_MODEL_RATE,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=0.9,
            rule2_min_trailing_silence=0.5,
            rule3_min_utterance_length=20.0,
            decoding_method="greedy_search",
            provider=provider,
            device=0,
        )

    def _load_model(self):
        self.model_loading_started_at = self.model_loading_started_at or time.monotonic()
        self._emit(
            running=False,
            model_loading=True,
            message="Подготовка T-one / sherpa-onnx STT…",
            last_error="",
        )
        self._download_model()

        provider = "cpu"
        if self._nvidia_gpu_present():
            gpu_error = None
            try:
                self._set_loading_phase("model-init", "Проверяю NVIDIA CUDA для STT…")
                self._prepare_windows_cuda_dll_search()
                recognizer = self._build_recognizer("cuda")
                provider = "cuda"
            except Exception as first_gpu_error:
                gpu_error = first_gpu_error
                try:
                    if not self._gpu_runtime_is_ready():
                        self._emit(
                            running=False,
                            model_loading=True,
                            message="Системная CUDA недоступна; готовлю локальный NVIDIA runtime для STT…",
                        )
                        self._ensure_gpu_runtime()
                    self._set_loading_phase("model-init", "Повторно запускаю T-one на NVIDIA CUDA…")
                    recognizer = self._build_recognizer("cuda")
                    provider = "cuda"
                except Exception as second_gpu_error:
                    gpu_error = second_gpu_error
                    detail = f"{type(first_gpu_error).__name__}: {first_gpu_error}; retry: {type(second_gpu_error).__name__}: {second_gpu_error}"
                    self._emit(
                        running=False,
                        model_loading=True,
                        message=f"CUDA STT недоступна ({detail}); перехожу на CPU T-one…",
                    )
                    recognizer = self._build_recognizer("cpu")
                    provider = "cpu"
        else:
            self._set_loading_phase("model-init", "NVIDIA GPU не найдена — запускаю CPU T-one…")
            recognizer = self._build_recognizer("cpu")

        self.recognizer = recognizer
        self.model = recognizer
        self.runtime_device = provider
        self.runtime_compute_type = "onnx"
        self.loaded_model_key = ("t-one-russian", provider)
        self.model_loading_started_at = None
        self.loading_phase = "idle"
        self.loading_progress = None
        self.loading_rate_mbps = None
        self._emit(
            running=False,
            model_loading=False,
            model_loaded=True,
            message=f"STT готов: T-one / sherpa-onnx · {provider.upper()} ✓",
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
            self.audio_rms = 0.0
            self.audio_peak = 0.0
            self.audio_blocks_received = 0
            self.audio_last_callback_at = None
            self.transcribe_attempts = 0
            self.last_transcribe_duration = 0.0
            self.last_transcribe_at = None
            self.last_transcribe_result = "ещё не запускалось"
            self.last_text = ""
            self.last_published_text = ""
            self.input_gain = 1.0
            self.model_loading_started_at = time.monotonic()
            self.loading_phase = "starting"
            self.loading_progress = None
            self.loading_rate_mbps = None
            self._emit(
                running=False,
                model_loading=True,
                loading_phase="starting",
                message="Запуск STT: T-one / sherpa-onnx…",
                last_error="",
            )
            self.start_thread = threading.Thread(
                target=self._start_worker,
                name="stt-start",
                daemon=True,
            )
            self.start_thread.start()

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
            self._emit(running=True, model_loading=False, message="STT запущен ✓")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            log.exception("STT startup failed")
            with self.lock:
                self.start_thread = None
                self.model_loading_started_at = None
                self.loading_phase = "error"
                self.loading_progress = None
                self.loading_rate_mbps = None
            self._emit(
                running=False,
                model_loading=False,
                message=f"Ошибка запуска STT: {detail}",
                last_error=detail,
            )

    def stop(self):
        self.stop_event.set()
        self.runtime_device = None
        self.runtime_compute_type = None
        self._emit(running=False, model_loading=False, message="STT остановлен")

    def _callback(self, indata, frames, time_info, status):
        if status:
            self._emit(running=True, message=f"Audio input: {status}")
        matrix = np.asarray(indata, dtype=np.float32)
        if matrix.ndim > 1:
            if matrix.shape[1] > 1:
                channel_energy = np.mean(np.square(matrix, dtype=np.float64), axis=0)
                data = matrix[:, int(np.argmax(channel_energy))].copy()
            else:
                data = matrix[:, 0].copy()
        else:
            data = matrix.copy()
        self.audio_q.append(data)
        self.audio_blocks_received += 1
        self.audio_last_callback_at = time.time()
        self.audio_rms = float(np.sqrt(np.mean(np.square(data), dtype=np.float64))) if data.size else 0.0
        self.audio_peak = float(np.max(np.abs(data))) if data.size else 0.0

    def _candidate_input_rates(self, device_override=None):
        requested = max(1, int(self.config.sample_rate))
        device = self.config.input_device if device_override is None else device_override
        try:
            info = sd.query_devices(device, kind="input")
        except TypeError:
            info = sd.query_devices(device)
        except Exception as e:
            raise RuntimeError(
                f"Не удалось получить сведения о микрофоне: {type(e).__name__}: {e}"
            ) from e
        max_input_channels = int(info.get("max_input_channels", 0) or 0)
        if max_input_channels <= 0:
            raise RuntimeError(f"У выбранного аудиоустройства нет входных каналов: {info.get('name', device)}")
        default_rate = int(round(float(info["default_samplerate"])))
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
        requested_device = self.config.input_device
        device_candidates = [requested_device]
        if requested_device is not None:
            device_candidates.append(None)
        errors = []
        for device_override in device_candidates:
            requested, rates, max_input_channels, hostapi_name, actual_device = self._candidate_input_rates(device_override)
            is_wasapi = "WASAPI" in hostapi_name.upper()
            channels = [2, 1] if max_input_channels >= 2 else [1]
            extra_settings_variants = [None]
            if is_wasapi and hasattr(sd, "WasapiSettings"):
                try:
                    extra_settings_variants = [sd.WasapiSettings(auto_convert=True), None]
                except Exception:
                    extra_settings_variants = [None]
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
                        "blocksize": max(160, int((rate or requested or 48000) * 0.1)),
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
                    try:
                        info = sd.query_devices(actual_device, kind="input")
                        self._emit(
                            running=True,
                            message=f"Микрофон открыт: {info.get('name', actual_device)} · {actual_rate_int} Hz · {channel_count}ch",
                        )
                    except Exception:
                        pass
                    if requested_device is not None and actual_device is None:
                        self._emit(
                            running=True,
                            message="Выбранный input не открылся; использую системный микрофон по умолчанию."
                        )
                    return stream, actual_rate_int
                except Exception as e:
                    label_rate = f"{rate} Hz" if rate is not None else "device default"
                    mode = "WASAPI auto-convert" if extra_settings is not None else "default"
                    device_label = "default input" if actual_device is None else f"device {actual_device}"
                    errors.append(f"{device_label}: {label_rate}/{channel_count}ch [{mode}]: {type(e).__name__}: {e}")
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
        stream = None
        com_initialized = False
        if sys.platform == "win32":
            try:
                hr = int(ctypes.windll.ole32.CoInitialize(None))
                com_initialized = hr >= 0
            except Exception as e:
                self._emit(running=True, message=f"Windows audio COM initialization warning: {type(e).__name__}: {e}")
        try:
            stream, input_rate = self._open_input_stream()
            model_stream = self.recognizer.create_stream()
            last_decode_at = time.monotonic()
            while not self.stop_event.is_set():
                if not self.audio_q:
                    time.sleep(0.01)
                    continue
                queued = []
                while self.audio_q:
                    queued.append(self.audio_q.popleft())
                if not queued:
                    continue
                source_audio = np.concatenate(queued).astype(np.float32, copy=False)
                raw_peak = float(np.max(np.abs(source_audio))) if source_audio.size else 0.0
                if raw_peak > 0.0005 and raw_peak < 0.08:
                    gain = min(12.0, max(1.0, 0.18 / raw_peak))
                else:
                    gain = 1.0
                self.input_gain = gain
                if gain > 1.0:
                    source_audio = np.clip(source_audio * gain, -1.0, 1.0).astype(np.float32)
                if input_rate != T_ONE_MODEL_RATE:
                    source_audio = resample_poly(
                        source_audio,
                        T_ONE_MODEL_RATE,
                        input_rate,
                    ).astype(np.float32)
                model_stream.accept_waveform(T_ONE_MODEL_RATE, source_audio)
                while self.recognizer.is_ready(model_stream):
                    started = time.monotonic()
                    self.recognizer.decode_stream(model_stream)
                    self.last_transcribe_duration = time.monotonic() - started
                    self.transcribe_attempts += 1
                    self.last_transcribe_at = time.time()
                result = self.recognizer.get_result_all(model_stream)
                text = _normalize_text(result.text)
                self.last_text = text
                if text and text != self.last_published_text:
                    now = time.monotonic()
                    # Avoid repainting OBS every decoder tick while still giving
                    # the subtitle browser genuinely live partial results.
                    if now - self.last_publish_at >= 0.12:
                        self._publish(text)
                        self.last_published_text = text
                        self.last_publish_at = now
                        self.last_transcribe_result = "текст получен"
                elif not text:
                    self.last_transcribe_result = "текста нет"
                if self.recognizer.is_endpoint(model_stream):
                    final_text = _normalize_text(self.recognizer.get_result(model_stream).text)
                    if final_text:
                        self._publish(final_text)
                    self.last_published_text = ""
                    self.last_publish_at = 0.0
                    self.recognizer.reset(model_stream)
                if time.monotonic() - last_decode_at > 5:
                    self._emit(running=True)
                    last_decode_at = time.monotonic()
        except Exception as e:
            self._emit(
                running=False,
                model_loading=False,
                message=f"Ошибка STT: {type(e).__name__}: {e}",
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
            self.stream = None
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    def _publish(self, text: str):
        normalized = _normalize_text(text)
        if not normalized or _should_skip_segment(None, normalized):
            return
        ts = time.time()
        self.on_subtitle(
            {
                "source": True,
                "text": normalized,
                "start": None,
                "end": None,
                "language": self.config.language or "ru",
                "timestamp": ts,
            }
        )
        tracks = self.get_subtitle_tracks()
        if not self.translator:
            return
        for track in tracks:
            if not track.get("enabled", True) or str(track.get("mode", "source")) != "translate":
                continue
            target_language = str(track.get("language", "")).strip().lower().split("-")[0]
            source_language = (self.config.language or "ru").lower().split("-")[0]
            if not target_language or target_language == source_language:
                continue

            def translate_one(
                track_id=track.get("id", target_language),
                target=target_language,
                source_text=normalized,
                source_language=source_language,
                timestamp=ts,
            ):
                try:
                    translated = self.translator.translate(source_text, source_language, target)
                    if translated:
                        self.on_subtitle(
                            {
                                "source": False,
                                "track_id": track_id,
                                "text": _normalize_text(translated),
                                "start": None,
                                "end": None,
                                "language": target,
                                "timestamp": timestamp,
                            }
                        )
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
