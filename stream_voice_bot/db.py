from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self):
        with self.lock, self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                duration_sec REAL,
                status TEXT NOT NULL DEFAULT 'queued',
                repeat_of INTEGER,
                profile TEXT DEFAULT 'normal',
                external_event_id TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS twitch_rewards (
                reward_id TEXT PRIMARY KEY,
                reward_title TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                profile TEXT NOT NULL DEFAULT 'normal',
                auto_fulfill INTEGER NOT NULL DEFAULT 1,
                user_input_required INTEGER NOT NULL DEFAULT 1,
                prompt TEXT NOT NULL DEFAULT 'Введите текст для озвучки',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT 'twitch',
                message_id TEXT,
                broadcaster_user_id TEXT,
                broadcaster_login TEXT,
                user_id TEXT,
                username TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                raw_json TEXT,
                UNIQUE(platform, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_created_at ON chat_messages(created_at);
            CREATE TABLE IF NOT EXISTS event_dedupe (
                message_id TEXT PRIMARY KEY,
                seen_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_event_dedupe_seen_at ON event_dedupe(seen_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_history_external_event_id
                ON history(external_event_id)
                WHERE external_event_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS event_inbox (
                event_key TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                history_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_event_inbox_status_updated
                ON event_inbox(status, updated_at);
            """)
            hcols = {r["name"] for r in conn.execute("PRAGMA table_info(history)").fetchall()}
            if "profile" not in hcols:
                conn.execute("ALTER TABLE history ADD COLUMN profile TEXT DEFAULT 'normal'")
            if "external_event_id" not in hcols:
                conn.execute("ALTER TABLE history ADD COLUMN external_event_id TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_external_event_id "
                "ON history(external_event_id) WHERE external_event_id IS NOT NULL"
            )
            rcols = {r["name"] for r in conn.execute("PRAGMA table_info(twitch_rewards)").fetchall()}
            if "user_input_required" not in rcols:
                conn.execute("ALTER TABLE twitch_rewards ADD COLUMN user_input_required INTEGER NOT NULL DEFAULT 1")
            if "prompt" not in rcols:
                conn.execute("ALTER TABLE twitch_rewards ADD COLUMN prompt TEXT NOT NULL DEFAULT 'Введите текст для озвучки'")
            ccols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
            chat_migrations = {
                "platform": "TEXT NOT NULL DEFAULT 'twitch'",
                "message_id": "TEXT",
                "broadcaster_user_id": "TEXT",
                "broadcaster_login": "TEXT",
                "user_id": "TEXT",
                "raw_json": "TEXT",
            }
            for name, definition in chat_migrations.items():
                if name not in ccols:
                    conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {name} {definition}")

            # Older releases created chat_messages without the current
            # message metadata columns. SQLite cannot add a UNIQUE constraint
            # to an existing table with ALTER TABLE, so the duplicate guard is
            # handled by save_chat_message() and event_dedupe instead.

    def add_history(self, username, text, source, created_at, repeat_of=None, profile="normal", status="queued", external_event_id=None):
        with self.lock, self._connect() as conn:
            if external_event_id:
                existing = conn.execute(
                    "SELECT id FROM history WHERE external_event_id=? LIMIT 1",
                    (str(external_event_id),),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cur = conn.execute(
                "INSERT INTO history(username,text,source,created_at,queued_at,status,repeat_of,profile,external_event_id) VALUES(?,?,?,?,datetime('now'),?,?,?,?)",
                (username, text, source, created_at, status, repeat_of, profile, external_event_id),
            )
            return int(cur.lastrowid)

    def get_history_by_external_event_id(self, external_event_id):
        key = str(external_event_id or "").strip()
        if not key:
            return None
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM history WHERE external_event_id=? LIMIT 1",
                (key,),
            ).fetchone()
            return dict(row) if row else None

    def record_event(self, event_key, platform, event_type, payload_json):
        key = str(event_key or "").strip()
        if not key:
            return True
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO event_inbox(
                    event_key,platform,event_type,payload_json,status,attempts,created_at,updated_at
                ) VALUES(?,?,?,?, 'pending',0,?,?)""",
                (key, str(platform), str(event_type), str(payload_json), now, now),
            )
            row = conn.execute(
                "SELECT status FROM event_inbox WHERE event_key=?",
                (key,),
            ).fetchone()
            if cur.rowcount == 1:
                return True
            return bool(row and row["status"] != "done")

    def mark_event_queued(self, event_key, history_id):
        key = str(event_key or "").strip()
        if not key:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, self._connect() as conn:
            conn.execute(
                """UPDATE event_inbox
                   SET status='queued', history_id=?, attempts=attempts+1,
                       last_error=NULL, updated_at=?
                   WHERE event_key=?""",
                (int(history_id), now, key),
            )

    def mark_event_done(self, event_key):
        key = str(event_key or "").strip()
        if not key:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE event_inbox SET status='done', last_error=NULL, updated_at=? WHERE event_key=?",
                (now, key),
            )

    def mark_event_failed(self, event_key, error):
        key = str(event_key or "").strip()
        if not key:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, self._connect() as conn:
            conn.execute(
                """UPDATE event_inbox
                   SET status='pending', attempts=attempts+1, last_error=?, updated_at=?
                   WHERE event_key=?""",
                (str(error)[:2000], now, key),
            )

    def pending_events(self, limit=100):
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT event_key,platform,event_type,payload_json,status,history_id,attempts,last_error,created_at,updated_at
                   FROM event_inbox
                   WHERE status != 'done'
                   ORDER BY created_at ASC LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def prune_event_inbox(self, keep_days=30):
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        cutoff_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
        with self.lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM event_inbox WHERE status='done' AND updated_at < ?",
                (cutoff_text,),
            )

    def set_history_status(self, row_id, status, duration_sec=None):
        with self.lock, self._connect() as conn:
            if status == "playing":
                conn.execute("UPDATE history SET status=?, started_at=datetime('now') WHERE id=?", (status, row_id))
            else:
                conn.execute("UPDATE history SET status=?, finished_at=datetime('now'), duration_sec=COALESCE(?,duration_sec) WHERE id=?", (status, duration_sec, row_id))

    def prune_history(self, max_rows=50000):
        max_rows = max(1000, int(max_rows))
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                DELETE FROM history
                WHERE status NOT IN ('queued', 'playing')
                  AND id NOT IN (
                      SELECT id FROM history
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (max_rows,),
            )

    def clear_history(self):
        """Delete only terminal history rows; never remove active/queued TTS state."""
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM history WHERE status NOT IN ('queued', 'playing')"
            )
            return cur.rowcount

    def history(self, limit=100):
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT id,username,text,source,created_at,queued_at,started_at,finished_at,duration_sec,status,repeat_of,profile,external_event_id FROM history ORDER BY id DESC LIMIT ?", (max(1,min(limit,500)),)).fetchall()
            return [dict(r) for r in rows]

    def get_history(self, row_id):
        with self.lock, self._connect() as conn:
            r = conn.execute("SELECT * FROM history WHERE id=?", (row_id,)).fetchone()
            return dict(r) if r else None

    def get_setting(self, key, default=None):
        with self.lock, self._connect() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key, value):
        with self.lock, self._connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def delete_setting(self, key):
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def claim_event(self, message_id, ttl_seconds=7 * 24 * 60 * 60):
        """Atomically claim an EventSub/message id and suppress redelivery duplicates."""
        key = str(message_id or "").strip()
        if not key:
            return True
        now = time.time()
        cutoff = now - max(60, int(ttl_seconds))
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO event_dedupe(message_id, seen_at) VALUES(?, ?)",
                (key, now),
            )
            conn.execute("DELETE FROM event_dedupe WHERE seen_at < ?", (cutoff,))
            return cur.rowcount == 1

    def mark_pending_history(self, row_ids, status="cleared"):
        """Mark still-queued history rows as terminal after queue.clear()."""
        ids = [int(x) for x in row_ids]
        if not ids:
            return 0
        if status not in {"cleared", "skipped", "canceled"}:
            raise ValueError("Unsupported pending-history status")
        placeholders = ",".join("?" for _ in ids)
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE history SET status=?, finished_at=datetime('now') "
                f"WHERE id IN ({placeholders}) AND status='queued'",
                (status, *ids),
            )
            return cur.rowcount

    def save_chat_message(self, platform, message_id, broadcaster_user_id, broadcaster_login, user_id, username, text, created_at, raw_json=""):
        with self.lock, self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO chat_messages(platform,message_id,broadcaster_user_id,broadcaster_login,user_id,username,text,created_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (platform,message_id,broadcaster_user_id,broadcaster_login,user_id,username,text,created_at,raw_json)
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def rewards(self):
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT reward_id,reward_title,enabled,profile,auto_fulfill,user_input_required,prompt,updated_at FROM twitch_rewards ORDER BY reward_title COLLATE NOCASE").fetchall()
            return [dict(r) for r in rows]

    def save_reward(self, reward_id, reward_title, enabled, profile, auto_fulfill, updated_at, user_input_required=True, prompt="Введите текст для озвучки"):
        with self.lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO twitch_rewards(reward_id,reward_title,enabled,profile,auto_fulfill,user_input_required,prompt,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(reward_id) DO UPDATE SET reward_title=excluded.reward_title, enabled=excluded.enabled, profile=excluded.profile, auto_fulfill=excluded.auto_fulfill, user_input_required=excluded.user_input_required, prompt=excluded.prompt, updated_at=excluded.updated_at""",
                (reward_id,reward_title,int(enabled),profile,int(auto_fulfill),int(user_input_required),prompt,updated_at)
            )

    def get_reward(self, reward_id):
        with self.lock, self._connect() as conn:
            r = conn.execute("SELECT reward_id,reward_title,enabled,profile,auto_fulfill,user_input_required,prompt,updated_at FROM twitch_rewards WHERE reward_id=?", (reward_id,)).fetchone()
            return dict(r) if r else None
