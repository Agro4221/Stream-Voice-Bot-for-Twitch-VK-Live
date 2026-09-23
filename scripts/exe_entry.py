from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
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
# PyInstaller 6.22+ stores onedir runtime files under `_internal` and
# exposes that directory through sys._MEIPASS. Keep writable user data
# beside the EXE so the visible bundle stays clean and portable.
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT)) if getattr(sys, "frozen", False) else ROOT
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


def create_desktop_shortcut_once() -> None:
    """Create the app's desktop shortcut on the first EXE launch only."""
    if not getattr(sys, "frozen", False):
        return

    target = Path(sys.executable).resolve()
    marker = DATA_DIR / ".desktop_shortcut_created"
    try:
        if marker.is_file():
            saved_target = marker.read_text(encoding="utf-8").strip()
            if saved_target.casefold() == str(target).casefold():
                return
    except OSError:
        pass

    env = os.environ.copy()
    env["STREAMVOICEBOT_SHORTCUT_TARGET"] = str(target)
    powershell = shutil.which("powershell.exe") or "powershell.exe"
    script = r'''
$ErrorActionPreference = 'Stop'
$target = $env:STREAMVOICEBOT_SHORTCUT_TARGET
if ([string]::IsNullOrWhiteSpace($target) -or -not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Stream Voice Bot executable was not found: $target"
}
$desktop = [Environment]::GetFolderPath('Desktop')
if ([string]::IsNullOrWhiteSpace($desktop)) {
    throw 'Windows Desktop folder could not be resolved.'
}
$link = Join-Path $desktop 'Stream Voice Bot.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
$shortcut.IconLocation = "$target,0"
$shortcut.Description = 'Stream Voice Bot'
$shortcut.Save()
'''

    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            boot_log("Desktop shortcut creation failed" + (f": {detail}" if detail else "."))
            return

        marker.write_text(str(target), encoding="utf-8")
        boot_log("Desktop shortcut created: Stream Voice Bot.lnk")
    except Exception as exc:
        boot_log(f"Desktop shortcut creation skipped: {type(exc).__name__}: {exc}")


if str(RESOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(RESOURCE_ROOT))

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
    boot_log(f"Starting application from resource root: {RESOURCE_ROOT}")
    boot_log(f"Writable data root: {ROOT}")
    app = create_app(RESOURCE_ROOT, data_root=ROOT)
    boot_log("create_app completed successfully.")
    create_desktop_shortcut_once()
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
