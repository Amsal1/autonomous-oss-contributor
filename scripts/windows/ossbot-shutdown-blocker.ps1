<#
.SYNOPSIS
  ossbot shutdown blocker (CR-001 6a, DESIGN 13.5, ADR-0021): stops ossbot gracefully before Windows
  shuts down, restarts or signs out.
.DESCRIPTION
  Started at logon by the second action of the 'ossbot-autostart' task (scripts\bootstrap.ps1). Compiles
  scripts\windows\ShutdownBlocker.cs at runtime (Add-Type; nothing is copied out of the repo) and runs
  one off-screen top-level window until the session ends.
  At start it opens the drain channel: one long-lived, hidden
  `wsl.exe -d ossbot -u root --exec /opt/ossbot/app/.venv/bin/ossctl drain-listen` child with its
  stdin/stdout kept as pipes, checked every 60 s and restarted while the distro runs (it never starts
  the distro). On WM_QUERYENDSESSION it registers "ossbot: stopping safely..." with
  ShutdownBlockReasonCreate, writes "down <DownTimeout> blocker" to the channel, waits for "drained"
  (shutdown type graceful), destroys the reason and lets the session end. No process is started at
  shutdown time: Windows refuses to initialise one in an ending session (ADR-0021 addendum post-v1).
  One instance per session (mutex Local\ossbot-shutdown-blocker); a second start exits 0. Log:
  .ossbot-local\shutdown-blocker.log; state: .ossbot-local\shutdown-blocker.state; the drain's
  start/end lines go to .ossbot-local\lifecycle.log like down.ps1's.
  Stop it with scripts\bootstrap.ps1 -Uninstall -Only shutdownhooks (or close its window).
.PARAMETER DownTimeout
  Drain timeout sent on the channel (default 90 s, DESIGN 13.5; the listener accepts 0..120).
.PARAMETER KeepConsole
  Stay attached to the console (debugging only).
#>
[CmdletBinding()]
param(
    [ValidateRange(0, 120)][int]$DownTimeout = 90,
    [switch]$KeepConsole
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\lib\common.ps1')

$local = New-OssLocalDir
$log = Join-Path $local 'shutdown-blocker.log'
$state = Join-Path $local 'shutdown-blocker.state'
$lifecycle = Join-Path $local 'lifecycle.log'
$listener = '/opt/ossbot/app/.venv/bin/ossctl'
if (Test-OssBlockerRunning) {
    # Cheap check before compiling; Run() re-checks with the mutex itself.
    Write-OssLifecycleLog "shutdown blocker pid=${PID}: another instance runs; exiting"
    exit 0
}
if (-not ('Ossbot.ShutdownBlocker' -as [type])) {
    $src = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'ShutdownBlocker.cs'), [Text.Encoding]::UTF8)
    Add-Type -TypeDefinition $src -Language CSharp -ReferencedAssemblies @('System.Windows.Forms', 'System.Drawing')
}
exit ([Ossbot.ShutdownBlocker]::Run($script:OssDistro, $listener, $DownTimeout, $log, $state, $lifecycle, [bool]$KeepConsole))
