from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # The user-facing launcher keeps the frozen core in a child directory.
        # Prefer the bundle root when its runtime files are present.
        if (exe_dir / "VERSION").is_file() and (exe_dir / "stream_voice_bot" / "web").is_dir():
            return exe_dir
        if (
            exe_dir.name == "StreamVoiceBotCore"
            and (exe_dir.parent / "VERSION").is_file()
            and (exe_dir.parent / "stream_voice_bot" / "web").is_dir()
        ):
            return exe_dir.parent
        return exe_dir
    return Path(__file__).resolve().parent.parent


ROOT = runtime_root()
# Keep large runtime/model caches beside the project, regardless of whether the
# project lives on C:, D:, E: or another local drive.
(ROOT / ".cache" / "pip").mkdir(parents=True, exist_ok=True)
(ROOT / ".cache" / "huggingface").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PIP_CACHE_DIR", str(ROOT / ".cache" / "pip"))
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(ROOT / ".cache" / "huggingface" / "hub"))
app = create_app(ROOT)

if __name__ == "__main__":
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8787,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    app.state.server = server
    server.run()
