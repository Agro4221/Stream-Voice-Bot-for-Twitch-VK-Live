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

if not exist ".venv\Scripts\pythonw.exe" (
  echo [ERROR] Python windowless runtime not found.
  pause
  exit /b 1
)

echo Starting Stream Voice Bot...
start "" "%~dp0.venv\Scripts\pythonw.exe" -m stream_voice_bot

rem Wait until the local server is ready and open the admin page.
rem open_admin.ps1 is hidden, so the console closes immediately after the browser opens.
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\open_admin.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Admin page could not be opened automatically.
  echo Run start_bot_debug.bat to diagnose startup problems.
  pause
  exit /b 1
)

exit /b 0
