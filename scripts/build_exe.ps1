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

if (-not (Test-Path $VenvPython -PathType Leaf)) {
    throw "Python virtual environment is missing. Run scripts\install_windows.ps1 first."
}
if (-not (Test-Path $Model -PathType Leaf) -or ((Get-Item $Model).Length -lt 1MB)) {
    throw "models\v5_ru.pt is missing. Run scripts\install_windows.ps1 first."
}
$PrivateNodeExe = Join-Path $NodeRuntime "node.exe"
if (-not (Test-Path $PrivateNodeExe -PathType Leaf)) {
    # The installer may reuse an existing system Node.js. For a portable EXE
    # bundle we still need a local node.exe so the target PC needs no Node install.
    $systemNode = Get-Command node -ErrorAction SilentlyContinue
    if ($systemNode -and (Test-Path $systemNode.Source -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path $NodeRuntime | Out-Null
        Copy-Item $systemNode.Source $PrivateNodeExe -Force
        Write-Host "Copied system node.exe into .runtime\node for the portable bundle." -ForegroundColor Yellow
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

Write-Host "=== Stream Voice Bot v$Version EXE build ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"

Write-Host "Checking source startup..." -ForegroundColor Cyan
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

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $VenvPython -m pip install --disable-pip-version-check --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller installation failed."
}

$BuildRoot = Join-Path $ProjectRoot "build\exe"
$DistRoot = Join-Path $ProjectRoot $OutputDirectory
$CoreWork = Join-Path $BuildRoot "core-work"
$LauncherWork = Join-Path $BuildRoot "launcher-work"
$Stage = Join-Path $BuildRoot ("StreamVoiceBot_" + $Version)

foreach ($path in @($CoreWork, $LauncherWork, $Stage)) {
    if (Test-Path $path) {
        Remove-Item $path -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $BuildRoot, $DistRoot, $Stage | Out-Null

$CoreDist = Join-Path $DistRoot "StreamVoiceBotCore"
if (Test-Path $CoreDist) {
    Remove-Item $CoreDist -Recurse -Force
}

$LauncherExe = Join-Path $DistRoot "StreamVoiceBot.exe"
if (Test-Path $LauncherExe) {
    Remove-Item $LauncherExe -Force
}

$Spec = Join-Path $ProjectRoot "packaging\StreamVoiceBotCore.spec"

Write-Host "Building StreamVoiceBotCore.exe..." -ForegroundColor Cyan
$coreArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", $DistRoot,
    "--workpath", $CoreWork,
    $Spec
)
& $VenvPython @coreArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller core build failed."
}

Write-Host "Building user-facing StreamVoiceBot.exe launcher..." -ForegroundColor Cyan
$launcherArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "StreamVoiceBot",
    "--distpath", $DistRoot,
    "--workpath", $LauncherWork,
    (Join-Path $ProjectRoot "scripts\exe_launcher.py")
)
& $VenvPython @launcherArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller launcher build failed."
}

if (-not (Test-Path (Join-Path $CoreDist "StreamVoiceBotCore.exe") -PathType Leaf)) {
    throw "Core EXE was not produced."
}
if (-not (Test-Path $LauncherExe -PathType Leaf)) {
    throw "Launcher EXE was not produced."
}

Write-Host "Staging portable EXE bundle..." -ForegroundColor Cyan
Copy-Item $CoreDist (Join-Path $Stage "StreamVoiceBotCore") -Recurse -Force
Copy-Item $LauncherExe (Join-Path $Stage "StreamVoiceBot.exe") -Force
Copy-Item (Join-Path $ProjectRoot "VERSION") (Join-Path $Stage "VERSION") -Force

$StageWeb = Join-Path $Stage "stream_voice_bot\web"
New-Item -ItemType Directory -Force -Path $StageWeb | Out-Null
Copy-Item (Join-Path $Web "*") $StageWeb -Recurse -Force

$StageBridge = Join-Path $Stage "stream_voice_bot\vk_bridge"
New-Item -ItemType Directory -Force -Path $StageBridge | Out-Null
Copy-Item $Bridge (Join-Path $StageBridge "bridge.js") -Force

$StageModelDir = Join-Path $Stage "models"
New-Item -ItemType Directory -Force -Path $StageModelDir | Out-Null
Copy-Item $Model (Join-Path $StageModelDir "v5_ru.pt") -Force

$Readme = Join-Path $Stage "README_EXE.txt"
@"
Stream Voice Bot v$Version

Запуск:
1. Запусти StreamVoiceBot.exe.
2. Программа сама поднимет локальный сервер и откроет админку:
   http://127.0.0.1:8787/

В комплекте:
- StreamVoiceBot.exe — пользовательский запускатор.
- StreamVoiceBotCore\ — основное приложение и его Python/runtime-зависимости.
- models\v5_ru.pt — модель Silero TTS.
- stream_voice_bot\web\ — админка.

Примечания:
- STT-модель faster-whisper загружается в локальный .cache при первом запуске STT.
- Пакеты Argos Translate для дополнительных языков загружаются по запросу.
- Настройки, база и лог запуска находятся в data\.
- Для VK bridge используется встроенный Node.js runtime внутри StreamVoiceBotCore.
"@ | Set-Content -LiteralPath $Readme -Encoding UTF8

$FinalStage = Join-Path $DistRoot ("StreamVoiceBot_{0}" -f $Version)
if (Test-Path $FinalStage) {
    Remove-Item $FinalStage -Recurse -Force
}
Copy-Item $Stage $FinalStage -Recurse -Force

$Zip = Join-Path $DistRoot ("StreamVoiceBot_{0}_win64.zip" -f $Version)
if (Test-Path $Zip) {
    Remove-Item $Zip -Force
}

# Windows PowerShell 5 Compress-Archive can fail on large ML bundles.
# ZIP is convenient but is not required for the EXE itself.
$sevenZip = @(
    (Join-Path $env:ProgramFiles "7-Zip\7z.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "7-Zip\7z.exe"),
    (Join-Path $ProjectRoot "7z.exe")
) | Where-Object { $_ -and (Test-Path $_ -PathType Leaf) } | Select-Object -First 1

if ($sevenZip) {
    Write-Host "Creating ZIP with 7-Zip..." -ForegroundColor Cyan
    & $sevenZip a -tzip -mx=5 $Zip (Join-Path $FinalStage "*") | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $Zip -Force -ErrorAction SilentlyContinue
        Write-Warning "7-Zip could not create the ZIP. The EXE bundle folder is still valid."
    }
} else {
    Write-Warning "7-Zip was not found. Skipping ZIP creation because Windows PowerShell Compress-Archive can fail on large ML bundles."
}

Write-Host ""
Write-Host "EXE bundle created:" -ForegroundColor Green
Write-Host "  Folder: $FinalStage"
Write-Host "  Start:  $FinalStage\StreamVoiceBot.exe"
if (Test-Path $Zip) {
    Write-Host "  ZIP:    $Zip"
}
