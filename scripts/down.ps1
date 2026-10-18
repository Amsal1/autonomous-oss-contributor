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
.PARAMETER Kind
  graceful (default) or hook (the shutdown-event task, CR-001 6b): only changes the marker type.
#>
[CmdletBinding()]
param(
    [int]$Timeout = -1,
    [switch]$All,
    [ValidateSet('graceful', 'hook')][string]$Kind = 'graceful'
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$logFile = Join-Path (New-OssLocalDir) 'lifecycle.log'
function Write-Log([string]$Text) {
    Add-Content -LiteralPath $logFile -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString('o'), $Text)
}

$exit = 0
if (Test-OssDistroRunning) {
    $argLine = "-d $($script:OssDistro) -u root --exec /opt/ossbot/app/.venv/bin/ossctl down --kind $Kind"
    if ($Timeout -ge 0) { $argLine += " --timeout $Timeout" }
    $drain = if ($Timeout -ge 0) { $Timeout } else { 120 }
    $capSec = $drain + 90
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
    Write-Log "down kind=$Kind timeout=$Timeout ossctl-exit=$exit"
} else {
    Write-Host "$($script:OssDistro) distro not running; nothing to drain."
    Write-Log "down kind=${Kind}: distro not running"
}
if ($All) {
    & wsl.exe --shutdown | Out-Null
    Write-Host 'WSL stopped (wsl --shutdown)'
} else {
    & wsl.exe --terminate $script:OssDistro | Out-Null
    Write-Host "distro $($script:OssDistro) stopped (wsl --terminate)"
}
exit $exit
