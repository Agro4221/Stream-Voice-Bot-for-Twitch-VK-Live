@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python environment not found. Run start_bot.bat once for first-time setup.
  pause
  exit /b 1
)

echo Starting Stream Voice Bot in diagnostic mode...
echo Admin: http://127.0.0.1:8787
echo.

rem Keep a visible console for diagnostics while opening the admin page separately.
start "Stream Voice Bot - Debug" /min cmd /k ""%~dp0.venv\Scripts\python.exe" -m stream_voice_bot"

powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\open_admin.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Admin page could not be opened automatically.
)
pause
