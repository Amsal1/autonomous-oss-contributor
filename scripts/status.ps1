<#
.SYNOPSIS
  One-screen ossbot state (CR-001): lifecycle, kill, dry-run, running work, pending approvals,
  Telegram last poll, last shutdown type.
.DESCRIPTION
  Never starts the distro: when it is stopped, only the Windows-side facts are shown (kill
  marker, autostart task, last lifecycle log line). Otherwise runs `ossctl status` as root.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$repo = Get-OssRepoRoot
$marker = Join-Path $repo '.ossbot-local\KILL'
$task = Get-ScheduledTask -TaskName 'ossbot-autostart' -ErrorAction SilentlyContinue
Write-Host ("kill marker:    {0}" -f $(if (Test-Path -LiteralPath $marker) { 'PRESENT (killed, level full)' } else { 'absent' }))
Write-Host ("autostart task: {0}" -f $(if ($task) { $task.State } else { 'missing' }))
$log = Join-Path $repo '.ossbot-local\lifecycle.log'
if (Test-Path -LiteralPath $log) { Write-Host ("last lifecycle: {0}" -f (Get-Content -LiteralPath $log -Tail 1)) }
if (-not (Test-OssDistroRunning)) {
    Write-Host "distro:         $($script:OssDistro) stopped (scripts\up.ps1 starts it)"
    exit 0
}
Write-Host "distro:         $($script:OssDistro) running"
& wsl.exe -d $script:OssDistro -u root --exec /opt/ossbot/app/.venv/bin/ossctl status
exit $LASTEXITCODE
