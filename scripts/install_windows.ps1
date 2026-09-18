[CmdletBinding()]
param(
    [switch]$SkipModel,
    [switch]$SkipOptionalTools
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$LocalPythonRoot = Join-Path $RuntimeRoot "python"
$LocalNodeRoot = Join-Path $RuntimeRoot "node"
$DownloadRoot = Join-Path $RuntimeRoot "downloads"
$CacheRoot = Join-Path $ProjectRoot ".cache"
$PipCache = Join-Path $CacheRoot "pip"
$HfCache = Join-Path $CacheRoot "huggingface"

New-Item -ItemType Directory -Force -Path $RuntimeRoot, $DownloadRoot, $PipCache, $HfCache | Out-Null
$env:PIP_CACHE_DIR = $PipCache
$env:HF_HOME = $HfCache
$env:HUGGINGFACE_HUB_CACHE = Join-Path $HfCache "hub"

Write-Host "=== Stream Voice Bot 1.0.3 installer ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host "Runtime/cache root: $RuntimeRoot"

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-PythonExe([string]$Exe) {
    if (-not (Test-Path $Exe -PathType Leaf)) { return $false }
    try {
        & $Exe -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}'); raise SystemExit(0 if (v.major == 3 and 11 <= v.minor <= 13) else 1)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    $local = Join-Path $LocalPythonRoot "python.exe"
    if (Test-PythonExe $local) { return $local }

    try {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & $py.Source -3.11 -c "import sys; v=sys.version_info; raise SystemExit(0 if (v.major == 3 and v.minor == 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return "py -3.11" }
        }
    } catch {}

    foreach ($candidate in @("python", "python3")) {
        try {
            $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
            if (-not $cmd) { continue }
            $version = & $cmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $parts = $version.Trim().Split('.')
                if ($parts.Count -ge 2) {
                    $major = [int]$parts[0]; $minor = [int]$parts[1]
                    if ($major -eq 3 -and $minor -ge 11 -and $minor -le 13) { return $cmd.Source }
                }
            }
        } catch {}
    }
    return $null
}

function Install-LocalPython {
    # Python 3.11.9 is used as a compatibility bootstrap because it is the
    # final 3.11 release that still ships the classic Windows executable installer.
    $installer = Join-Path $DownloadRoot "python-3.11.9-amd64.exe"
    $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    Write-Host "No compatible Python found. Installing a private Python runtime into:" -ForegroundColor Yellow
    Write-Host "  $LocalPythonRoot" -ForegroundColor Yellow
    if (-not (Test-Path $installer)) {
        Invoke-WebRequest -Uri $url -OutFile $installer
    }
    if (Test-Path (Join-Path $LocalPythonRoot "python.exe")) {
        Remove-Item $LocalPythonRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $LocalPythonRoot | Out-Null
    $args = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=0",
        "Include_launcher=0",
        "Include_test=0",
        "Include_pip=1",
        "Include_doc=0",
        "Include_tcltk=0",
        "TargetDir=`"$LocalPythonRoot`""
    )
    $proc = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru
    if ($proc.ExitCode -ne 0 -or -not (Test-Path (Join-Path $LocalPythonRoot "python.exe"))) {
        throw "Could not install private Python runtime (exit code $($proc.ExitCode))."
    }
}

function Install-LocalNode {
    $nodeExe = Join-Path $LocalNodeRoot "node.exe"
    $npmCmd = Join-Path $LocalNodeRoot "npm.cmd"
    if ((Test-Path $nodeExe) -and (Test-Path $npmCmd)) {
        return
    }

    # Current Node.js 24 LTS Windows x64 archive from nodejs.org.
    $version = "24.21.0"
    $archive = Join-Path $DownloadRoot "node-v$version-win-x64.zip"
    $url = "https://nodejs.org/dist/v$version/node-v$version-win-x64.zip"
    Write-Host "No usable Node.js found. Installing a private Node.js runtime into:" -ForegroundColor Yellow
    Write-Host "  $LocalNodeRoot" -ForegroundColor Yellow
    Invoke-WebRequest -Uri $url -OutFile $archive

    $extract = Join-Path $DownloadRoot "node_extract"
    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    if (Test-Path $LocalNodeRoot) { Remove-Item $LocalNodeRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    Expand-Archive -Path $archive -DestinationPath $extract -Force
    $inner = Get-ChildItem $extract -Directory | Select-Object -First 1
    if (-not $inner) { throw "Node.js archive extraction failed." }
    Move-Item $inner.FullName $LocalNodeRoot -Force
    Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path $nodeExe) -or -not (Test-Path $npmCmd)) {
        throw "Private Node.js runtime installation failed."
    }
}

function Find-Node {
    $local = Join-Path $LocalNodeRoot "node.exe"
    $localNpm = Join-Path $LocalNodeRoot "npm.cmd"
    if ((Test-Path $local -PathType Leaf) -and (Test-Path $localNpm -PathType Leaf)) {
        return @{ Node = $local; Npm = $localNpm }
    }
    Refresh-Path
    $node = Get-Command node -ErrorAction SilentlyContinue
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($node -and $npm) { return @{ Node = $node.Source; Npm = $npm.Source } }
    return $null
}

# ---------- Python ----------
$pythonCmd = Find-Python
if (-not $pythonCmd) {
    Install-LocalPython
    $pythonCmd = Find-Python
}
if (-not $pythonCmd) { throw "Python 3.11-3.13 was not found and private installation failed." }
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
    & $venvPython -c "import torch; print(torch.__version__)" 2>$null | Out-Null
    $torchOk = ($LASTEXITCODE -eq 0)
} catch {}
if (-not $torchOk) {
    if ($hasNvidia) {
        Write-Host "NVIDIA GPU detected. Installing PyTorch CUDA 12.8 build..." -ForegroundColor Cyan
        & $venvPython -m pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128
    } else {
        Write-Host "NVIDIA GPU not detected. Installing PyTorch CPU build..." -ForegroundColor Cyan
        & $venvPython -m pip install torch==2.10.0 torchaudio==2.10.0
    }
    if ($LASTEXITCODE -ne 0) { throw "PyTorch installation failed" }
}

# ---------- Python dependencies ----------
Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed" }

# ---------- Node.js / VK bridge ----------
$nodeInfo = Find-Node
if (-not $nodeInfo) {
    Install-LocalNode
    $nodeInfo = Find-Node
}
if (-not $nodeInfo) {
    throw "Node.js/npm could not be prepared for the VK Video Live bridge."
}

$nodeDir = Split-Path -Parent $nodeInfo.Node
$env:Path = "$nodeDir;$env:Path"
$bridgeDir = Join-Path $ProjectRoot "stream_voice_bot\vk_bridge"
Push-Location $bridgeDir
try {
    $lockExists = Test-Path (Join-Path $bridgeDir "package-lock.json")
    Write-Host "Installing VK Video Live bridge dependencies..." -ForegroundColor Cyan
    if ($lockExists) { & $nodeInfo.Npm ci --no-audit --no-fund } else { & $nodeInfo.Npm install --no-audit --no-fund }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "npm failed. Cleaning npm cache and retrying once..." -ForegroundColor Yellow
        & $nodeInfo.Npm cache clean --force | Out-Null
        if (Test-Path (Join-Path $bridgeDir "node_modules")) {
            Remove-Item (Join-Path $bridgeDir "node_modules") -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($lockExists) { & $nodeInfo.Npm ci --no-audit --no-fund } else { & $nodeInfo.Npm install --no-audit --no-fund }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "VK Video Live bridge dependencies failed to install after retry. Close the bot/Node processes and rerun the installer."
    }
} finally {
    Pop-Location
}
Write-Host "VK Video Live bridge: ready" -ForegroundColor Green

# ---------- FFmpeg (optional only; never required by the core bot) ----------
Refresh-Path
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Host "FFmpeg: detected" -ForegroundColor Green
} elseif (-not $SkipOptionalTools) {
    Write-Host "FFmpeg: not installed (optional; core TTS/STT/chat features do not require it)." -ForegroundColor DarkYellow
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
