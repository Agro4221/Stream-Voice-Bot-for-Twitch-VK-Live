[CmdletBinding()]
param(
    [string]$OutputDirectory = "dist_release"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($env:OS -ne "Windows_NT") {
    throw "This build script must run on Windows."
}

$Version = (Get-Content -Raw (Join-Path $ProjectRoot "VERSION")).Trim()
$DevPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BuildVenv = Join-Path $ProjectRoot ".build_venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$Model = Join-Path $ProjectRoot "models\v5_ru.pt"
$NodeRuntime = Join-Path $ProjectRoot ".runtime\node"
$NodeExe = Join-Path $NodeRuntime "node.exe"
$NodeNpm = Join-Path $NodeRuntime "npm.cmd"
$BridgeDir = Join-Path $ProjectRoot "stream_voice_bot\vk_bridge"
$Bridge = Join-Path $BridgeDir "bridge.js"
$BridgeLock = Join-Path $BridgeDir "package-lock.json"
$Web = Join-Path $ProjectRoot "stream_voice_bot\web"
$Spec = Join-Path $ProjectRoot "packaging\StreamVoiceBot.spec"
$IconPng = Join-Path $ProjectRoot "packaging\content-factory-favicon.png"
$IconIco = Join-Path $ProjectRoot "packaging\StreamVoiceBot.ico"

if (-not (Test-Path $Model -PathType Leaf) -or ((Get-Item $Model).Length -lt 1MB)) {
    throw "models\v5_ru.pt is missing. Run scripts\install_windows.ps1 first."
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

function Find-BootstrapPython {
    if (Test-Path $DevPython -PathType Leaf) {
        return $DevPython
    }

    foreach ($candidate in @("py", "python", "python3")) {
        try {
            $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
            if (-not $cmd) { continue }

            if ($candidate -eq "py") {
                & $cmd.Source -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    return "$($cmd.Source) -3.11"
                }
            } else {
                & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    return $cmd.Source
                }
            }
        } catch {}
    }

    throw "No compatible Python 3.11-3.13 bootstrap was found. Run scripts\install_windows.ps1 first."
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$FailureMessage
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Invoke-BootstrapPython {
    param(
        [string]$PythonCommand,
        [string[]]$Arguments,
        [string]$FailureMessage
    )

    if ($PythonCommand -match " -3\.11$") {
        & py -3.11 @Arguments
    } else {
        & $PythonCommand @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function New-CpuBuildVenv {
    $bootstrapPython = Find-BootstrapPython

    if (Test-Path $BuildVenv) {
        $existingTorch = ""
        if (Test-Path $BuildPython -PathType Leaf) {
            try {
                $existingTorch = (& $BuildPython -c "import torch; print(torch.__version__)").Trim()
            } catch {
                $existingTorch = ""
            }
        }

        if ($existingTorch -notmatch "\+cpu$") {
            Write-Host "Build venv is not CPU-only. Recreating it safely..." -ForegroundColor Yellow
            Remove-Item $BuildVenv -Recurse -Force
        }
    }

    if (-not (Test-Path $BuildPython -PathType Leaf)) {
        Write-Host "Creating isolated CPU-only build environment..." -ForegroundColor Cyan
        Invoke-BootstrapPython $bootstrapPython @("-m", "venv", $BuildVenv) "Could not create .build_venv."
    }

    Write-Host "Bootstrapping CPU build environment..." -ForegroundColor Cyan
    Invoke-Checked $BuildPython @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "--upgrade", "pip", "setuptools", "wheel"
    ) "Build environment bootstrap failed."

    Write-Host "Installing CPU-only PyTorch for the portable TTS bundle..." -ForegroundColor Cyan
    Invoke-Checked $BuildPython @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "--upgrade",
        "--index-url", "https://download.pytorch.org/whl/cpu",
        "torch==2.10.0+cpu"
    ) "CPU-only PyTorch installation failed."

    Write-Host "Installing application dependencies into the build environment..." -ForegroundColor Cyan
    Invoke-Checked $BuildPython @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "-r", (Join-Path $ProjectRoot "requirements.txt")
    ) "Application dependency installation failed."

    Write-Host "Installing the pinned PyInstaller version..." -ForegroundColor Cyan
    Invoke-Checked $BuildPython @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "pyinstaller==6.22.3"
    ) "PyInstaller installation failed."

    $torchCheck = (& $BuildPython -c "import torch; print(torch.__version__)").Trim()
    if ($torchCheck -notmatch "\+cpu$") {
        throw "Build environment is not CPU-only (torch=$torchCheck). Refusing to create a GPU-bloated release."
    }

    Write-Host "Build PyTorch: $torchCheck" -ForegroundColor Green
}

function New-IcoFromPng {
    param(
        [string]$PngPath,
        [string]$IcoPath
    )

    $png = [System.IO.File]::ReadAllBytes($PngPath)
    if ($png.Length -lt 32) {
        throw "Icon source is too small: $PngPath"
    }

    $ico = New-Object byte[] (22 + $png.Length)
    [Array]::Clear($ico, 0, $ico.Length)
    [BitConverter]::GetBytes([UInt16]0).CopyTo($ico, 0)
    [BitConverter]::GetBytes([UInt16]1).CopyTo($ico, 2)
    [BitConverter]::GetBytes([UInt16]1).CopyTo($ico, 4)
    $ico[6] = 64
    $ico[7] = 64
    $ico[8] = 0
    $ico[9] = 0
    [BitConverter]::GetBytes([UInt16]1).CopyTo($ico, 10)
    [BitConverter]::GetBytes([UInt16]32).CopyTo($ico, 12)
    [BitConverter]::GetBytes([UInt32]$png.Length).CopyTo($ico, 14)
    [BitConverter]::GetBytes([UInt32]22).CopyTo($ico, 18)
    [Array]::Copy($png, 0, $ico, 22, $png.Length)
    [System.IO.File]::WriteAllBytes($IcoPath, $ico)
}

function Ensure-NodeRuntime {
    if (-not (Test-Path $NodeExe -PathType Leaf)) {
        $systemNode = Get-Command node -ErrorAction SilentlyContinue
        if (-not $systemNode -or -not (Test-Path $systemNode.Source -PathType Leaf)) {
            throw "Node.js runtime is missing. Install Node.js or run scripts\install_windows.ps1 first."
        }
        New-Item -ItemType Directory -Force -Path $NodeRuntime | Out-Null
        Copy-Item $systemNode.Source $NodeExe -Force
    }

    $env:Path = "$NodeRuntime;$env:Path"

    if (Test-Path $NodeNpm -PathType Leaf) {
        $script:NpmCommand = $NodeNpm
    } else {
        $systemNpm = Get-Command npm -ErrorAction SilentlyContinue
        if (-not $systemNpm -or -not (Test-Path $systemNpm.Source -PathType Leaf)) {
            throw "npm is missing. Install Node.js/npm or run scripts\install_windows.ps1 first."
        }
        $script:NpmCommand = $systemNpm.Source
    }

    if (-not (Test-Path $BridgeLock -PathType Leaf)) {
        throw "VK bridge package-lock.json is missing: $BridgeLock"
    }

    Write-Host "Installing VK bridge runtime dependencies..." -ForegroundColor Cyan
    Push-Location $BridgeDir
    try {
        Invoke-Checked $script:NpmCommand @("ci", "--omit=dev", "--no-audit", "--no-fund") "VK bridge dependencies failed to install."
    } finally {
        Pop-Location
    }
}

Write-Host "=== Stream Voice Bot v$Version Windows EXE build ===" -ForegroundColor Cyan
if (-not (Test-Path $IconPng -PathType Leaf)) { throw "Icon source is missing: $IconPng" }
New-IcoFromPng $IconPng $IconIco
Write-Host "Application icon prepared from Content Factory favicon." -ForegroundColor Green
Write-Host "Project root: $ProjectRoot"
Write-Host "Release output: $OutputDirectory"
Write-Host "The existing dist/ folder will not be touched."

New-CpuBuildVenv
Ensure-NodeRuntime

Write-Host "Checking application startup in the exact CPU build environment..." -ForegroundColor Cyan
Invoke-Checked $BuildPython @(
    "-c",
    "from pathlib import Path; from stream_voice_bot.app import create_app; a=create_app(Path.cwd()); a.state.tts_queue.shutdown(); print('Build preflight: OK')"
) "Application preflight failed."

Write-Host "Running Python tests..." -ForegroundColor Cyan
Invoke-Checked $BuildPython @(
    "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"
) "Python tests failed."

Write-Host "Checking VK bridge..." -ForegroundColor Cyan
Invoke-Checked $NodeExe @("--check", $Bridge) "VK bridge syntax check failed."

Write-Host "Checking PyInstaller..." -ForegroundColor Cyan
Invoke-Checked $BuildPython @("-m", "PyInstaller", "--version") "PyInstaller is unavailable."

$BuildRoot = Join-Path $ProjectRoot "build\exe-release"
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
Invoke-Checked $BuildPython @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", $DistRoot,
    "--workpath", $WorkRoot,
    $Spec
) "PyInstaller build failed."

$BuiltExe = Join-Path $BuiltBundle "StreamVoiceBot.exe"
if (-not (Test-Path $BuiltExe -PathType Leaf)) {
    throw "StreamVoiceBot.exe was not produced."
}

Sign-BuiltExe $BuiltExe

Write-Host "Preparing isolated versioned release folder..." -ForegroundColor Cyan
Copy-Item $BuiltBundle $FinalStage -Recurse -Force
Copy-Item (Join-Path $ProjectRoot "scripts\Create_Desktop_Shortcut.cmd") $FinalStage -Force

$Readme = Join-Path $FinalStage "README_EXE.txt"
@"
Stream Voice Bot v$Version

Запуск:
1. Запусти StreamVoiceBot.exe.
2. Админка автоматически откроется:
   http://127.0.0.1:8787/

Ярлык:
- Запусти Create_Desktop_Shortcut.cmd из этой папки.
- На рабочем столе появится "Stream Voice Bot.lnk" с иконкой приложения.

SmartScreen:
- Без доверенной цифровой подписи Windows может показать предупреждение "Windows защитила ваш компьютер".
- Для публичного распространения используй сертификат Authenticode от доверенного удостоверяющего центра.
- Подпись помогает формировать репутацию издателя и последующих версий; для нового файла мгновенного обхода SmartScreen нет.

Важно:
- Распространяй всю папку StreamVoiceBot_$Version целиком.
- Не запускай только StreamVoiceBot.exe без соседних файлов.
- Настройки и база создаются в папке data рядом с этой папкой.
- Silero v5_ru.pt уже входит в пакет.
- STT large-v3-turbo загружается в пользовательский Hugging Face cache при первом запуске STT.
- Argos Translate скачивает дополнительные языковые пакеты по запросу.
- Twitch access/refresh tokens не входят в пакет; авторизация выполняется отдельно на каждом ПК.
"@ | Set-Content -LiteralPath $Readme -Encoding UTF8

$RequiredFiles = @(
    (Join-Path $FinalStage "StreamVoiceBot.exe"),
    (Join-Path $FinalStage "models\v5_ru.pt"),
    (Join-Path $FinalStage "stream_voice_bot\web\index.html"),
    (Join-Path $FinalStage "stream_voice_bot\vk_bridge\bridge.js"),
    (Join-Path $FinalStage ".runtime\node\node.exe"),
    (Join-Path $FinalStage "VERSION"),
    (Join-Path $FinalStage "Create_Desktop_Shortcut.cmd")
)

foreach ($required in $RequiredFiles) {
    if (-not (Test-Path $required -PathType Leaf)) {
        throw "Release bundle is incomplete. Missing: $required"
    }
}

if (Test-Path $BuiltBundle) {
    Remove-Item $BuiltBundle -Recurse -Force
}

if (Test-Path $WorkRoot) {
    Remove-Item $WorkRoot -Recurse -Force
}

Write-Host ""
Write-Host "BUILD SUCCESS" -ForegroundColor Green
Write-Host "Clean EXE bundle: $FinalStage"
Write-Host "Launcher:         $FinalStage\StreamVoiceBot.exe"
Write-Host ""
Write-Host "Existing dist/ and build/ folders were not modified."
Write-Host "For distribution, archive the whole versioned folder."
