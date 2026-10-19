<#
.SYNOPSIS
  Encrypted backup of the state outside git (DESIGN 18.8, ADR-0016): `ossctl backup` as root.
.DESCRIPTION
  Pauses ossbot (unless it is already paused or killed), lets running stages checkpoint, copies the
  gatekeeper DB with SQLite's online backup, verifies the audit chain on the copy, collects the
  include list (profile, anchors, config snapshots, open contributions' bundles, Hermes state, active
  skills, feature stores, config\telegram.yaml), encrypts it with age to profile.backup.age_recipients
  and writes backups\ossbot-<yyyyMMdd-HHmmss>.tar.age (gitignored; the newest 8 are kept), then lifts
  its own pause. Every archive is encrypted; with no recipient in the profile nothing is written.
  Starts the distro if it is stopped.
.PARAMETER IncludeSecrets
  Also /etc/ossbot/secrets/*.env (keep such an archive as safe as the keys themselves).
.PARAMETER IncludeArtifacts
  Every artifact and run log, not only the bundles of open contributions.
.PARAMETER IncludeAttachments
  The transient chat attachments (excluded by default; they expire after 24 h).
#>
[CmdletBinding()]
param([switch]$IncludeSecrets, [switch]$IncludeArtifacts, [switch]$IncludeAttachments)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

if (-not (Test-OssDistroRunning)) {
    $state = Start-OssDistro
    if ($state -eq 'timeout') { Write-Warning "the $($script:OssDistro) distro did not finish booting"; exit 1 }
}
$cmd = @('/opt/ossbot/app/.venv/bin/ossctl', 'backup')
if ($IncludeSecrets) { $cmd += '--include-secrets' }
if ($IncludeArtifacts) { $cmd += '--include-artifacts' }
if ($IncludeAttachments) { $cmd += '--include-attachments' }
& wsl.exe -d $script:OssDistro -u root --exec @cmd
exit $LASTEXITCODE
