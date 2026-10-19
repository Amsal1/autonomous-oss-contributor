<#
.SYNOPSIS
  Bring ossbot back up after scripts\down.ps1 or a laptop restart (CR-001).
.DESCRIPTION
  1. Refuses while a kill is active (.ossbot-local\KILL): kill overrides lifecycle; use resume.ps1.
  2. Starts the distro (systemd starts the units; the boot gate ends a distro-scope down).
  3. Runs `ossctl up` as root: starts the units, waits until they are healthy (-Timeout, default
     from policy.yaml lifecycle.up_health_timeout_s = 180 s), lifts draining/down, runs
     `doctor --quick` and sends "Back up: X resumed, Y awaiting approval" to Telegram.
  Not a resume: dry-run, pause and approvals are unchanged. Serialised with down.ps1 on the
  per-user mutex Local\ossbot-lifecycle (waits up to -LockTimeout s); logged to
  .ossbot-local\lifecycle.log.
#>
[CmdletBinding()]
param(
    [int]$Timeout = 0,
    [int]$LockTimeout = 300,
    [ValidatePattern('^[A-Za-z0-9_-]{1,40}$')][string]$Caller = 'manual'
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$tag = "up caller=$Caller pid=$PID"
$marker = Join-Path (Get-OssRepoRoot) '.ossbot-local\KILL'
if (Test-Path -LiteralPath $marker) {
    Write-Warning 'ossbot is killed (.ossbot-local\KILL present): up refused. Use scripts\resume.ps1 first.'
    Write-OssLifecycleLog "${tag}: refused (kill marker present)"
    exit 3
}
$lock = Enter-OssLifecycleLock -TimeoutSec 0
if ($null -eq $lock) {
    Write-OssLifecycleLog "${tag}: waiting for the lifecycle lock (another down/up runs)"
    $lock = Enter-OssLifecycleLock -TimeoutSec $LockTimeout
    if ($null -eq $lock) {
        Write-OssLifecycleLog "${tag}: lock not free after $LockTimeout s; up not run"
        Write-Warning "another down/up still runs after $LockTimeout s; up not run (see .ossbot-local\lifecycle.log)"
        exit 1
    }
}
Write-OssLifecycleLog "${tag}: start"
$code = 0
$state = Start-OssDistro
if ($state -eq 'timeout') {
    Write-Warning "the $($script:OssDistro) distro did not finish booting in time"
    $code = 1
} else {
    Write-Host "distro $($script:OssDistro): systemd $state"
    $cmd = @('/opt/ossbot/app/.venv/bin/ossctl', 'up')
    if ($Timeout -gt 0) { $cmd += @('--timeout', "$Timeout") }
    & wsl.exe -d $script:OssDistro -u root --exec @cmd
    $code = $LASTEXITCODE
}
Write-OssLifecycleLog "${tag}: end exit=$code"
Exit-OssLifecycleLock $lock
exit $code
