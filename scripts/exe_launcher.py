from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


URL = "http://127.0.0.1:8787/"
TIMEOUT_SECONDS = 120
CORE_EXE = Path("StreamVoiceBotCore") / "StreamVoiceBotCore.exe"
CREATE_NO_WINDOW = 0x08000000


def log(root: Path, message: str) -> None:
    log_dir = root / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "launcher.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def is_ready() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def show_error(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "Stream Voice Bot", 0x10)
    except Exception:
        pass


def main() -> int:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent
    core = root / CORE_EXE

    if is_ready():
        webbrowser.open(URL)
        log(root, "Admin was already ready; browser opened.")
        return 0

    if not core.is_file():
        message = f"Не найден файл приложения:\n{core}"
        log(root, message)
        show_error(message)
        return 1

    try:
        process = subprocess.Popen(
            [str(core)],
            cwd=str(core.parent),
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
        log(root, f"Core started with PID={process.pid}.")
    except OSError as exc:
        message = f"Не удалось запустить StreamVoiceBotCore.exe:\n{exc}"
        log(root, message)
        show_error(message)
        return 1

    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_ready():
            webbrowser.open(URL)
            log(root, "Admin became ready; browser opened.")
            return 0
        if process.poll() is not None:
            message = (
                "StreamVoiceBotCore.exe завершился до запуска админки.\n"
                f"См. лог: {root / 'data' / 'launcher.log'}"
            )
            log(root, f"Core exited before HTTP ready. code={process.returncode}")
            show_error(message)
            return 1
        time.sleep(0.5)

    message = (
        "Админка не открылась за 120 секунд.\n"
        f"См. лог: {root / 'data' / 'launcher.log'}"
    )
    log(root, "Admin did not become ready within 120 seconds.")
    show_error(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
