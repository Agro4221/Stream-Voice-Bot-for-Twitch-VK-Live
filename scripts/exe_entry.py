from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from threading import Thread

import uvicorn

from stream_voice_bot.app import create_app


URL = "http://127.0.0.1:8787/"
TIMEOUT_SECONDS = 120


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = runtime_root()


def wait_for_admin_and_open() -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(URL)
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)


app = create_app(ROOT)


if __name__ == "__main__":
    Thread(target=wait_for_admin_and_open, daemon=True).start()

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
