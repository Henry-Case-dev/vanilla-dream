<#
.SYNOPSIS
  Deploy Vanilla Dream Radio to the production VPS.

.DESCRIPTION
  Builds the frontend locally, pushes the current main branch, uploads the static
  build and runs deploy/update.sh on the server for that exact commit.

  The server pulls code from GitHub, so the commit must be pushed first.

.EXAMPLE
  pwsh deploy/deploy.ps1
  pwsh deploy/deploy.ps1 -SkipPush
#>
param(
  [string]$SshHost = "root@192.255.209.205",
  [string]$SshKey = "$env:USERPROFILE\.ssh\adminbot_vps",
  [switch]$SkipPush,
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  $sha = (git rev-parse HEAD).Trim()
  $branch = (git rev-parse --abbrev-ref HEAD).Trim()
  if ($branch -ne "main") { throw "Deploy is allowed only from main (current: $branch)" }

  if (-not $SkipPush) {
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw "git push failed" }
  }

  if (-not $SkipBuild) {
    Push-Location (Join-Path $root "frontend")
    try {
      npm ci --no-audit --no-fund
      if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
      npm run build
      if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    } finally { Pop-Location }
  }

  $tar = Join-Path ([IO.Path]::GetTempPath()) "vdr-dist-$sha.tar.gz"
  if (Test-Path $tar) { Remove-Item $tar -Force }
  tar -czf $tar -C (Join-Path $root "frontend") dist

  $remoteTar = "/tmp/vdr-dist-$sha.tar.gz"
  scp -i $SshKey -q $tar "${SshHost}:$remoteTar"
  if ($LASTEXITCODE -ne 0) { throw "scp failed" }

  ssh -i $SshKey $SshHost "bash /opt/vanilla-dream-radio/current/deploy/update.sh --sha $sha --frontend $remoteTar"
  if ($LASTEXITCODE -ne 0) { throw "remote update failed" }

  Remove-Item $tar -Force
  Write-Host "Deployed $sha"
} finally {
  Pop-Location
}
