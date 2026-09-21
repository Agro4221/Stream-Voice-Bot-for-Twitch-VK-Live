param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Url = "http://127.0.0.1:8787/",
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$LogFile = Join-Path $RuntimeDir "launcher.log"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Log([string]$Text) {
    Add-Content -LiteralPath $LogFile -Value "$(Get-Date -Format "yyyy-MM-dd HH:mm:ss") $Text" -Encoding UTF8
}

try {
    if (-not (Test-Path $PythonExe -PathType Leaf)) {
        throw "Python runtime not found: $PythonExe"
    }

    Log "Launching bot with python.exe."
    $p = Start-Process -FilePath $PythonExe -ArgumentList @("-m", "stream_voice_bot") -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
    Log "Bot PID=$($p.Id)."

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if ($p.HasExited) {
            Log "Bot exited before HTTP ready. ExitCode=$($p.ExitCode)."
            exit 1
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Log "Admin ready: $Url"
                Start-Process $Url | Out-Null
                Log "Browser start requested."
                exit 0
            }
        } catch {
        }
    }
    Log "Admin did not become ready within $TimeoutSeconds seconds."
    exit 1
}
catch {
    Log "Launcher exception: $($_.Exception.Message)"
    exit 1
}
