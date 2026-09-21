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

# Keep local TTS unobtrusive on gaming/streaming PCs. PyTorch otherwise
# chooses a thread pool based on the host CPU, which can create avoidable
# CPU spikes during Silero inference. Override with SVB_TORCH_THREADS when
# a higher throughput profile is preferred.
_torch_threads = max(1, int(os.environ.get("SVB_TORCH_THREADS", "1")))
try:
    torch.set_num_threads(_torch_threads)
    torch.set_num_interop_threads(1)
except RuntimeError:
    # PyTorch may reject changing inter-op threads after work has started.
    pass


class SileroV5:
    def __init__(self, model_path: Path, device: str = "cpu"):
        self.model_path = model_path
        self.device = torch.device(device)
        self.model = None
        self._model_importer = None
        self._model_file = None
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            if self.model is not None:
                return
            if not self.model_path.exists():
                raise FileNotFoundError(f"Silero model not found: {self.model_path}")
            # Windows/PyTorch's native file reader can fail on paths with
            # non-ASCII characters even when Python can read the same file.
            # PackageImporter accepts a seekable binary file object, so pass
            # the already-open Python file instead of a Unicode path. Keep the
            # file and importer alive for as long as the model is in use.
            model_file = self.model_path.open("rb")
            try:
                importer = PackageImporter(model_file)
                model = importer.load_pickle("tts_models", "model")
            except Exception:
                model_file.close()
                raise
            self._model_file = model_file
            self._model_importer = importer
            self.model = model
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
    volume_db: float = 0.0
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

    def _prepare(self, audio: np.ndarray, source_rate: int, target_rate: int, volume_db: float) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32)
        speed = min(1.5, max(0.5, self.settings.speed))
        if abs(speed - 1.0) > 1e-3 and len(audio) > 100:
            # speed > 1 shortens playback; speed < 1 lengthens it.
            numerator = max(1, int(round(100.0 / speed)))
            audio = resample_poly(audio, numerator, 100).astype(np.float32)
        if source_rate != target_rate and len(audio) > 10:
            audio = resample_poly(audio, target_rate, source_rate).astype(np.float32)
        # Stable, human-friendly volume control: normalize the generated clip
        # to a predictable peak, then apply a real dB gain. Finally use a very
        # simple peak limiter instead of tanh, so +dB settings remain audible.
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 1e-6:
            audio = audio * (0.50 / peak)  # about -6 dBFS before profile gain; leaves headroom for the slider
        gain = float(10.0 ** (float(volume_db) / 20.0))
        audio = audio * gain
        # Soft-limit only extreme peaks; do not rescale the whole clip to a
        # common peak after applying the user's gain, otherwise the slider
        # appears to do nothing.
        if len(audio):
            audio = np.tanh(audio / 0.95) * 0.95
        return np.clip(audio, -0.95, 0.95).astype(np.float32)

    def play(self, audio: np.ndarray, sample_rate: int, volume_db: float = 0.0) -> str:
        # Do not clear stop/skip here: the worker may receive the command
        # while Silero is still generating the current item. Clearing here
        # would make that command disappear before playback starts.
        self.last_error = None
        try:
            info, target_rate = self._resolve_output_device()
            self.last_sample_rate = target_rate
            audio = self._prepare(audio, sample_rate, target_rate, volume_db)
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

    def test_tone(self, frequency: float = 880.0, duration: float = 0.35, volume_db: float | None = None) -> str:
        self.stop_event.clear(); self.skip_event.clear(); self.last_error = None
        try:
            _, target_rate = self._resolve_output_device()
            self.last_sample_rate = target_rate
            t = np.arange(int(target_rate * duration), dtype=np.float32) / target_rate
            mono = (0.18 * np.sin(2*np.pi*frequency*t)).astype(np.float32)
            effective_volume_db = self.settings.volume_db if volume_db is None else float(volume_db)
            gain = float(10.0 ** (effective_volume_db / 20.0))
            mono *= gain
            mono = np.tanh(mono / 0.95) * 0.95
            audio_stereo = np.column_stack((mono, mono)).astype(np.float32)
            chunk = max(256, int(target_rate * self.settings.chunk_ms / 1000))
            with sd.OutputStream(device=self.settings.device, samplerate=target_rate, channels=2, dtype="float32", blocksize=chunk, latency="low") as stream:
                self._stream = stream
                for pos in range(0, len(mono), chunk):
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
        self.volume_setter=volume_setter or (lambda: 0.0); self.db=history_db; self.max_chars_getter=max_chars_getter
        self.max_queue_items = 200
        self.queue=Queue(maxsize=self.max_queue_items); self.pending=[]; self.current=None; self.running=True
        self.cancelled_ids=set()
        self.lock=threading.RLock(); self.on_change=lambda: None
        self.thread=threading.Thread(target=self._worker, name="tts-worker", daemon=True); self.thread.start()

    def enqueue(self, item: QueueItem, history_id: int|None=None, profile="normal") -> int:
        if not item.text.strip(): raise ValueError("Text is empty")
        if len(item.text)>self.max_chars_getter(): raise ValueError(f"Text is too long (max {self.max_chars_getter()} chars)")

        created_history = history_id is None
        with self.lock:
            if len(self.pending) >= self.max_queue_items:
                raise RuntimeError(
                    f"TTS queue is full (max {self.max_queue_items} pending items)"
                )
            if created_history:
                history_id = self.db.add_history(
                    item.username,
                    item.text,
                    item.source,
                    item.created_at,
                    item.repeat_of,
                    profile,
                )
            self.pending.append((item, history_id, profile))
            try:
                self.queue.put_nowait((item, history_id, profile))
            except Exception as e:
                self.pending.pop()
                if created_history:
                    try:
                        self.db.set_history_status(history_id, "error")
                    except Exception:
                        pass
                raise RuntimeError("TTS queue is full") from e
        self.on_change(); return history_id

    def _worker(self):
        while self.running:
            try:
                item, hid, profile = self.queue.get(timeout=.2)
            except Empty:
                continue

            with self.lock:
                self.pending = [x for x in self.pending if x[1] != hid]
                canceled = hid in self.cancelled_ids
                if canceled:
                    self.cancelled_ids.discard(hid)
                else:
                    self.current = (item, hid, profile)

            if canceled:
                try:
                    self.db.set_history_status(hid, "cleared")
                except Exception as e:
                    self.player.last_error = f"history-clear: {type(e).__name__}: {e}"
                try:
                    self.on_change()
                except Exception:
                    pass
                finally:
                    self.queue.task_done()
                continue

            result = "finished"
            started = time.monotonic()
            try:
                try:
                    self.db.set_history_status(hid, "playing")
                except Exception as e:
                    self.player.last_error = f"history-start: {type(e).__name__}: {e}"

                try:
                    self.on_change()
                except Exception as e:
                    self.player.last_error = f"queue-state: {type(e).__name__}: {e}"

                try:
                    audio = self.model.generate(
                        item.text,
                        speaker=self._profile_speaker(profile),
                        sample_rate=48000,
                    )
                    result = self.player.play(
                        audio,
                        48000,
                        volume_db=self._profile_volume(profile),
                    )
                except Exception as e:
                    self.player.last_error = f"{type(e).__name__}: {e}"
                    result = "error"
            finally:
                try:
                    self.db.set_history_status(
                        hid,
                        result,
                        round(time.monotonic() - started, 3),
                    )
                except Exception as e:
                    self.player.last_error = f"history-finish: {type(e).__name__}: {e}"
                with self.lock:
                    self.current = None
                try:
                    self.on_change()
                except Exception as e:
                    self.player.last_error = f"queue-state: {type(e).__name__}: {e}"
                finally:
                    self.queue.task_done()

    def _profile_speaker(self, profile):
        fn=getattr(self,"_profile_speaker_getter",None); return fn(profile) if fn else self.speaker_getter()
    def _profile_volume(self, profile):
        fn=getattr(self,"_profile_volume_getter",None); return float(fn(profile)) if fn else float(self.volume_setter())
    def pause(self): self.player.pause(); self.on_change()
    def resume(self): self.player.resume(); self.on_change()
    def stop(self):
        with self.lock:
            active = self.current is not None
        if active:
            self.player.stop()
        self.on_change()

    def skip(self):
        with self.lock:
            active = self.current is not None
        if active:
            self.player.skip()
        self.on_change()

    def clear(self):
        removed_ids = []
        with self.lock:
            removed_ids = [hid for _, hid, _ in self.pending]
            self.cancelled_ids.update(removed_ids)
            self.pending.clear()
            while True:
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except Empty:
                    break
        if removed_ids and hasattr(self.db, "mark_pending_history"):
            self.db.mark_pending_history(removed_ids, status="cleared")
        self.on_change()
        return len(removed_ids)
    def queued(self):
        with self.lock: return [item.as_dict()|{"history_id":hid,"profile":profile} for item,hid,profile in self.pending]
    def state(self):
        with self.lock:
            cur=None if self.current is None else self.current[0].as_dict()|{"history_id":self.current[1],"profile":self.current[2]}
            return {"current":cur,"queued":self.queued(),"paused":self.player.paused,"last_audio_error":self.player.last_error,"last_audio_device":self.player.last_device,"last_audio_sample_rate":self.player.last_sample_rate}
    def shutdown(self):
        self.running=False
        self.player.stop()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=2.0)
