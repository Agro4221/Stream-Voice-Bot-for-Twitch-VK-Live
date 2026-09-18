import tempfile
import unittest
from pathlib import Path

from stream_voice_bot.db import Database


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


if __name__ == "__main__":
    unittest.main()
