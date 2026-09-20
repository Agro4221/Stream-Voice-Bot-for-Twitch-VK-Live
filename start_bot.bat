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
  echo [ERROR] Stream Voice Bot could not start.
  echo Use start_bot_debug.bat for the full traceback.
  pause
  exit /b 1
)

echo Starting Stream Voice Bot...
if not exist ".venv\Scripts\pythonw.exe" (
  echo [ERROR] Python windowless runtime not found.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m stream_voice_bot

rem Wait until the local server is ready and open the admin page.
rem open_admin.ps1 is hidden during the normal launch.
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\open_admin.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Admin page did not become available within 120 seconds.
  echo Run start_bot_debug.bat to diagnose startup problems.
  pause
  exit /b 1
)

exit /b 0
