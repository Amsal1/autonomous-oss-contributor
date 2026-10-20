<#
.SYNOPSIS
  Graceful down before a laptop shutdown (CR-001). Not a kill.
.DESCRIPTION
  1. If the distro runs: `ossctl down` as root. It stops scheduling new work (draining), lets
     running jobs finish for up to -Timeout seconds (default: policy.yaml lifecycle.drain_timeout_s
     = 120), marks the rest interrupted (re-queued on up), sends "Going down: N finished,
     M interrupted" to Telegram, writes the shutdown marker and stops the units (workers first,
     gatekeeper and proxy last).
  2. Then `wsl --terminate ossbot`; -All uses `wsl --shutdown` (WSL is dedicated to ossbot).
  The autostart task stays enabled and armed approvals stay valid; scripts\up.ps1 (or the next
  logon) brings it back. A wedged in-distro step is cut off after Timeout + 90 s and the distro
  is terminated anyway (crash safety covers the rest).
  down.ps1, up.ps1 and the shutdown blocker's channel drain serialise on the per-user mutex
  Local\ossbot-lifecycle (DESIGN 13.5): a second down waits up to its own Timeout + 90 s, then exits 0
  when the distro is already stopped. Every run is logged to .ossbot-local\lifecycle.log.
  At a Windows shutdown the blocker does not run this script (no new process can start in an ending
  session); it drains through its channel instead (ADR-0021 addendum post-v1). This script is for
  manual, Start-menu and test use.
.PARAMETER Kind
  graceful (default) or hook (the retired 6b task's marker type; kept for manual tests): only changes
  the marker type.
.PARAMETER Caller
  Who asked (manual, blocker, hook, a test name); logged only.
#>
[CmdletBinding()]
param(
    [int]$Timeout = -1,
    [switch]$All,
    [ValidateSet('graceful', 'hook')][string]$Kind = 'graceful',
    [ValidatePattern('^[A-Za-z0-9_-]{1,40}$')][string]$Caller = 'manual'
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$drain = if ($Timeout -ge 0) { $Timeout } else { 120 }
$capSec = $drain + 90
$tag = "down kind=$Kind caller=$Caller pid=$PID"

# 6b fires on User32 1074, usually a moment before the blocker (6a) gets WM_QUERYENDSESSION: give the
# primary path (90 s drain) the first claim on the lock when the blocker is running.
if ($Kind -eq 'hook' -and (Test-OssBlockerRunning)) { Start-Sleep -Seconds 3 }

$lock = Enter-OssLifecycleLock -TimeoutSec 0
if ($null -eq $lock) {
    Write-OssLifecycleLog "${tag}: waiting for the lifecycle lock (another down/up runs)"
    $lock = Enter-OssLifecycleLock -TimeoutSec $capSec
    if ($null -eq $lock) {
        if (-not (Test-OssDistroRunning)) {
            Write-OssLifecycleLog "${tag}: lock not free after $capSec s; distro already stopped, nothing to do"
            Write-Host "another lifecycle run holds the lock; $($script:OssDistro) is already stopped"
            exit 0
        }
        Write-OssLifecycleLog "${tag}: lock not free after $capSec s and the distro still runs; terminating it"
        Write-Warning "another lifecycle run holds the lock for more than $capSec s; terminating the distro"
        & wsl.exe --terminate $script:OssDistro | Out-Null
        exit 4
    }
}

$exit = 0
Write-OssLifecycleLog "${tag}: start timeout=$Timeout"
if (Test-OssDistroRunning) {
    $argLine = "-d $($script:OssDistro) -u root --exec /opt/ossbot/app/.venv/bin/ossctl down --kind $Kind"
    if ($Timeout -ge 0) { $argLine += " --timeout $Timeout" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo 'wsl.exe'
    $psi.Arguments = $argLine
    $psi.UseShellExecute = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    if (-not $p.WaitForExit($capSec * 1000)) {
        Write-Warning "ossctl down did not finish in $capSec s; terminating the distro anyway"
        try { $p.Kill() } catch { }
        $exit = 4
    } elseif ($p.ExitCode -ne 0) {
        Write-Warning "ossctl down exit $($p.ExitCode)"
        $exit = $p.ExitCode
    }
    Write-OssLifecycleLog "${tag}: ossctl-exit=$exit"
} else {
    Write-Host "$($script:OssDistro) distro not running; nothing to drain."
    Write-OssLifecycleLog "${tag}: distro not running"
}
if ($All) {
    & wsl.exe --shutdown | Out-Null
    Write-Host 'WSL stopped (wsl --shutdown)'
} else {
    & wsl.exe --terminate $script:OssDistro | Out-Null
    Write-Host "distro $($script:OssDistro) stopped (wsl --terminate)"
}
Write-OssLifecycleLog "${tag}: end exit=$exit"
Exit-OssLifecycleLock $lock
exit $exit
