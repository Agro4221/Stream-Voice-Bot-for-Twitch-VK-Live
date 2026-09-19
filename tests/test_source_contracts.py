import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StabilitySourceContractTests(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_tts_controls_are_present(self):
        src = self.read("stream_voice_bot/tts.py")
        self.assertIn("def test_tone(self, frequency: float = 880.0, duration: float = 0.35, volume_db: float | None = None)", src)
        self.assertIn("numerator = max(1, int(round(100.0 / speed)))", src)
        self.assertIn("resample_poly(audio, numerator, 100)", src)
        self.assertIn("Do not clear stop/skip here", src)
        self.assertIn("active = self.current is not None", src)
        self.assertIn("if active:", src)
        self.assertIn("self.cancelled_ids.update(removed_ids)", src)

    def test_twitch_dcf_and_reconnect_contract(self):
        src = self.read("stream_voice_bot/twitch.py")
        self.assertIn('"scopes": " ".join(self.REQUIRED_SCOPES)', src)
        self.assertIn("await client.post(OAUTH_TOKEN, data=params)", src)
        self.assertIn("await self._open_eventsub_socket(reconnect_url)", src)
        self.assertIn("await old_ws.close()", src)

    def test_vk_watchdog_contract(self):
        src = self.read("stream_voice_bot/vk_bridge/bridge.js")
        self.assertIn("client?.socket", src)
        self.assertIn("lastPongAt", src)
        self.assertIn("--self-test", src)
        self.assertIn("ctx?.message?.createdAt", src)

    def test_app_and_db_stability_contract(self):
        app = self.read("stream_voice_bot/app.py")
        db = self.read("stream_voice_bot/db.py")
        self.assertIn('db.claim_event("twitch:" + str(dedupe_id))', app)
        self.assertIn('queue.enqueue(QueueItem(text, username, "twitch-chat"), profile="normal")', app)
        self.assertIn("from .vkplay import VKPlayService, parse_vk_reward_announcement", app)
        self.assertIn('"cleared", "audio_error"', app)
        self.assertIn("def claim_event", db)
        self.assertIn("def mark_pending_history", db)

    def test_stt_is_bounded_and_validated(self):
        src = self.read("stream_voice_bot/stt.py")
        self.assertIn("deque(maxlen=200)", src)
        self.assertIn("overlap_seconds must be smaller than chunk_seconds", src)


if __name__ == "__main__":
    unittest.main()
