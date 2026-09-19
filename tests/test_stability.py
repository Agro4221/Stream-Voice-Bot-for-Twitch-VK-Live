import tempfile
import unittest
from pathlib import Path

from stream_voice_bot.db import Database
from stream_voice_bot.vkplay import parse_vk_reward_announcement


class StabilityDatabaseTests(unittest.TestCase):
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
        event_text = "ChatBot: Jostik получает награду: Другая награда за 2: Тест"
        self.assertIsNone(parse_vk_reward_announcement(event_text))

if __name__ == "__main__":
    unittest.main()
