from __future__ import annotations

import ctypes
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from threading import Thread


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = runtime_root()
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
BOOT_LOG = DATA_DIR / "exe_startup.log"


def boot_log(message: str) -> None:
    try:
        with BOOT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


# PyInstaller windowed apps may have no console, so stdout/stderr can be None.
# Uvicorn's default formatter expects a usable stream; give it harmless sinks
# before importing Uvicorn or code that may configure logging.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def show_error(message: str) -> None:
    boot_log(message)
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "Stream Voice Bot", 0x10)
    except Exception:
        pass


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import uvicorn

    from stream_voice_bot.app import create_app
except Exception:
    details = traceback.format_exc()
    boot_log("Import failure:\n" + details)
    show_error(
        "Stream Voice Bot не смог загрузить приложение.\n\n"
        f"Подробности записаны в:\n{BOOT_LOG}"
    )
    raise


URL = "http://127.0.0.1:8787/"
TIMEOUT_SECONDS = 120


def wait_for_admin_and_open() -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=2) as response:
                if response.status == 200:
                    boot_log(f"Admin ready: {URL}")
                    webbrowser.open(URL)
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    boot_log("Admin did not become ready within timeout.")


try:
    boot_log(f"Starting application from root: {ROOT}")
    app = create_app(ROOT)
    boot_log("create_app completed successfully.")
except Exception:
    details = traceback.format_exc()
    boot_log("create_app failure:\n" + details)
    show_error(
        "Stream Voice Bot завершился при инициализации.\n\n"
        f"Подробности записаны в:\n{BOOT_LOG}"
    )
    raise


if __name__ == "__main__":
    Thread(target=wait_for_admin_and_open, daemon=True).start()

    try:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=8787,
            log_level="warning",
            access_log=False,
            log_config=None,
            use_colors=False,
        )
        server = uvicorn.Server(config)
        app.state.server = server
        boot_log("Starting Uvicorn server on 127.0.0.1:8787.")
        server.run()
    except Exception:
        details = traceback.format_exc()
        boot_log("Uvicorn failure:\n" + details)
        show_error(
            "Stream Voice Bot не смог запустить локальный сервер.\n\n"
            f"Подробности записаны в:\n{BOOT_LOG}"
        )
        raise
