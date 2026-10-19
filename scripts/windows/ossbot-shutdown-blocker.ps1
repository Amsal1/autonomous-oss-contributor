<#
.SYNOPSIS
  ossbot shutdown blocker (CR-001 6a, DESIGN 13.5, ADR-0021): stops ossbot gracefully before Windows
  shuts down, restarts or signs out.
.DESCRIPTION
  Started at logon by the second action of the 'ossbot-autostart' task (scripts\bootstrap.ps1). Compiles
  scripts\windows\ShutdownBlocker.cs at runtime (Add-Type; nothing is copied out of the repo) and runs
  one off-screen top-level window until the session ends. On WM_QUERYENDSESSION it registers
  "ossbot: stopping safely..." with ShutdownBlockReasonCreate, runs
  scripts\down.ps1 -Timeout <DownTimeout> -Caller blocker (shutdown type graceful), destroys the reason
  and lets the session end. One instance per session (mutex Local\ossbot-shutdown-blocker); a second
  start exits 0. Log: .ossbot-local\shutdown-blocker.log; state: .ossbot-local\shutdown-blocker.state.
  Stop it with scripts\bootstrap.ps1 -Uninstall -Only shutdownhooks (or close its window).
.PARAMETER DownTimeout
  Drain timeout passed to down.ps1 (default 90 s, DESIGN 13.5).
.PARAMETER KeepConsole
  Stay attached to the console (debugging only).
#>
[CmdletBinding()]
param(
    [ValidateRange(0, 600)][int]$DownTimeout = 90,
    [switch]$KeepConsole
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\lib\common.ps1')

$local = New-OssLocalDir
$log = Join-Path $local 'shutdown-blocker.log'
$state = Join-Path $local 'shutdown-blocker.state'
$down = Join-Path (Get-OssRepoRoot) 'scripts\down.ps1'
if (Test-OssBlockerRunning) {
    # Cheap check before compiling; Run() re-checks with the mutex itself.
    Write-OssLifecycleLog "shutdown blocker pid=${PID}: another instance runs; exiting"
    exit 0
}
if (-not ('Ossbot.ShutdownBlocker' -as [type])) {
    $src = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'ShutdownBlocker.cs'), [Text.Encoding]::UTF8)
    Add-Type -TypeDefinition $src -Language CSharp -ReferencedAssemblies @('System.Windows.Forms', 'System.Drawing')
}
exit ([Ossbot.ShutdownBlocker]::Run($down, $DownTimeout, $log, $state, [bool]$KeepConsole))
