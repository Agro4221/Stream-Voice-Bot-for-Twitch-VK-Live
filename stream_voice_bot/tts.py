from __future__ import annotations

import os
import threading
import time
import tempfile
from pathlib import Path
from queue import Queue, Empty
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf
import torch
from scipy.signal import resample_poly
from torch.package import PackageImporter

from .models import QueueItem
from .text_normalize import normalize_for_tts


class SileroV5:
    def __init__(self, model_path: Path, device: str = "cpu"):
        self.model_path = model_path
        self.device = torch.device(device)
        self.model = None
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            if self.model is not None:
                return
            if not self.model_path.exists():
                raise FileNotFoundError(f"Silero model not found: {self.model_path}")
            self.model = PackageImporter(str(self.model_path)).load_pickle("tts_models", "model")
            self.model.to(self.device)

    def generate(self, text: str, speaker: str = "xenia", sample_rate: int = 48000) -> np.ndarray:
        self.load()
        clean_text = normalize_for_tts(text)
        if not clean_text:
            raise ValueError("Text is empty after normalization")

        # The supplied legacy voice implementation uses save_wav plus
        # put_accent/put_yo. We retain those useful V5 controls here.
        with self.lock:
            fd, tmp_name = tempfile.mkstemp(prefix="svb_", suffix=".wav")
            # IMPORTANT on Windows: close mkstemp's file descriptor before
            # Silero opens the same path, otherwise cleanup can raise WinError 32.
            os.close(fd)
            Path(tmp_name).unlink(missing_ok=True)
            try:
                with torch.no_grad():
                    self.model.save_wav(
                        text=clean_text,
                        speaker=speaker,
                        sample_rate=sample_rate,
                        audio_path=tmp_name,
                        put_accent=True,
                        put_yo=True,
                    )
                audio, sr = sf.read(tmp_name, dtype="float32", always_2d=False)
            finally:
                Path(tmp_name).unlink(missing_ok=True)

        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1).astype(np.float32)
        if sr != sample_rate:
            audio = resample_poly(audio, sample_rate, sr).astype(np.float32)
        return audio

    def normalize_preview(self, text: str) -> str:
        return normalize_for_tts(text)


@dataclass
class PlayerSettings:
    device: int | None = None
    volume: float = 1.0
    speed: float = 1.0
    speaker: str = "xenia"
    sample_rate: int = 48000
    chunk_ms: int = 50


class AudioPlayer:
    def __init__(self, settings: PlayerSettings):
        self.settings = settings
        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.pause_event = threading.Event()
        self._stream = None
        self.last_error: str | None = None
        self.last_device: str | None = None
        self.last_sample_rate: int | None = None
        self.last_device_default_rate: int | None = None

    def _resolve_output_device(self):
        info = (
            sd.query_devices(self.settings.device)
            if self.settings.device is not None
            else sd.query_devices(kind="output")
        )
        if int(info["max_output_channels"]) <= 0:
            raise RuntimeError(
                f"Selected audio device has no output channels: {info['name']}"
            )

        self.last_device = str(info["name"])
        self.last_device_default_rate = int(round(float(info["default_samplerate"])))

        # VB-CABLE on Windows 10/11 is intended to run at stereo 48 kHz.
        # Using one fixed format on the whole TTS -> CABLE -> OBS path avoids
        # pitch/speed changes caused by clock/rate mismatches.
        is_cable = "cable input" in self.last_device.lower() or "vb-audio" in self.last_device.lower()
        target_rate = 48000 if is_cable else self.settings.sample_rate

        try:
            sd.check_output_settings(
                device=self.settings.device,
                samplerate=target_rate,
                channels=2,
                dtype="float32",
            )
        except Exception as e:
            if is_cable:
                raise RuntimeError(
                    f"VB-CABLE не принимает 48 kHz stereo. "
                    f"Открой свойства CABLE Input -> Дополнительно -> "
                    f"поставь 2 канала, 16/24 bit, 48000 Hz. "
                    f"Текущая default rate: {self.last_device_default_rate} Hz. "
                    f"PortAudio: {e}"
                ) from e
            raise

        return info, target_rate

    def _prepare(self, audio: np.ndarray, source_rate: int, target_rate: int, volume: float) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32)
        speed = min(1.5, max(0.5, self.settings.speed))
        if abs(speed - 1.0) > 1e-3 and len(audio) > 100:
            audio = resample_poly(audio, 100, max(1, int(round(100 / speed)))).astype(np.float32)
        if source_rate != target_rate and len(audio) > 10:
            audio = resample_poly(audio, target_rate, source_rate).astype(np.float32)
        audio *= min(2.5, max(0.0, float(volume)))
        return np.clip(np.tanh(audio / 0.95) * 0.95, -0.98, 0.98).astype(np.float32)

    def play(self, audio: np.ndarray, sample_rate: int, volume: float = 1.0) -> str:
        self.stop_event.clear(); self.skip_event.clear(); self.last_error = None
        try:
            info, target_rate = self._resolve_output_device()
            self.last_sample_rate = target_rate
            audio = self._prepare(audio, sample_rate, target_rate, volume)
            chunk = max(256, int(target_rate * self.settings.chunk_ms / 1000))

            # VB-CABLE/OBS path is stereo; duplicate mono Silero output into
            # both channels instead of asking the Windows driver to convert it.
            audio_stereo = np.column_stack((audio, audio)).astype(np.float32)

            with sd.OutputStream(
                device=self.settings.device,
                samplerate=target_rate,
                channels=2,
                dtype="float32",
                blocksize=chunk,
                latency="low",
            ) as stream:
                self._stream = stream
                for pos in range(0, len(audio), chunk):
                    if self.stop_event.is_set(): return "stopped"
                    if self.skip_event.is_set(): return "skipped"
                    while self.pause_event.is_set() and not self.stop_event.is_set() and not self.skip_event.is_set():
                        time.sleep(0.05)
                    if self.stop_event.is_set(): return "stopped"
                    if self.skip_event.is_set(): return "skipped"
                    stream.write(audio_stereo[pos:pos+chunk])
            return "finished"
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return "audio_error"
        finally:
            self._stream = None
            self.stop_event.clear(); self.skip_event.clear()

    def test_tone(self, frequency: float = 880.0, duration: float = 0.35) -> str:
        self.stop_event.clear(); self.skip_event.clear(); self.last_error = None
        try:
            _, target_rate = self._resolve_output_device()
            self.last_sample_rate = target_rate
            t = np.arange(int(target_rate * duration), dtype=np.float32) / target_rate
            mono = (0.18 * np.sin(2*np.pi*frequency*t)).astype(np.float32)
            mono *= min(2.5, max(0.0, float(self.settings.volume)))
            audio_stereo = np.column_stack((mono, mono)).astype(np.float32)
            chunk = max(256, int(target_rate * self.settings.chunk_ms / 1000))
            with sd.OutputStream(device=self.settings.device, samplerate=target_rate, channels=2, dtype="float32", blocksize=chunk, latency="low") as stream:
                self._stream = stream
                for pos in range(0,len(audio),chunk):
                    if self.stop_event.is_set(): return "stopped"
                    stream.write(audio_stereo[pos:pos+chunk])
            return "finished"
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return "audio_error"
        finally:
            self._stream = None
            self.stop_event.clear()

    def stop(self): self.stop_event.set(); self.pause_event.clear()
    def skip(self): self.skip_event.set(); self.pause_event.clear()
    def pause(self): self.pause_event.set()
    def resume(self): self.pause_event.clear()
    @property
    def paused(self): return self.pause_event.is_set()


class TTSQueue:
    def __init__(self, model, player, speaker_getter, history_db, max_chars_getter, volume_setter=None):
        self.model=model; self.player=player; self.speaker_getter=speaker_getter
        self.volume_setter=volume_setter or (lambda: 1.0); self.db=history_db; self.max_chars_getter=max_chars_getter
        self.queue=Queue(); self.pending=[]; self.current=None; self.running=True
        self.lock=threading.RLock(); self.on_change=lambda: None
        self.thread=threading.Thread(target=self._worker, name="tts-worker", daemon=True); self.thread.start()

    def enqueue(self, item: QueueItem, history_id: int|None=None, profile="normal") -> int:
        if not item.text.strip(): raise ValueError("Text is empty")
        if len(item.text)>self.max_chars_getter(): raise ValueError(f"Text is too long (max {self.max_chars_getter()} chars)")
        if history_id is None:
            history_id=self.db.add_history(item.username,item.text,item.source,item.created_at,item.repeat_of,profile)
        with self.lock: self.pending.append((item,history_id,profile))
        self.queue.put((item,history_id,profile)); self.on_change(); return history_id

    def _worker(self):
        while self.running:
            try: item, hid, profile=self.queue.get(timeout=.2)
            except Empty: continue
            with self.lock:
                self.pending=[x for x in self.pending if x[1]!=hid]
                self.current=(item,hid,profile)
            self.db.set_history_status(hid,"playing"); self.on_change()
            result="finished"; started=time.monotonic()
            try:
                audio=self.model.generate(item.text, speaker=self._profile_speaker(profile), sample_rate=48000)
                result=self.player.play(audio,48000,volume=self._profile_volume(profile))
            except Exception as e:
                self.player.last_error=f"{type(e).__name__}: {e}"; result="error"
            self.db.set_history_status(hid,result,round(time.monotonic()-started,3))
            with self.lock: self.current=None
            self.on_change(); self.queue.task_done()

    def _profile_speaker(self, profile):
        fn=getattr(self,"_profile_speaker_getter",None); return fn(profile) if fn else self.speaker_getter()
    def _profile_volume(self, profile):
        fn=getattr(self,"_profile_volume_getter",None); return float(fn(profile)) if fn else float(self.volume_setter())
    def pause(self): self.player.pause(); self.on_change()
    def resume(self): self.player.resume(); self.on_change()
    def stop(self): self.player.stop(); self.on_change()
    def skip(self): self.player.skip(); self.on_change()
    def clear(self):
        with self.lock: self.pending.clear()
        while True:
            try: self.queue.get_nowait(); self.queue.task_done()
            except Empty: break
        self.on_change()
    def queued(self):
        with self.lock: return [item.as_dict()|{"history_id":hid,"profile":profile} for item,hid,profile in self.pending]
    def state(self):
        with self.lock:
            cur=None if self.current is None else self.current[0].as_dict()|{"history_id":self.current[1],"profile":self.current[2]}
            return {"current":cur,"queued":self.queued(),"paused":self.player.paused,"last_audio_error":self.player.last_error,"last_audio_device":self.player.last_device,"last_audio_sample_rate":self.player.last_sample_rate}
    def shutdown(self): self.running=False; self.player.stop()
