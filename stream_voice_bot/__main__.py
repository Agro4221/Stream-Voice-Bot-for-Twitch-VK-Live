from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn

from .app import create_app

ROOT = Path(__file__).resolve().parent.parent
app = create_app(ROOT)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8787,
        log_level="info",
    )
