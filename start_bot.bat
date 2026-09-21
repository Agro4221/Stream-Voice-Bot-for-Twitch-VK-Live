@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "NEED_SETUP=0"
if not exist ".venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "models\v5_ru.pt" set "NEED_SETUP=1"
if exist "models\v5_ru.pt" (
  for %%F in ("models\v5_ru.pt") do if %%~zF LSS 1048576 set "NEED_SETUP=1"
)
if not exist "stream_voice_bot\vk_bridge\node_modules\vklive-message-client" set "NEED_SETUP=1"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import argostranslate" >nul 2>&1 || set "NEED_SETUP=1"
  ".venv\Scripts\python.exe" -c "import sounddevice as sd, inspect; p=inspect.signature(sd.WasapiSettings).parameters; raise SystemExit(0 if 'auto_convert' in p else 1)" >nul 2>&1 || set "NEED_SETUP=1"
)

if "%NEED_SETUP%"=="1" (
  echo.
  echo ===============================================
  echo      Stream Voice Bot - first run setup
  echo ===============================================
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_windows.ps1"
  if errorlevel 1 (
    echo.
    echo [ERROR] Installation/setup failed.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python runtime not found.
  pause
  exit /b 1
)

echo Checking Python application startup...
".venv\Scripts\python.exe" -c "from pathlib import Path; from stream_voice_bot.app import create_app; a=create_app(Path.cwd()); a.state.tts_queue.shutdown(); print('Startup preflight: OK')"
if errorlevel 1 (
  echo.
  echo [ERROR] Stream Voice Bot preflight failed.
  echo The project was not started.
  pause
  exit /b 1
)

echo Starting Stream Voice Bot...
echo Opening admin: http://127.0.0.1:8787

rem Hand the bot startup and admin readiness check to one hidden PowerShell process.
rem This BAT then exits immediately, so no terminal stays on the taskbar.
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\launch_bot.ps1"

exit /b 0
