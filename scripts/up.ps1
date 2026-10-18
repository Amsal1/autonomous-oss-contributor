<#
.SYNOPSIS
  Bring ossbot back up after scripts\down.ps1 or a laptop restart (CR-001).
.DESCRIPTION
  1. Refuses while a kill is active (.ossbot-local\KILL): kill overrides lifecycle; use resume.ps1.
  2. Starts the distro (systemd starts the units; the boot gate ends a distro-scope down).
  3. Runs `ossctl up` as root: starts the units, waits until they are healthy (-Timeout, default
     from policy.yaml lifecycle.up_health_timeout_s = 180 s), lifts draining/down, runs
     `doctor --quick` and sends "Back up: X resumed, Y awaiting approval" to Telegram.
  Not a resume: dry-run, pause and approvals are unchanged.
#>
[CmdletBinding()]
param([int]$Timeout = 0)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$marker = Join-Path (Get-OssRepoRoot) '.ossbot-local\KILL'
if (Test-Path -LiteralPath $marker) {
    Write-Warning 'ossbot is killed (.ossbot-local\KILL present): up refused. Use scripts\resume.ps1 first.'
    exit 3
}
$state = Start-OssDistro
if ($state -eq 'timeout') {
    Write-Warning "the $($script:OssDistro) distro did not finish booting in time"
    exit 1
}
Write-Host "distro $($script:OssDistro): systemd $state"
$cmd = @('/opt/ossbot/app/.venv/bin/ossctl', 'up')
if ($Timeout -gt 0) { $cmd += @('--timeout', "$Timeout") }
& wsl.exe -d $script:OssDistro -u root --exec @cmd
$code = $LASTEXITCODE
Add-Content -LiteralPath (Join-Path (New-OssLocalDir) 'lifecycle.log') -Value ("{0} up exit {1}" -f (Get-Date).ToUniversalTime().ToString('o'), $code)
exit $code
