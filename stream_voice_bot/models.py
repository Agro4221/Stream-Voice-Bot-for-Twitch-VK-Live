from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class QueueItem:
    text: str
    username: str = "test"
    source: str = "admin"
    repeat_of: Optional[int] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=utc_now)
    chars: int = 0

    def __post_init__(self) -> None:
        self.chars = len(self.text)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "username": self.username,
            "source": self.source,
            "repeat_of": self.repeat_of,
            "created_at": self.created_at,
            "chars": self.chars,
        }
