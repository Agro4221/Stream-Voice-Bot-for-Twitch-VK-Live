[CmdletBinding()]
param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($env:OS -ne "Windows_NT") {
    throw "This build script must run on Windows."
}

$Version = (Get-Content -Raw (Join-Path $ProjectRoot "VERSION")).Trim()
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Model = Join-Path $ProjectRoot "models\v5_ru.pt"
$NodeRuntime = Join-Path $ProjectRoot ".runtime\node"
$Bridge = Join-Path $ProjectRoot "stream_voice_bot\vk_bridge\bridge.js"
$Web = Join-Path $ProjectRoot "stream_voice_bot\web"
$Spec = Join-Path $ProjectRoot "packaging\StreamVoiceBot.spec"

if (-not (Test-Path $VenvPython -PathType Leaf)) {
    throw "Python virtual environment is missing. Run scripts\install_windows.ps1 first."
}
if (-not (Test-Path $Model -PathType Leaf) -or ((Get-Item $Model).Length -lt 1MB)) {
    throw "models\v5_ru.pt is missing. Run scripts\install_windows.ps1 first."
}
if (-not (Test-Path (Join-Path $NodeRuntime "node.exe") -PathType Leaf)) {
    $systemNode = Get-Command node -ErrorAction SilentlyContinue
    if ($systemNode -and (Test-Path $systemNode.Source -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path $NodeRuntime | Out-Null
        Copy-Item $systemNode.Source (Join-Path $NodeRuntime "node.exe") -Force
    } else {
        throw "Node.js runtime is missing. Install Node.js or run scripts\install_windows.ps1 again."
    }
}
if (-not (Test-Path $Bridge -PathType Leaf)) {
    throw "VK bridge is missing: $Bridge"
}
if (-not (Test-Path (Join-Path $Web "index.html") -PathType Leaf)) {
    throw "Admin web files are missing: $Web"
}
if (-not (Test-Path $Spec -PathType Leaf)) {
    throw "PyInstaller spec is missing: $Spec"
}

Write-Host "=== Stream Voice Bot v$Version Windows EXE build ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"

Write-Host "Checking application startup..." -ForegroundColor Cyan
& $VenvPython -c "from pathlib import Path; from stream_voice_bot.app import create_app; a=create_app(Path.cwd()); a.state.tts_queue.shutdown(); print('Build preflight: OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Application preflight failed."
}

Write-Host "Running Python tests..." -ForegroundColor Cyan
& $VenvPython -m unittest discover -s tests -p "test_*.py" -v
if ($LASTEXITCODE -ne 0) {
    throw "Python tests failed."
}

Write-Host "Checking VK bridge..." -ForegroundColor Cyan
$NodeExe = Join-Path $NodeRuntime "node.exe"
& $NodeExe --check $Bridge
if ($LASTEXITCODE -ne 0) {
    throw "VK bridge syntax check failed."
}

Write-Host "Checking PyInstaller..." -ForegroundColor Cyan
& $VenvPython -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
    & $VenvPython -m pip install --disable-pip-version-check --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller installation failed."
    }
}

$BuildRoot = Join-Path $ProjectRoot "build\exe"
$DistRoot = Join-Path $ProjectRoot $OutputDirectory
$WorkRoot = Join-Path $BuildRoot "work"
$BuiltBundle = Join-Path $DistRoot "StreamVoiceBot"
$FinalStage = Join-Path $DistRoot ("StreamVoiceBot_{0}" -f $Version)

if (Test-Path $WorkRoot) {
    Remove-Item $WorkRoot -Recurse -Force
}
if (Test-Path $BuiltBundle) {
    Remove-Item $BuiltBundle -Recurse -Force
}
if (Test-Path $FinalStage) {
    Remove-Item $FinalStage -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $BuildRoot, $DistRoot | Out-Null

Write-Host "Building the single user-facing onedir application..." -ForegroundColor Cyan
$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", $DistRoot,
    "--workpath", $WorkRoot,
    $Spec
)
& $VenvPython @pyiArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$BuiltExe = Join-Path $BuiltBundle "StreamVoiceBot.exe"
if (-not (Test-Path $BuiltExe -PathType Leaf)) {
    throw "StreamVoiceBot.exe was not produced."
}

Write-Host "Preparing versioned portable folder..." -ForegroundColor Cyan
Copy-Item $BuiltBundle $FinalStage -Recurse -Force

$Readme = Join-Path $FinalStage "README_EXE.txt"
@"
Stream Voice Bot v$Version

Запуск:
1. Запусти StreamVoiceBot.exe.
2. Админка автоматически откроется после запуска локального сервера:
   http://127.0.0.1:8787/

Важно:
- Распространяй всю папку StreamVoiceBot_$Version целиком.
- Не запускай только StreamVoiceBot.exe без соседних файлов.
- Настройки и база создаются в папке data рядом с этой папкой.
- STT-модель faster-whisper скачивается в .cache\huggingface при первом запуске STT.
- Argos Translate скачивает дополнительные языковые пакеты по запросу.
"@ | Set-Content -LiteralPath $Readme -Encoding UTF8

Write-Host ""
Write-Host "BUILD SUCCESS" -ForegroundColor Green
Write-Host "EXE bundle: $FinalStage"
Write-Host "Launcher:    $FinalStage\StreamVoiceBot.exe"
Write-Host ""
Write-Host "For distribution, copy the whole folder above."
Write-Host "A ZIP is intentionally not created here because this ML bundle can exceed the limits of Windows PowerShell Compress-Archive."
