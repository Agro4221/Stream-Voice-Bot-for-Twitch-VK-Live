param(
  [Parameter(Mandatory=$true)]
  [string]$GitHubRepoUrl
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "Git for Windows is not installed. Install Git, then rerun this script."
}

if (-not (Test-Path (Join-Path $ProjectRoot 'LICENSE'))) { throw "LICENSE is missing." }
if (-not (Test-Path (Join-Path $ProjectRoot 'README.md'))) { throw "README.md is missing." }

# Generate/update npm lockfile for reproducible VK bridge installs.
$bridge = Join-Path $ProjectRoot 'stream_voice_bot\vk_bridge'
if (Test-Path (Join-Path $bridge 'package.json')) {
  Push-Location $bridge
  if (Get-Command npm -ErrorAction SilentlyContinue) { npm install --package-lock-only --ignore-scripts --no-audit --no-fund }
  Pop-Location
}

if (-not (Test-Path (Join-Path $ProjectRoot '.git'))) {
  git init
}

$hasOrigin = @(git remote 2>$null | Where-Object { $_ -eq "origin" }).Count -gt 0
if ($hasOrigin) {
  $remote = (git remote get-url origin 2>$null).Trim()
  if ($remote -ne $GitHubRepoUrl) {
    git remote set-url origin $GitHubRepoUrl
  }
} else {
  git remote add origin $GitHubRepoUrl
}

git branch -M main
git add .
git status --short
Write-Host "Review the files above. Then press Enter to create the initial commit." -ForegroundColor Cyan
Read-Host | Out-Null
git commit -m "Publish release"
git push -u origin main
