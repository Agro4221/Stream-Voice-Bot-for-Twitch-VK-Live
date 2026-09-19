from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime

_MAX_RECORDS = 500
_records: deque[dict[str, str]] = deque(maxlen=_MAX_RECORDS)
_lock = threading.RLock()
_handler: logging.Handler | None = None


class RuntimeLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            item = {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
            with _lock:
                _records.append(item)
        except Exception:
            # Logging must never be able to break application code.
            pass


def install() -> None:
    """Install the in-memory runtime log collector once per process."""
    global _handler
    with _lock:
        if _handler is not None:
            return

        handler = RuntimeLogHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))

        root = logging.getLogger()
        root.addHandler(handler)

        # Keep the project's useful INFO-level diagnostics even when uvicorn
        # itself is configured more quietly for the normal launcher.
        logging.getLogger("stream_voice_bot").setLevel(logging.INFO)
        _handler = handler


def get_logs(limit: int = 250) -> list[dict[str, str]]:
    limit = max(1, min(int(limit), _MAX_RECORDS))
    with _lock:
        return list(_records)[-limit:]


def clear() -> None:
    with _lock:
        _records.clear()
