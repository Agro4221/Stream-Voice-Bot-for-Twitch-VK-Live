from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


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
    def __init__(self, db, on_subtitle: Callable[[dict], None], on_status: Callable[[dict], None]):
        self.db = db
        self.on_subtitle = on_subtitle
        self.on_status = on_status
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
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.audio_q: deque[np.ndarray] = deque()
        self.last_text = ""

    def state(self):
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "model_loaded": self.model is not None,
            "model": self.config.model_name,
            "language": self.config.language,
            "input_device": self.config.input_device,
            "sample_rate": self.config.sample_rate,
            "chunk_seconds": self.config.chunk_seconds,
            "overlap_seconds": self.config.overlap_seconds,
            "beam_size": self.config.beam_size,
            "compute_type": self.config.compute_type,
            "last_text": self.last_text,
        }

    def save_config(self, **kwargs):
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
            # A loaded WhisperModel keeps the previous model in memory.
            # Drop it so the newly selected model is loaded on next STT start.
            self.model = None
            self.on_status({
                "running": bool(self.thread and self.thread.is_alive()),
                "message": "Модель STT изменена; новая модель загрузится при следующем запуске STT.",
            })

    def _load_model(self):
        if self.model is not None:
            return
        self.on_status({"running": False, "model_loading": True, "message": f"Загрузка STT: {self.config.model_name}..."})
        try:
            self.model = WhisperModel(
                self.config.model_name,
                device="cuda",
                compute_type=self.config.compute_type,
            )
        except Exception as e:
            # Keep the user informed and make a CPU fallback available.
            self.on_status({
                "running": False,
                "model_loading": False,
                "message": f"GPU STT не запустился: {type(e).__name__}: {e}. Пробую CPU int8.",
            })
            self.model = WhisperModel(
                self.config.model_name,
                device="cpu",
                compute_type="int8",
            )
        self.on_status({"running": False, "model_loading": False, "model_loaded": True, "message": "STT модель готова"})

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._load_model()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="stt-worker", daemon=True)
        self.thread.start()
        self.on_status({"running": True, "message": "STT запущен"})

    def stop(self):
        self.stop_event.set()
        self.on_status({"running": False, "message": "STT остановлен"})

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.on_status({"running": True, "message": f"Audio input: {status}"})
        # Copy because PortAudio reuses the buffer.
        self.audio_q.append(indata[:, 0].copy())

    def _run(self):
        sample_rate = self.config.sample_rate
        chunk_samples = int(sample_rate * self.config.chunk_seconds)
        overlap_samples = int(sample_rate * self.config.overlap_seconds)

        try:
            with sd.InputStream(
                device=self.config.input_device,
                channels=1,
                samplerate=sample_rate,
                dtype="float32",
                callback=self._callback,
                blocksize=1600,
                latency="low",
            ):
                buf = np.zeros(0, dtype=np.float32)
                while not self.stop_event.is_set():
                    if self.audio_q:
                        buf = np.concatenate([buf, self.audio_q.popleft()])
                    else:
                        time.sleep(0.03)
                        continue

                    if len(buf) < chunk_samples:
                        continue

                    audio = buf[:chunk_samples]
                    buf = buf[chunk_samples - overlap_samples:]

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
                            self.last_text = text
                            self.on_subtitle({
                                "text": text,
                                "start": start,
                                "end": end,
                                "language": getattr(info, "language", self.config.language),
                                "timestamp": time.time(),
                            })
                    except Exception as e:
                        self.on_status({
                            "running": True,
                            "message": f"STT error: {type(e).__name__}: {e}",
                        })
        except Exception as e:
            self.on_status({
                "running": False,
                "message": f"Audio input error: {type(e).__name__}: {e}",
            })
