<#
.SYNOPSIS
  Undo ossbot's host-side changes (DESIGN 7.7 step 5): restore %USERPROFILE%\.wslconfig from the
  newest backup (or delete it when the recorded state is 'none'), remove the 'ossbot-autostart'
  Scheduled Task, and remove the Hyper-V rules (elevated sessions only).
  Never touches the distro, its VHD or anything under .wsl\.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\wslconfig.ps1')

$path = Get-OssWslConfigPath
$local = Join-Path (Get-OssRepoRoot) '.ossbot-local'
$stateFile = Join-Path $local 'wslconfig.state'
$backup = Get-ChildItem -LiteralPath $local -Filter 'wslconfig.backup-*' -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if ($backup) {
    Copy-Item -LiteralPath $backup.FullName -Destination $path -Force
    Write-Host "restored $path from $($backup.Name)"
} elseif ((Test-Path -LiteralPath $stateFile) -and ((Get-Content -LiteralPath $stateFile -TotalCount 1) -eq 'none')) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force; Write-Host "deleted $path (state was 'none')" }
} else {
    Write-Host "no backup or state recorded; leaving $path unchanged"
}

if (Get-ScheduledTask -TaskName 'ossbot-autostart' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'ossbot-autostart' -Confirm:$false
    Write-Host 'removed Scheduled Task ossbot-autostart'
}

if (Test-OssElevated) {
    & powershell -NoProfile -File (Join-Path $PSScriptRoot 'hyperv-firewall.ps1') -Remove
} else {
    Write-Host 'not elevated: Hyper-V rules (if any) left in place; run elevated: scripts\hyperv-firewall.ps1 -Remove'
}
Write-Host 'WSL settings change on the next start: wsl --shutdown'
exit 0
