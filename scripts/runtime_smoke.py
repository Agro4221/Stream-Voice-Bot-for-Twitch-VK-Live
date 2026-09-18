from __future__ import annotations

import math
import tempfile
from pathlib import Path
from unittest.mock import Mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    import stream_voice_bot.app as app_module
    import stream_voice_bot.db as db_module
    import stream_voice_bot.stt as stt_module
    import stream_voice_bot.text_normalize as normalize_module
    import stream_voice_bot.twitch as twitch_module
    import stream_voice_bot.tts as tts_module
    import stream_voice_bot.vkplay as vkplay_module

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

    # Full application construction: imports and dependency wiring must work.
    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        (temp_root / "VERSION").write_text(version + "\n", encoding="utf-8")
        app = app_module.create_app(temp_root)
        try:
            assert app.version == version
            assert app.title == "Stream Voice Bot"
        finally:
            app.state.tts_queue.shutdown()

    # Global app created at import time owns a daemon TTS worker too.
    app_module.app.state.tts_queue.shutdown()

    # Basic normalization should remain usable under the installed dependency set.
    normalized = normalize_module.normalize_for_tts("Привет 25% и 12:30!")
    assert "процентов" in normalized
    assert "двадцать пять" in normalized or "двадцать пять" in normalized.replace("-", " ")
    assert normalized

    print("runtime smoke: OK")


if __name__ == "__main__":
    main()
