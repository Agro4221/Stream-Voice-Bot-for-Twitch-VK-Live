[CmdletBinding()]
param(
  [string]$Destination = ""
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = Join-Path $ProjectRoot "models\v5_ru.pt" }
$parent = Split-Path -Parent $Destination
New-Item -ItemType Directory -Force -Path $parent | Out-Null

$url = "https://models.silero.ai/models/tts/ru/v5_ru.pt"
$partial = "$Destination.part"
Remove-Item $partial -Force -ErrorAction SilentlyContinue
Write-Host "Downloading Silero V5 RU model..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $url -OutFile $partial
if (-not (Test-Path $partial) -or ((Get-Item $partial).Length -lt 1MB)) {
  Remove-Item $partial -Force -ErrorAction SilentlyContinue
  throw "Downloaded model is missing or unexpectedly small."
}
Move-Item $partial $Destination -Force
Write-Host "Model saved to: $Destination" -ForegroundColor Green
