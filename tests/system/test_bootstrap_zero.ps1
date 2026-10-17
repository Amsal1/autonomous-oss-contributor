<#
.SYNOPSIS
  Bootstrap from zero against a TEST distro (DESIGN 22.7a). Manual and destructive to the test
  distro only: it refuses to run with -DistroName ossbot.
.DESCRIPTION
  Runs scripts\bootstrap.ps1 -DistroName <name> -Location <dir> twice: the first run creates the
  distro (wsl --install Ubuntu-24.04 --name <name> --location <dir> --no-launch) and provisions it,
  the second must report only 'unchanged'. Note: WSL distros share one VM and one network
  namespace, so the provision steps that touch iptables and Docker affect the whole VM; run this
  on a machine (or at a time) where that is acceptable. Remove the test distro afterwards with
  wsl --unregister <name> (never for ossbot).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DistroName,
    [Parameter(Mandatory)][string]$Location
)
$ErrorActionPreference = 'Continue'
if ($DistroName -eq 'ossbot') { Write-Host 'refused: test_bootstrap_zero.ps1 never runs against the ossbot distro'; exit 2 }
if ($DistroName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$') { Write-Host 'refused: bad distro name'; exit 2 }
$bootstrap = Join-Path $PSScriptRoot '..\..\scripts\bootstrap.ps1'

Write-Host "== first run (from zero): $DistroName at $Location"
& powershell -NoProfile -File $bootstrap -DistroName $DistroName -Location $Location
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL first bootstrap run exit $LASTEXITCODE"; exit 1 }
Write-Host '== second run (must be unchanged)'
$out = & powershell -NoProfile -File $bootstrap -DistroName $DistroName -Location $Location 2>&1 | ForEach-Object { "$_" }
$out | ForEach-Object { Write-Host $_ }
$changed = @($out | Where-Object { $_ -match '^step \S+: changed' })
if ($LASTEXITCODE -ne 0 -or $changed.Count) {
    Write-Host "FAIL second run: exit $LASTEXITCODE, changed steps: $($changed -join '; ')"
    exit 1
}
Write-Host 'PASS bootstrap from zero is idempotent'
exit 0
