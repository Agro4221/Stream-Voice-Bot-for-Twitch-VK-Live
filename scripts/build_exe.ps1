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

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $kitsRoot = Join-Path $env:ProgramFiles(x86) "Windows Kits\10\bin"
    if (Test-Path $kitsRoot) {
        $candidate = Get-ChildItem -Path $kitsRoot -Filter signtool.exe -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }

    return $null
}

function Sign-BuiltExe {
    param([string]$ExePath)

    $pfxBase64 = $env:STREAMVOICEBOT_PFX_BASE64
    $pfxPassword = $env:STREAMVOICEBOT_PFX_PASSWORD
    if ([string]::IsNullOrWhiteSpace($pfxBase64) -or [string]::IsNullOrWhiteSpace($pfxPassword)) {
        Write-Host "Code signing not configured. The EXE will be unsigned." -ForegroundColor Yellow
        Write-Host "Configure STREAMVOICEBOT_PFX_BASE64 and STREAMVOICEBOT_PFX_PASSWORD for Authenticode signing." -ForegroundColor Yellow
        return
    }

    $signTool = Find-SignTool
    if (-not $signTool) {
        throw "A signing certificate is configured, but signtool.exe was not found."
    }

    $pfxPath = Join-Path $env:TEMP ("StreamVoiceBot-" + [Guid]::NewGuid().ToString("N") + ".pfx")
    try {
        [System.IO.File]::WriteAllBytes($pfxPath, [Convert]::FromBase64String($pfxBase64))
        Write-Host "Signing StreamVoiceBot.exe with Authenticode..." -ForegroundColor Cyan
        Invoke-Checked $signTool @(
            "sign",
            "/fd", "SHA256",
            "/f", $pfxPath,
            "/p", $pfxPassword,
            "/tr", "http://timestamp.digicert.com",
            "/td", "SHA256",
            $ExePath
        ) "Authenticode signing failed."

        $signature = Get-AuthenticodeSignature -FilePath $ExePath
        if ($signature.Status -ne "Valid") {
            throw "Authenticode signature is not valid after signing: $($signature.Status)"
        }
        Write-Host ("Code signing: VALID ({0})" -f $signature.SignerCertificate.Subject) -ForegroundColor Green
    }
    finally {
        Remove-Item $pfxPath -Force -ErrorAction SilentlyContinue
    }
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

if (Test-Path $DistRoot) {
    Get-ChildItem $DistRoot -Force |
        Where-Object { $_.Name -eq "StreamVoiceBot" -or $_.Name -like "StreamVoiceBot_*" } |
        Remove-Item -Recurse -Force
}

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

$Readme = Join-Path $FinalStage "README_EXE.txt"
$readmeBytes = [Convert]::FromBase64String("U3RyZWFtIFZvaWNlIEJvdCB2MS4xLjExCgrQkdGL0YHRgtGA0YvQuSDQt9Cw0L/Rg9GB0LoKMS4g0JfQsNC/0YPRgdGC0LggU3RyZWFtVm9pY2VCb3QuZXhlLgoyLiDQm9C+0LrQsNC70YzQvdCw0Y8g0LDQtNC80LjQvdC60LAg0L7RgtC60YDQvtC10YLRgdGPINCw0LLRgtC+0LzQsNGC0LjRh9C10YHQutC4OgogICBodHRwOi8vMTI3LjAuMC4xOjg3ODcvCgrQp9GC0L4g0LLRhdC+0LTQuNGCINCyINC/0LDQutC10YIKLSBTaWxlcm8gVjUgKG1vZGVscy92NV9ydS5wdCkg0YPQttC1INCy0YHRgtGA0L7QtdC9INCyIEVYRS3Qv9Cw0LrQtdGCLgotIE5vZGUuanMgcnVudGltZSDQtNC70Y8gVksg0JLQuNC00LXQviBMaXZlINGD0LbQtSDQstGB0YLRgNC+0LXQvS4KLSDQktGB0LUg0YHQu9GD0LbQtdCx0L3Ri9C1IERMTC9QWUQg0LggUHl0aG9uLdC30LDQstC40YHQuNC80L7RgdGC0Lgg0L3QsNGF0L7QtNGP0YLRgdGPINCy0L3Rg9GC0YDQuCDQv9Cw0L/QutC4IF9pbnRlcm5hbC4KLSDQn9C+0LvRjNC30L7QstCw0YLQtdC70YzRgdC60LjQtSDQvdCw0YHRgtGA0L7QudC60Lgg0Lgg0LHQsNC30LAg0YHQvtC30LTQsNGO0YLRgdGPINCyINC/0LDQv9C60LUgZGF0YSDRgNGP0LTQvtC8INGBIEVYRS4KClRUUyDQuCBPQlMKLSDQkiDQvdCw0YHRgtGA0L7QudC60LDRhSDQstGL0LHQtdGA0Lgg0L3Rg9C20L3QvtC1INCw0YPQtNC40L7Rg9GB0YLRgNC+0LnRgdGC0LLQviDQuCDQv9GA0L7QstC10YDRjCDQt9Cy0YPQuiDQutC90L7Qv9C60L7QuSDCq9Ci0LXRgdGCINC30LLRg9C60LDCuy4KLSDQlNC70Y8gT0JTINC40YHQv9C+0LvRjNC30YPQtdGC0YHRjyBWQi1DQUJMRTog0LHQvtGCINCy0YvQstC+0LTQuNGCINC30LLRg9C6INCyIENBQkxFIElucHV0LCDQsCBPQlMg0LfQsNGF0LLQsNGC0YvQstCw0LXRgiBDQUJMRSBPdXRwdXQuCi0g0JzQvtC90LjRgtC+0YDQuNC90LMg0LPQvtC70L7RgdCwINCyINC90LDRg9GI0L3QuNC60Lgg0L3QsNGB0YLRgNCw0LjQstCw0LXRgtGB0Y8g0LLQvdGD0YLRgNC4IE9CUywg0YfRgtC+0LHRiyDQvdC1INGB0L7Qt9C00LDQstCw0YLRjCDQsNGD0LTQuNC+0L/QtdGC0LvRji4KClNUVCAvINGB0YPQsdGC0LjRgtGA0YsKLSBTVFQg0LfQsNC/0YPRgdC60LDQtdGC0YHRjyDQstGA0YPRh9C90YPRjiDQutC90L7Qv9C60L7QuSDCq9CX0LDQv9GD0YHRgtC40YLRjCBTVFTCuy4KLSDQn9C+INGD0LzQvtC70YfQsNC90LjRjiDQuNGB0L/QvtC70YzQt9GD0LXRgtGB0Y8gbGFyZ2UtdjMtdHVyYm8uCi0g0JIg0YfQuNGB0YLQvtC5INGB0LjRgdGC0LXQvNC1INC/0LXRgNCy0YvQuSDQt9Cw0L/Rg9GB0LogU1RUINC80L7QttC10YIg0LHRi9GC0Ywg0LfQsNC80LXRgtC90L4g0LTQvtC70YzRiNC1INC+0LHRi9GH0L3QvtCz0L46INC80L7QtNC10LvRjCDQvNC+0LbQtdGCINGB0LrQsNGH0LjQstCw0YLRjNGB0Y8g0Lgg0L/QvtC00LPQvtGC0LDQstC70LjQstCw0YLRjNGB0Y8g0LTQu9GPINC70L7QutCw0LvRjNC90L7QuSDRgNCw0LHQvtGC0YsuCi0g0JIg0L/QtdGA0LXQvdC+0YHQuNC80L7QuSBXaW5kb3dzLdGB0LHQvtGA0LrQtSBTVFQg0LjRgdC/0L7Qu9GM0LfRg9C10YIgQ1BVIGludDgsINC/0L7RjdGC0L7QvNGDINC+0YLQtNC10LvRjNC90YvQtSBDVURBL2N1QkxBUyBETEwg0L3QtSDRgtGA0LXQsdGD0Y7RgtGB0Y8uCi0g0J/QvtGB0LvQtSDQv9C10YDQstC+0LPQviDRg9GB0L/QtdGI0L3QvtCz0L4g0LfQsNC/0YPRgdC60LAg0L/QvtCy0YLQvtGA0L3QsNGPINC30LDQs9GA0YPQt9C60LAg0LzQvtC00LXQu9C4INC+0LHRi9GH0L3QviDQsdGL0YHRgtGA0LXQtS4KLSDQldGB0LvQuCBTVFQg0LTQvtC70LPQviDQvtGB0YLQsNGR0YLRgdGPINCyINGB0L7RgdGC0L7Rj9C90LjQuCDQt9Cw0LPRgNGD0LfQutC4LCDQvtGC0LrRgNC+0LkgwqvQn9C+0LTRgNC+0LHQvdGL0LUg0LvQvtCz0LjCuyDQsiDQsNC00LzQuNC90LrQtSDQuCDQv9GA0L7QstC10YDRjCBkYXRhL2V4ZV9zdGFydHVwLmxvZy4KCtCU0LDQvdC90YvQtSDQuCDQv9C10YDQtdC90L7RgdC40LzQvtGB0YLRjAotINCd0LUg0LfQsNC/0YPRgdC60LDQuSDRgtC+0LvRjNC60L4g0L7QtNC40L0gU3RyZWFtVm9pY2VCb3QuZXhlINCx0LXQtyDQv9Cw0L/QutC4IF9pbnRlcm5hbC4KLSDQlNC70Y8g0L/QtdGA0LXQvdC+0YHQsCDQutC+0L/QuNGA0YPQuSDQstGB0Y4g0L/QsNC/0LrRgyBTdHJlYW1Wb2ljZUJvdF8xLjEuMTEg0YbQtdC70LjQutC+0LwuCi0g0J/QsNC/0LrQsCBkYXRhINGB0L7Qt9C00LDRkdGC0YHRjyDRgNGP0LTQvtC8INGBIEVYRSDQuCDQvdC1INC00L7Qu9C20L3QsCDQv9C+0L/QsNC00LDRgtGMINCyINC/0YPQsdC70LjRh9C90YvQuSDQsNGA0YXQuNCyINGBINC/0L7Qu9GM0LfQvtCy0LDRgtC10LvRjNGB0LrQuNC80Lgg0L3QsNGB0YLRgNC+0LnQutCw0LzQuC4KLSDQotC+0LrQtdC90YsgVHdpdGNoL1ZLINC90LUg0LLRhdC+0LTRj9GCINCyINC/0LDQutC10YIg0Lgg0L3QsNGB0YLRgNCw0LjQstCw0Y7RgtGB0Y8g0L7RgtC00LXQu9GM0L3QviDQvdCwINC60LDQttC00L7QvCDQn9CaLgoK0JXRgdC70LggV2luZG93cyDQv9C+0LrQsNC30YvQstCw0LXRgiBTbWFydFNjcmVlbgrQndC10L/QvtC00L/QuNGB0LDQvdC90YvQuSBFWEUg0LzQvtC20LXRgiDQv9C+0LrQsNC30LDRgtGMINGB0YLQsNC90LTQsNGA0YLQvdC+0LUg0L/RgNC10LTRg9C/0YDQtdC20LTQtdC90LjQtSBXaW5kb3dzLiDQrdGC0L4g0L3QtSDQvtC30L3QsNGH0LDQtdGCLCDRh9GC0L4g0L/RgNC40LvQvtC20LXQvdC40LUg0L3Rg9C20L3QviDQt9Cw0L/Rg9GB0LrQsNGC0Ywg0LLRgdC70LXQv9GD0Y47INC/0YDQvtCy0LXRgNGP0Lkg0LjRgdGC0L7Rh9C90LjQuiDRhNCw0LnQu9CwINC4INGG0LXQu9C+0YHRgtC90L7RgdGC0Ywg0LDRgNGF0LjQstCwLgoK0J/QvtC70LXQt9C90L4g0L/RgNC4INC/0YDQvtCx0LvQtdC80LDRhQotINCQ0LTQvNC40L3QutCwOiBodHRwOi8vMTI3LjAuMC4xOjg3ODcvCi0g0JvQvtCz0Lgg0LfQsNC/0YPRgdC60LAgRVhFOiBkYXRhL2V4ZV9zdGFydHVwLmxvZwotIFJ1bnRpbWUt0LvQvtCz0Lg6INGA0LDQt9C00LXQuyDCq9Cf0L7QtNGA0L7QsdC90YvQtSDQu9C+0LPQuMK7INCyINCw0LTQvNC40L3QutC1Lgo=")
$readmeText = [Text.Encoding]::UTF8.GetString($readmeBytes)
[IO.File]::WriteAllText($Readme, $readmeText, [Text.UTF8Encoding]::new($true))

$InternalRoot = Join-Path $FinalStage "_internal"

$RequiredFiles = @(
    (Join-Path $FinalStage "StreamVoiceBot.exe"),
    (Join-Path $InternalRoot "models\v5_ru.pt"),
    (Join-Path $InternalRoot "stream_voice_bot\web\index.html"),
    (Join-Path $InternalRoot "stream_voice_bot\vk_bridge\bridge.js"),
    (Join-Path $InternalRoot ".runtime\node\node.exe"),
    (Join-Path $InternalRoot "VERSION")
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
Write-Host "Desktop shortcut: created automatically on first EXE launch."
Write-Host "User-facing layout: EXE + _internal + data + README."
Write-Host ""
Write-Host "Existing dist/ and build/ folders were not modified."
Write-Host "For distribution, archive the whole versioned folder."
