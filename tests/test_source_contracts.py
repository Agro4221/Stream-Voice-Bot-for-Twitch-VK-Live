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
        self.assertIn("127.0.0.1:8787", helper)
        self.assertIn("Invoke-WebRequest", helper)
        self.assertIn("Start-Process $Url", helper)
        self.assertIn('onclick="shutdownBot()"', web)
        self.assertIn("await api('/api/shutdown',{method:'POST'})", web)
        self.assertIn("refreshTimer=null", web)
        self.assertIn("let logTimer=null", web)
        self.assertIn("api('/api/logs?limit=250')", web)
        self.assertIn("api('/api/logs/clear'", web)
        launcher = self.read("start_bot.bat")
        debug = self.read("start_bot_debug.bat")
        self.assertIn('start "" "%~dp0.venv\\Scripts\\pythonw.exe" -m stream_voice_bot', launcher)
        self.assertIn('powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\\open_admin.ps1"', launcher)
        self.assertIn("exit /b 0", launcher)
        self.assertIn('"Stream Voice Bot - Debug"', debug)


    def test_vk_bridge_contract(self):
        src = self.read("stream_voice_bot/vk_bridge/bridge.js")
        self.assertIn('import VKPLMessageClient from "vklive-message-client";', src)
        self.assertIn('auth: "readonly"', src)
        self.assertIn('channels: [channel]', src)
        self.assertIn('client.on("message"', src)
        self.assertIn('ctx?.message?.text', src)
        self.assertIn('vklive-message-client@5.3.2', src)
        self.assertIn("--self-test", src)


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
        self.assertIn("from .log_buffer import get_logs, install as install_log_buffer", app)
        self.assertIn('@app.get("/api/logs")', app)
        self.assertIn('@app.post("/api/logs/clear")', app)

    def test_stt_is_bounded_and_validated(self):
        src = self.read("stream_voice_bot/stt.py")
        self.assertIn("deque(maxlen=200)", src)
        self.assertIn("def _candidate_input_rates", src)
        self.assertIn("def _open_input_stream", src)
        self.assertIn("stream = sd.InputStream(", src)
        self.assertIn("stream.start()", src)
        self.assertIn("resample_poly", src)
        self.assertIn("overlap_seconds must be smaller than chunk_seconds", src)


if __name__ == "__main__":
    unittest.main()
