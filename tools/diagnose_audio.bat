@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run start_bot.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sounddevice as sd; print(sd.query_devices()); print(); print('Default output:', sd.query_devices(kind='output'))"
pause
