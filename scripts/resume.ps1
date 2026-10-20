<#
.SYNOPSIS
  Resume ossbot after a kill or pause, from Windows (DESIGN 13.3).
.DESCRIPTION
  Starts the distro (the boot gate keeps it killed while the marker exists), re-enables the
  'ossbot-autostart' task, asks for "Type RESUME", then streams scripts/bin/ossbot-resume --offline
  as root. ossbot-resume appends to /srv/ossbot/logs/resume.log first (the gatekeeper turns it into
  an audit event and a Telegram FYI), removes the flags and the marker together and starts the
  units in order: gatekeeper, proxy, dockerfilter, sandboxd (runner, hermes when installed).
  Dry-run is never changed by a resume.
.PARAMETER AssumeYes
  Skip the typed confirmation (tests/system/test_kill.ps1). The Windows user is root-equivalent over
  the distro anyway (T7); the resume is still logged and announced.
#>
[CmdletBinding()]
param([switch]$AssumeYes)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$state = Start-OssDistro
Write-Host "distro $($script:OssDistro) started (systemd: $state)"
$task = Get-ScheduledTask -TaskName 'ossbot-autostart' -ErrorAction SilentlyContinue
if (Test-OssTaskDisabled -Task $task) {
    Enable-ScheduledTask -TaskName 'ossbot-autostart' | Out-Null
    Write-Host 'Scheduled Task ossbot-autostart re-enabled'
}
if (-not $AssumeYes) {
    $answer = Read-Host 'Type RESUME to resume ossbot'
    if ($answer -ne 'RESUME') { Write-Host 'not confirmed'; exit 1 }
}
$actor = ("windows:" + $env:USERNAME) -replace '[^A-Za-z0-9_.@:-]', ''
$code = Invoke-OssStream -Text (Get-OssScriptText @('scripts/bin/ossbot-resume')) -Arguments @('--offline', '--via', 'resume.ps1', '--actor', $actor, '--actor-uid', '0')
if ($code -ne 0) { Write-Warning "ossbot-resume exit $code (3 = a unit did not start)" }
exit $code
