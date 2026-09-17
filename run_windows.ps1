$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Virtual environment not found. Running first-time setup..." -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\install_windows.ps1")
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

Write-Host "Starting Stream Voice Bot..." -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host "URL: http://127.0.0.1:8787"
& $venvPython -m stream_voice_bot
