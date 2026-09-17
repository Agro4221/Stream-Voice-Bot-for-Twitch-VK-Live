[CmdletBinding()]
param(
    [switch]$SkipModel,
    [switch]$SkipOptionalTools
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "=== Stream Voice Bot 1.0.0 installer ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Find-Python {
    # Prefer the py launcher with a known-good 3.11 installation.
    try {
        $null = & py -3.11 -c "import sys; print(sys.version)" 2>$null
        if ($LASTEXITCODE -eq 0) { return "py -3.11" }
    } catch {}

    foreach ($candidate in @("python", "python3")) {
        try {
            $version = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $parts = $version.Trim().Split('.')
                $major = [int]$parts[0]
                $minor = [int]$parts[1]
                if ($major -eq 3 -and $minor -ge 11 -and $minor -le 13) {
                    return $candidate
                }
            }
        } catch {}
    }
    return $null
}

function Install-WingetPackage([string]$Id) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Host "Installing $Id with winget..." -ForegroundColor Yellow
    & $winget.Source install --exact --id $Id --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget installation failed for $Id." -ForegroundColor Yellow
        return $false
    }
    Refresh-Path
    return $true
}

# ---------- Python ----------
$pythonCmd = Find-Python
if (-not $pythonCmd) {
    if (-not (Install-WingetPackage "Python.Python.3.11")) {
        # Python 3.11.9 is the last 3.11 release with a traditional Windows installer.
        $pyInstaller = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
        Write-Host "winget is unavailable. Downloading Python 3.11.9 from python.org..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $pyInstaller
        Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1" -Wait
        Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
        Refresh-Path
    }
    $pythonCmd = Find-Python
}

if (-not $pythonCmd) {
    throw "Python 3.11+ not found. Install Python manually from https://www.python.org/ and rerun this script."
}

Write-Host "Python command: $pythonCmd" -ForegroundColor Green

# ---------- Virtual environment ----------
$venv = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating local virtual environment..." -ForegroundColor Cyan
    if ($pythonCmd -eq "py -3.11") {
        & py -3.11 -m venv $venv
    } else {
        & $pythonCmd -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv" }
}

& $venvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed" }

# ---------- PyTorch ----------
$hasNvidia = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi -L 2>$null | Out-Null
    $hasNvidia = ($LASTEXITCODE -eq 0)
}

$torchOk = $false
try {
    & $venvPython -c "import torch; print(torch.__version__)" 2>$null
    $torchOk = ($LASTEXITCODE -eq 0)
} catch {}

if (-not $torchOk) {
    if ($hasNvidia) {
        Write-Host "NVIDIA GPU detected. Installing PyTorch CUDA 12.8 build..." -ForegroundColor Cyan
        & $venvPython -m pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128
    } else {
        Write-Host "NVIDIA GPU not detected. Installing PyTorch from the normal Python index..." -ForegroundColor Cyan
        & $venvPython -m pip install torch==2.10.0 torchaudio==2.10.0
    }
    if ($LASTEXITCODE -ne 0) { throw "PyTorch installation failed" }
}

# ---------- Python dependencies ----------
Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed" }

# ---------- Node.js ----------
Refresh-Path
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) {
    if (-not (Install-WingetPackage "OpenJS.NodeJS.LTS")) {
        Write-Host "Node.js LTS is required for VK Video Live bridge." -ForegroundColor Yellow
    }
    Refresh-Path
    $node = Get-Command node -ErrorAction SilentlyContinue
    $npm = Get-Command npm -ErrorAction SilentlyContinue
}

if ($node -and $npm) {
    Push-Location (Join-Path $ProjectRoot "stream_voice_bot\vk_bridge")
    & $npm.Source install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        Pop-Location
        throw "VK Video Live bridge dependencies failed to install"
    }
    Pop-Location
    Write-Host "VK Video Live bridge: ready" -ForegroundColor Green
} else {
    Write-Host "Node.js/npm not available. VK Video Live will remain unavailable until Node.js is installed." -ForegroundColor Yellow
}

# ---------- FFmpeg ----------
Refresh-Path
if (-not $SkipOptionalTools -and -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Install-WingetPackage "Gyan.FFmpeg") {
        Write-Host "FFmpeg: installed" -ForegroundColor Green
    } else {
        Write-Host "FFmpeg not found and winget is unavailable. The bot can still start, but some optional media workflows may be unavailable." -ForegroundColor Yellow
    }
}

# ---------- Silero model ----------
if (-not $SkipModel) {
    $modelDir = Join-Path $ProjectRoot "models"
    $modelPath = Join-Path $modelDir "v5_ru.pt"
    $legacyRootPath = Join-Path $ProjectRoot "v5_ru.pt"
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null

    if ((Test-Path $legacyRootPath) -and -not (Test-Path $modelPath)) {
        Write-Host "Found legacy root v5_ru.pt. Copying to models\..." -ForegroundColor Cyan
        Copy-Item $legacyRootPath $modelPath -Force
    }

    if (-not (Test-Path $modelPath) -or ((Get-Item $modelPath).Length -lt 1MB)) {
        Write-Host "Silero model not found. Downloading official v5_ru.pt..." -ForegroundColor Cyan
        $partial = "$modelPath.part"
        Remove-Item $partial -Force -ErrorAction SilentlyContinue
        Invoke-WebRequest -Uri "https://models.silero.ai/models/tts/ru/v5_ru.pt" -OutFile $partial
        if (-not (Test-Path $partial) -or ((Get-Item $partial).Length -lt 1MB)) {
            Remove-Item $partial -Force -ErrorAction SilentlyContinue
            throw "Silero model download failed or returned an invalid file."
        }
        Move-Item $partial $modelPath -Force
        Write-Host "Silero model: downloaded" -ForegroundColor Green
    } else {
        Write-Host "Silero model: already present" -ForegroundColor Green
    }
}

# ---------- Optional VB-CABLE check ----------
try {
    $cable = & $venvPython -c "import sounddevice as sd; print('\\n'.join(str(d['name']) for d in sd.query_devices()))" 2>$null
    if (-not ($cable -match "(?i)CABLE Input")) {
        Write-Host "VB-CABLE: not detected. Install VB-CABLE manually if you want the OBS audio route." -ForegroundColor Yellow
    } else {
        Write-Host "VB-CABLE: detected" -ForegroundColor Green
    }
} catch {}

Write-Host "" 
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Run start_bot.bat and open http://127.0.0.1:8787" -ForegroundColor Cyan
