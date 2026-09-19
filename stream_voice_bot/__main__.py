from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app

ROOT = Path(__file__).resolve().parent.parent
# Keep large runtime/model caches beside the project, regardless of whether the
# project lives on C:, D:, E: or another local drive.
(ROOT / ".cache" / "pip").mkdir(parents=True, exist_ok=True)
(ROOT / ".cache" / "huggingface").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PIP_CACHE_DIR", str(ROOT / ".cache" / "pip"))
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(ROOT / ".cache" / "huggingface" / "hub"))
app = create_app(ROOT)

def _open_admin_when_ready(host: str, port: int, url: str, timeout: float = 120.0) -> None:
    """Open the local admin page only after the HTTP port accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                webbrowser.open(url, new=2)
                return
        except OSError:
            time.sleep(0.5)


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
    threading.Thread(
        target=_open_admin_when_ready,
        args=("127.0.0.1", 8787, "http://127.0.0.1:8787/"),
        name="open-admin",
        daemon=True,
    ).start()
    server.run()
