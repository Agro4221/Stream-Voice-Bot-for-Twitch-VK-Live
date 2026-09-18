from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    import stream_voice_bot.app as app_module
    import stream_voice_bot.db as db_module
    import stream_voice_bot.stt as stt_module
    import stream_voice_bot.text_normalize as normalize_module
    import stream_voice_bot.twitch as twitch_module
    import stream_voice_bot.tts as tts_module
    import stream_voice_bot.vkplay as vkplay_module
    from stream_voice_bot.models import QueueItem
    from fastapi.testclient import TestClient

    assert app_module._read_app_version(ROOT) == version
    assert twitch_module.TwitchService.REQUIRED_SCOPES == [
        "user:read:chat",
        "channel:read:redemptions",
        "channel:manage:redemptions",
    ]
    assert vkplay_module.normalize_channel(
        "https://live.vkvideo.ru/example_channel?x=1"
    ) == "example_channel"

    # Verify the fixed TTS speed semantics and dB gain numerically without
    # requiring a real audio device.
    player = tts_module.AudioPlayer(tts_module.PlayerSettings(speed=1.5))
    source = np.ones(4800, dtype=np.float32) * 0.25
    faster = player._prepare(source, 48000, 48000, 0.0)
    assert len(faster) < len(source), (len(source), len(faster))

    player.settings.speed = 0.5
    slower = player._prepare(source, 48000, 48000, 0.0)
    assert len(slower) > len(source), (len(source), len(slower))

    neutral = player._prepare(source, 48000, 48000, 0.0)
    loud = player._prepare(source, 48000, 48000, 6.0)
    neutral_rms = float(np.sqrt(np.mean(neutral * neutral)))
    loud_rms = float(np.sqrt(np.mean(loud * loud)))
    assert loud_rms > neutral_rms * 1.5, (neutral_rms, loud_rms)

    # Test the audio-test endpoint path at AudioPlayer level with a fully
    # mocked sounddevice backend. This catches the former volume_db regression
    # without needing VB-CABLE on the runner.
    class DummyStream:
        def __init__(self):
            self.blocks = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def write(self, block):
            assert getattr(block, "ndim", 0) == 2
            assert block.shape[1] == 2
            self.blocks += 1

    class FakeSD:
        def query_devices(self, device=None, kind=None):
            return {
                "name": "Mock Output",
                "max_output_channels": 2,
                "default_samplerate": 48000,
            }

        def check_output_settings(self, **kwargs):
            assert kwargs["samplerate"] == 48000
            assert kwargs["channels"] == 2

        def OutputStream(self, **kwargs):
            assert kwargs["channels"] == 2
            assert kwargs["samplerate"] == 48000
            return DummyStream()

    real_sd = tts_module.sd
    try:
        tts_module.sd = FakeSD()
        test_player = tts_module.AudioPlayer(tts_module.PlayerSettings(volume_db=3.0))
        assert test_player.test_tone(volume_db=3.0) == "finished"
        assert test_player.last_sample_rate == 48000
    finally:
        tts_module.sd = real_sd

    # STT validation is exercised independently of FastAPI routing.
    with tempfile.TemporaryDirectory() as tmp:
        db = db_module.Database(Path(tmp) / "smoke.sqlite3")
        service = stt_module.STTService(
            db,
            Mock(),
            Mock(),
            Mock(return_value=[]),
        )
        try:
            service.save_config(chunk_seconds=4.0, overlap_seconds=0.5)
            try:
                service.save_config(chunk_seconds=1.0, overlap_seconds=1.0)
            except ValueError:
                pass
            else:
                raise AssertionError("STT accepted overlap >= chunk")
        finally:
            pass

    # Full application construction plus real HTTP routes.
    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        (temp_root / "VERSION").write_text(version + "\n", encoding="utf-8")
        app = app_module.create_app(temp_root)
        assert app.version == version
        assert app.title == "Stream Voice Bot"

        async def exercise_http():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                state = await client.get("/api/state")
                assert state.status_code == 200, state.text
                payload = state.json()
                assert payload["app"]["version"] == version
                assert payload["queue"]["current"] is None

                normalized_response = await client.post(
                    "/api/tts/normalize",
                    json={"text": "Привет 25% и 12:30!"},
                )
                assert normalized_response.status_code == 200
                assert "процентов" in normalized_response.json()["normalized"]

                invalid_stt = await client.post(
                    "/api/stt/config",
                    json={"chunk_seconds": 1.0, "overlap_seconds": 1.0},
                )
                assert invalid_stt.status_code == 400, invalid_stt.text

        asyncio.run(exercise_http())
        app.state.tts_queue.shutdown()

    # Deterministic queue clear race: the second item must be cleared while
    # the first item is still inside model.generate().
    class FakeDB:
        def __init__(self):
            self.next_id = 1
            self.rows = {}
            self.lock = threading.Lock()

        def add_history(self, username, text, source, created_at, repeat_of=None, profile="normal", status="queued"):
            with self.lock:
                hid = self.next_id
                self.next_id += 1
                self.rows[hid] = {"id": hid, "status": status}
                return hid

        def set_history_status(self, hid, status, duration_sec=None):
            with self.lock:
                self.rows[hid]["status"] = status

        def get_history(self, hid):
            with self.lock:
                return dict(self.rows[hid])

        def mark_pending_history(self, ids, status="cleared"):
            with self.lock:
                changed = 0
                for hid in ids:
                    if self.rows.get(hid, {}).get("status") == "queued":
                        self.rows[hid]["status"] = status
                        changed += 1
                return changed

    class BlockingModel:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def generate(self, *_args, **_kwargs):
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("queue race test timed out")
            return np.zeros(480, dtype=np.float32)

    class FakePlayer:
        def __init__(self):
            self.last_error = None
            self.last_device = "fake"
            self.last_sample_rate = 48000
            self._paused = False
            self._stopped = False

        @property
        def paused(self):
            return self._paused

        def play(self, *_args, **_kwargs):
            return "stopped" if self._stopped else "finished"

        def pause(self):
            self._paused = True

        def resume(self):
            self._paused = False

        def stop(self):
            self._stopped = True
            self._paused = False

        def skip(self):
            self._stopped = True
            self._paused = False

    fake_db = FakeDB()
    blocking_model = BlockingModel()
    fake_player = FakePlayer()
    queue = tts_module.TTSQueue(
        model=blocking_model,
        player=fake_player,
        speaker_getter=lambda: "xenia",
        history_db=fake_db,
        max_chars_getter=lambda: 1000,
        volume_setter=lambda: 0.0,
    )
    try:
        first_id = queue.enqueue(QueueItem("first", "u1", "test"))
        assert blocking_model.started.wait(5)
        second_id = queue.enqueue(QueueItem("second", "u2", "test"))
        queue.clear()
        assert fake_db.get_history(second_id)["status"] == "cleared"
        blocking_model.release.set()
        deadline = time.time() + 5
        while time.time() < deadline and fake_db.get_history(first_id)["status"] not in {"finished", "error"}:
            time.sleep(0.05)
        assert fake_db.get_history(first_id)["status"] == "finished"
    finally:
        queue.shutdown()

    # Basic normalization should remain usable under the installed dependency set.
    normalized = normalize_module.normalize_for_tts("Привет 25% и 12:30!")
    assert "процентов" in normalized
    assert "двадцать пять" in normalized or "двадцать пять" in normalized.replace("-", " ")
    assert normalized

    print("runtime smoke: OK")


if __name__ == "__main__":
    main()
