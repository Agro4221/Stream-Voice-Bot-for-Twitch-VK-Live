@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "NEED_SETUP=0"
if not exist ".venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "models\v5_ru.pt" set "NEED_SETUP=1"
if not exist "stream_voice_bot\vk_bridge\node_modules\vklive-message-client" set "NEED_SETUP=1"

if "%NEED_SETUP%"=="1" (
  echo.
  echo ===============================================
  echo        Stream Voice Bot - first run setup
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

echo.
echo Starting Stream Voice Bot...
echo Open http://127.0.0.1:8787
echo.
"%~dp0.venv\Scripts\python.exe" -m stream_voice_bot
if errorlevel 1 (
  echo.
  echo [ERROR] Stream Voice Bot stopped with an error.
)
pause
