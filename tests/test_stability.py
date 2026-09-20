import logging
import tempfile
import unittest
from pathlib import Path

from stream_voice_bot.db import Database
from stream_voice_bot.vkplay import parse_vk_reward_announcement
from stream_voice_bot import log_buffer
import stream_voice_bot.stt as stt_module


class StabilityDatabaseTests(unittest.TestCase):
    def test_stt_falls_back_to_device_supported_sample_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            service = stt_module.STTService(db, lambda _: None, lambda _: None)
            service.config.sample_rate = 16000
            service.config.input_device = 3

            class FakeStream:
                def __init__(self, samplerate):
                    self.samplerate = samplerate
                    self.started = False
                    self.closed = False

                def start(self):
                    if self.samplerate != 48000:
                        raise stt_module.sd.PortAudioError("unsupported")
                    self.started = True

                def stop(self):
                    self.started = False

                def close(self):
                    self.closed = True

            class FakeSD:
                PortAudioError = RuntimeError

                def query_devices(self, device=None, kind=None):
                    self.queried = (device, kind)
                    return {"default_samplerate": 48000, "max_input_channels": 1, "name": "Mock mic", "hostapi": 0}

                def query_hostapis(self, index):
                    return {"name": "Windows DirectSound"}

                def InputStream(self, **kwargs):
                    rate = kwargs.get("samplerate", 48000)
                    return FakeStream(rate)

            real_sd = stt_module.sd
            try:
                stt_module.sd = FakeSD()
                stream, rate = service._open_input_stream()
                self.assertEqual(rate, 48000)
                self.assertEqual(service.input_stream_sample_rate, 48000)
                self.assertFalse(stream.closed)
                stream.close()
            finally:
                stt_module.sd = real_sd


    def test_stt_uses_wasapi_auto_convert_for_shared_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            service = stt_module.STTService(db, lambda _: None, lambda _: None)
            service.config.sample_rate = 16000
            service.config.input_device = 7

            class FakeWasapiSettings:
                def __init__(self, auto_convert=False):
                    self.auto_convert = auto_convert

            class FakeStream:
                def __init__(self, kwargs):
                    self.kwargs = kwargs
                    self.closed = False

                def start(self):
                    if self.kwargs.get("channels") == 1 and not getattr(
                        self.kwargs.get("extra_settings"), "auto_convert", False
                    ):
                        raise RuntimeError("AUDCLNT_E_UNSUPPORTED_FORMAT")
                def close(self):
                    self.closed = True

            class FakeSD:
                WasapiSettings = FakeWasapiSettings
                def query_devices(self, device=None, kind=None):
                    return {
                        "default_samplerate": 48000,
                        "max_input_channels": 2,
                        "name": "Mock WASAPI mic",
                        "hostapi": 0,
                    }
                def query_hostapis(self, index):
                    return {"name": "Windows WASAPI"}
                def InputStream(self, **kwargs):
                    return FakeStream(kwargs)

            real_sd = stt_module.sd
            try:
                stt_module.sd = FakeSD()
                stream, rate = service._open_input_stream()
                self.assertEqual(rate, 16000)
                self.assertFalse(stream.closed)
                self.assertTrue(stream.kwargs["extra_settings"].auto_convert)
                self.assertEqual(stream.kwargs["channels"], 1)
                stream.close()
            finally:
                stt_module.sd = real_sd


    def test_legacy_chat_messages_schema_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            import sqlite3
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL DEFAULT 'twitch',
                    username TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

            db = Database(path)
            db.save_chat_message(
                "vkplay", "42", "channel", "", "", "Jostik",
                "Привет", "2026-09-20T07:00:00", "{}"
            )

            with db._connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
            for name in {"message_id", "broadcaster_user_id", "broadcaster_login", "user_id", "raw_json"}:
                self.assertIn(name, columns)

    def test_runtime_log_buffer_collects_messages(self):
        log_buffer.install()
        log_buffer.clear()
        logging.getLogger("stream_voice_bot.test").info("runtime-log-test")
        logs = log_buffer.get_logs(10)
        self.assertTrue(any(item["message"] == "runtime-log-test" for item in logs))
        log_buffer.clear()
        self.assertEqual(log_buffer.get_logs(10), [])

    def test_event_claim_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            self.assertTrue(db.claim_event("twitch:abc"))
            self.assertFalse(db.claim_event("twitch:abc"))
            self.assertTrue(db.claim_event("vk:def"))

    def test_clear_marks_only_queued_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            first = db.add_history("u1", "hello", "test", "2026-09-18T20:00:00", status="queued")
            second = db.add_history("u2", "playing", "test", "2026-09-18T20:00:01", status="playing")
            changed = db.mark_pending_history([first, second], status="cleared")
            self.assertEqual(changed, 1)
            self.assertEqual(db.get_history(first)["status"], "cleared")
            self.assertEqual(db.get_history(second)["status"], "playing")

    def test_clear_history_removes_only_terminal_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            finished = db.add_history("u1", "finished", "vkplay-chat", "2026-09-18T20:00:00", status="finished")
            received = db.add_history("u2", "received", "twitch-chat", "2026-09-18T20:00:01", status="received")
            queued = db.add_history("u3", "queued", "vkplay-chat", "2026-09-18T20:00:02", status="queued")
            playing = db.add_history("u4", "playing", "twitch-chat", "2026-09-18T20:00:03", status="playing")

            removed = db.clear_history()

            self.assertEqual(removed, 2)
            self.assertIsNone(db.get_history(finished))
            self.assertIsNone(db.get_history(received))
            self.assertEqual(db.get_history(queued)["status"], "queued")
            self.assertEqual(db.get_history(playing)["status"], "playing")

    def test_vk_reward_announcement_extracts_viewer_text(self):
        event_text = "**ChatBot: Jostik** получает награду: Озвучить сообщение за 2: Тест-Тест 123"
        self.assertEqual(
            parse_vk_reward_announcement(event_text),
            {"username": "Jostik", "text": "Тест-Тест 123"},
        )

    def test_vk_reward_announcement_without_reward_title_extracts_viewer_text(self):
        cases = [
            "ChatBot: Jostik получает награду за 2: Привет лох!",
            "Jostik получает награду за 2 Привет лох!",
            "**ChatBot: Jostik** получает награду за 2\nПривет лох!",
        ]
        for event_text in cases:
            with self.subTest(event_text=event_text):
                self.assertEqual(
                    parse_vk_reward_announcement(event_text),
                    {"username": "Jostik", "text": "Привет лох!"},
                )

    def test_vk_reward_chat_format_from_live_message(self):
        cases = [
            "Jostik получает награду: Озвучить сообщение за 2\nТест-Тест 123",
            "Jostik получает награду: Озвучить сообщение за 2 Тест-Тест 123",
            "ChatBot: Jostik получает награду: Озвучить сообщение за 2: Тест-Тест 123",
            "Jostikполучает награду: Озвучить сообщение за 2 Проверка озвучки",
        ]
        expected = [
            {"username": "Jostik", "text": "Тест-Тест 123"},
            {"username": "Jostik", "text": "Тест-Тест 123"},
            {"username": "Jostik", "text": "Тест-Тест 123"},
            {"username": "Jostik", "text": "Проверка озвучки"},
        ]
        for event_text, expected_payload in zip(cases, expected):
            with self.subTest(event_text=event_text):
                self.assertEqual(
                    parse_vk_reward_announcement(event_text),
                    expected_payload,
                )

    def test_vk_reward_announcement_does_not_match_other_rewards(self):
        cases = [
            "ChatBot: Jostik получает награду: Другая награда за 2: Тест",
            "ChatBot: Jostik получает награду: Совсем другая награда за 500: Тест",
        ]
        for event_text in cases:
            with self.subTest(event_text=event_text):
                self.assertIsNone(parse_vk_reward_announcement(event_text))

if __name__ == "__main__":
    unittest.main()
