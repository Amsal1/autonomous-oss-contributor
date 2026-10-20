<#
.SYNOPSIS
  After the real Windows restart test (docs/FINAL_REPORT.md "Restart test" R1): print what the shutdown
  blocker (6a, drain channel) and the lifecycle wrote, whether the retired 6b task is still registered,
  plus status.ps1's "last shutdown".
#>
[CmdletBinding()]
param([int]$Lines = 25)
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$local = Join-Path $repo '.ossbot-local'
foreach ($name in 'shutdown-blocker.log', 'lifecycle.log') {
    $file = Join-Path $local $name
    Write-Output "== $name (last $Lines lines)"
    if (Test-Path -LiteralPath $file) { Get-Content -LiteralPath $file -Tail $Lines -Encoding UTF8 } else { Write-Output '(missing)' }
}
Write-Output '== shutdown-hook task (6b, retired: ADR-0021 addendum post-v1)'
$task = Get-ScheduledTask -TaskName 'ossbot-shutdown-hook' -ErrorAction SilentlyContinue
if ($task) {
    $info = $task | Get-ScheduledTaskInfo
    Write-Output ("still registered (scripts\bootstrap.ps1 -Only shutdownhooks removes it); last run {0}, result {1}" -f $info.LastRunTime, $info.LastTaskResult)
} else { Write-Output 'not registered (retired)' }
Write-Output '== status.ps1'
& powershell -NoProfile -File (Join-Path $repo 'scripts\status.ps1') | Where-Object { $_ -match '^(last shutdown|shutdown blocker|shutdown hook|autostart task|lifecycle|control)' }
