@echo off
setlocal
set "ROOT=%~dp0"
set "TARGET=%ROOT%StreamVoiceBot.exe"

if not exist "%TARGET%" (
  echo StreamVoiceBot.exe not found.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$target=$env:TARGET; $desktop=[Environment]::GetFolderPath('Desktop'); $link=Join-Path $desktop 'Stream Voice Bot.lnk'; $shell=New-Object -ComObject WScript.Shell; $shortcut=$shell.CreateShortcut($link); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory=[IO.Path]::GetDirectoryName($target); $shortcut.IconLocation=$target + ',0'; $shortcut.Description='Stream Voice Bot'; $shortcut.Save()"

if errorlevel 1 (
  echo Failed to create desktop shortcut.
  pause
  exit /b 1
)

echo Desktop shortcut created:
echo %USERPROFILE%\Desktop\Stream Voice Bot.lnk
exit /b 0
