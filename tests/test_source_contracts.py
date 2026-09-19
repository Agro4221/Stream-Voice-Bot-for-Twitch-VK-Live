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

    def test_runtime_lifecycle_and_silent_launcher_contract(self):
        main = self.read("stream_voice_bot/__main__.py")
        silent = self.read("start_bot_silent.bat")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn("access_log=False", main)
        self.assertIn('log_level="warning"', main)
        helper = self.read("scripts/open_admin.ps1")
        self.assertIn("access_log=False", main)
        self.assertIn('log_level="warning"', main)
        self.assertIn("app.state.server = server", main)
        self.assertIn('start "" "%~dp0.venv\\Scripts\\pythonw.exe" -m stream_voice_bot', silent)
        self.assertIn('start "" powershell.exe -NoProfile -WindowStyle Hidden', silent)
        self.assertIn("open_admin.ps1", helper)
        self.assertIn("Invoke-WebRequest", helper)
        self.assertIn("Start-Process $Url", helper)
        self.assertIn('onclick="shutdownBot()"', web)
        self.assertIn("await api('/api/shutdown',{method:'POST'})", web)
        self.assertIn("refreshTimer=null", web)

    def test_vk_watchdog_contract(self):
        src = self.read("stream_voice_bot/vk_bridge/bridge.js")
        self.assertIn('const WS_URL = "wss://pubsub.live.vkvideo.ru/connection/websocket?cf_protocol_version=v2";', src)
        self.assertIn('channel-chat:${channelId}', src)
        self.assertIn("function parseChatPush(payload)", src)
        self.assertIn("socket.ping()", src)
        self.assertIn("lastPongAt", src)
        self.assertIn("--self-test", src)
        self.assertIn("body?.createdAt", src)

    def test_app_and_db_stability_contract(self):
        app = self.read("stream_voice_bot/app.py")
        db = self.read("stream_voice_bot/db.py")
        self.assertIn('db.claim_event("twitch:" + str(dedupe_id))', app)
        self.assertIn('queue.enqueue(QueueItem(text, username, "twitch-chat", created_at=created_at), profile="normal")', app)
        self.assertIn("from .models import QueueItem, utc_now", app)
        self.assertIn('source = "vkplay-reward" if reward_key else "vkplay-chat"', app)
        self.assertIn("parse_vk_reward_announcement(raw_text)", app)
        self.assertIn('username.casefold() == "chatbot" and "получает награду:" in raw_text.casefold()', app)
        self.assertIn("QueueItem(text, username, source, created_at=created_at)", app)
        self.assertIn("from .vkplay import VKPlayService, parse_vk_reward_announcement", app)
        self.assertIn('"cleared", "audio_error"', app)
        self.assertIn("def claim_event", db)
        self.assertIn("def mark_pending_history", db)
        self.assertIn("def clear_history", db)
        self.assertIn('@app.delete("/api/history")', app)
        self.assertIn('@app.post("/api/shutdown")', app)
        self.assertIn("server.should_exit = True", app)

    def test_stt_is_bounded_and_validated(self):
        src = self.read("stream_voice_bot/stt.py")
        self.assertIn("deque(maxlen=200)", src)
        self.assertIn("overlap_seconds must be smaller than chunk_seconds", src)


if __name__ == "__main__":
    unittest.main()
