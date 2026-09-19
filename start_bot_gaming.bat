@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Gaming/streaming profile:
rem - Silero/PyTorch uses one CPU thread by default.
rem - The Python process is moved to Windows BelowNormal priority.
set "SVB_TORCH_THREADS=1"

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
  echo    Stream Voice Bot - gaming/streaming setup
  echo ===============================================
  echo.
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_windows.ps1"
  if errorlevel 1 (
    echo.
    echo [ERROR] Installation/setup failed.
    pause
    exit /b 1
  )
)

set "PYTHON_EXE=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python windowless runtime not found.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command ^
  "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList '-m','stream_voice_bot' -WorkingDirectory '%~dp0' -PassThru; Start-Sleep -Milliseconds 700; try { $p.PriorityClass = 'BelowNormal' } catch {}; Write-Output $p.Id | Set-Content -Path '%~dp0stream_voice_bot_gaming.pid' -Encoding ASCII"

powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\open_admin.ps1"

endlocal
exit /b 0
