from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

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
                profile TEXT DEFAULT 'normal'
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
            """)
            hcols = {r["name"] for r in conn.execute("PRAGMA table_info(history)").fetchall()}
            if "profile" not in hcols:
                conn.execute("ALTER TABLE history ADD COLUMN profile TEXT DEFAULT 'normal'")
            rcols = {r["name"] for r in conn.execute("PRAGMA table_info(twitch_rewards)").fetchall()}
            if "user_input_required" not in rcols:
                conn.execute("ALTER TABLE twitch_rewards ADD COLUMN user_input_required INTEGER NOT NULL DEFAULT 1")
            if "prompt" not in rcols:
                conn.execute("ALTER TABLE twitch_rewards ADD COLUMN prompt TEXT NOT NULL DEFAULT 'Введите текст для озвучки'")
            ccols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
            if "platform" not in ccols:
                conn.execute("ALTER TABLE chat_messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'twitch'")

    def add_history(self, username, text, source, created_at, repeat_of=None, profile="normal", status="queued"):
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO history(username,text,source,created_at,queued_at,status,repeat_of,profile) VALUES(?,?,?,?,datetime('now'),?,?,?)",
                (username, text, source, created_at, status, repeat_of, profile),
            )
            return int(cur.lastrowid)

    def set_history_status(self, row_id, status, duration_sec=None):
        with self.lock, self._connect() as conn:
            if status == "playing":
                conn.execute("UPDATE history SET status=?, started_at=datetime('now') WHERE id=?", (status, row_id))
            else:
                conn.execute("UPDATE history SET status=?, finished_at=datetime('now'), duration_sec=COALESCE(?,duration_sec) WHERE id=?", (status, duration_sec, row_id))

    def history(self, limit=100):
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT id,username,text,source,created_at,queued_at,started_at,finished_at,duration_sec,status,repeat_of,profile FROM history ORDER BY id DESC LIMIT ?", (max(1,min(limit,500)),)).fetchall()
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
