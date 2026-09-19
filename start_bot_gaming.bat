@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Gaming/streaming profile: keep Silero TTS below normal Windows priority
rem and restrict PyTorch to one CPU thread by default.
set "SVB_TORCH_THREADS=1"

set "PYTHON_EXE=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python runtime not found. Run start_bot.bat once to finish setup.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command ^
  "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList '-m','stream_voice_bot' -WorkingDirectory '%~dp0' -PassThru; Start-Sleep -Milliseconds 700; try { $p.PriorityClass = 'BelowNormal' } catch {}; Write-Output $p.Id | Set-Content -Path '%~dp0stream_voice_bot_gaming.pid' -Encoding ASCII"

powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\open_admin.ps1"

endlocal
exit /b 0
