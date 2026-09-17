[CmdletBinding()]
param(
  [string]$OutputDirectory = "dist"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$rootModel = Join-Path $ProjectRoot "v5_ru.pt"
$model = Join-Path $ProjectRoot "models\v5_ru.pt"
if (-not (Test-Path $model) -and (Test-Path $rootModel)) { Copy-Item $rootModel $model -Force }
if (-not (Test-Path $model) -or ((Get-Item $model).Length -lt 1MB)) {
  throw "models\v5_ru.pt is required to build an offline portable bundle. Run scripts\download_silero_model.ps1 first."
}

$out = Join-Path $ProjectRoot $OutputDirectory
New-Item -ItemType Directory -Force -Path $out | Out-Null
$stage = Join-Path $env:TEMP ("StreamVoiceBot_release_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null

try {
  $items = @(
    "README.md", "README_RU.md", "SECURITY.md", "START_HERE_RU.md", "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore", ".gitattributes",
    "requirements.txt", "start_bot.bat", "start_bot_minimized.bat", "run_windows.ps1",
    "scripts", "stream_voice_bot", "models"
  )
  foreach ($item in $items) {
    $src = Join-Path $ProjectRoot $item
    if (Test-Path $src) { Copy-Item $src (Join-Path $stage $item) -Recurse -Force }
  }

  Get-ChildItem $stage -Recurse -Force -Directory | Where-Object { $_.Name -in @(".venv", "node_modules", "__pycache__", ".git") } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem $stage -Recurse -Force -File | Where-Object { $_.Extension -in @(".sqlite3", ".wav", ".log") } | Remove-Item -Force -ErrorAction SilentlyContinue

  $zip = Join-Path $out "StreamVoiceBot_1.0.0_portable.zip"
  if (Test-Path $zip) { Remove-Item $zip -Force }
  Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal
  Write-Host "Portable release created: $zip" -ForegroundColor Green
}
finally {
  Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}
