@echo off
setlocal
cd /d "%~dp0"
start "Stream Voice Bot" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_windows.ps1"
start "" powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\open_admin.ps1"
