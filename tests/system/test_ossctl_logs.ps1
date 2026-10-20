<#
.SYNOPSIS
  scripts\ossctl.ps1 logs (post-v1 item 6): the argv builder and one live, read-only journalctl call.
.DESCRIPTION
  1 Get-OssLogsArgv: the unit name maps to ossbot-<name>.service, -Since maps to journalctl's relative
    form, a '|' in -Grep stays inside one argument, and bad names, -Since, -Grep and -Lines are refused.
  2 Live (needs the distro running): `scripts\ossctl.ps1 logs gatekeeper -Since 5m -Grep "a|b"` exits 0
    and prints no bash error ("command not found", "syntax error").
#>
[CmdletBinding()]
param([switch]$NoLive)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '..\..\scripts\lib\common.ps1')
$repo = Get-OssRepoRoot
$script:Fail = 0
function Ok([string]$Name, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "PASS $Name $Detail" -ForegroundColor Green }
    else { Write-Host "FAIL $Name $Detail" -ForegroundColor Red; $script:Fail++ }
}
function Refused([scriptblock]$Call) {
    try { & $Call | Out-Null; return $false } catch { return $true }
}

Write-Host '== 1: Get-OssLogsArgv'
$a = Get-OssLogsArgv -Name gatekeeper -Since 15m -Grep 'Too Many Requests|429'
Ok 'exec without a shell' (($a[0..5] -join ' ') -eq '-d ossbot -u root --exec /usr/bin/journalctl') ($a -join ' ')
Ok 'unit' ($a -contains 'ossbot-gatekeeper.service')
Ok 'since 15m -> --since=-15min' ($a -contains '--since=-15min')
Ok 'the pipe stays one element' ($a[-1] -eq '--grep=Too Many Requests|429' -and @($a | Where-Object { $_ -eq '|' }).Count -eq 0)
Ok 'default lines 200, short-iso, no pager' (($a -contains '200') -and ($a -contains 'short-iso') -and ($a -contains '--no-pager'))
$b = Get-OssLogsArgv -Name hermes -Since 2h
Ok 'no grep: no --grep' (@($b | Where-Object { $_ -like '--grep*' }).Count -eq 0 -and ($b -contains '--since=-2h'))
Ok 'seconds and days' (((Get-OssLogsArgv -Name runner -Since 30s) -contains '--since=-30s') -and ((Get-OssLogsArgv -Name sandboxd -Since 1d) -contains '--since=-1d'))
foreach ($bad in @(
        @{ N = 'unknown name'; C = { Get-OssLogsArgv -Name proxy } },
        @{ N = 'name with a path'; C = { Get-OssLogsArgv -Name '../gatekeeper' } },
        @{ N = 'since without a unit'; C = { Get-OssLogsArgv -Name hermes -Since 15 } },
        @{ N = 'since with a shell word'; C = { Get-OssLogsArgv -Name hermes -Since '15m;id' } },
        @{ N = 'since too long'; C = { Get-OssLogsArgv -Name hermes -Since 12345m } },
        @{ N = 'grep with a double quote'; C = { Get-OssLogsArgv -Name hermes -Grep 'a"b' } },
        @{ N = 'grep with a newline'; C = { Get-OssLogsArgv -Name hermes -Grep "a`nb" } },
        @{ N = 'grep ending in a backslash'; C = { Get-OssLogsArgv -Name hermes -Grep 'a\' } },
        @{ N = 'grep over 200 characters'; C = { Get-OssLogsArgv -Name hermes -Grep ('x' * 201) } },
        @{ N = 'lines 0'; C = { Get-OssLogsArgv -Name hermes -Lines 0 } },
        @{ N = 'lines 5001'; C = { Get-OssLogsArgv -Name hermes -Lines 5001 } })) {
    Ok "refused: $($bad.N)" (Refused $bad.C)
}

if (-not $NoLive) {
    Write-Host '== 2: live, read-only'
    if (-not (Test-OssDistroRunning)) { Write-Host 'SKIP live call: the distro is not running' }
    else {
        $out = @(& powershell -NoProfile -File (Join-Path $repo 'scripts\ossctl.ps1') logs gatekeeper -Since 5m -Grep 'a|b' 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
        Ok 'logs gatekeeper -Grep "a|b" exits 0' ($code -eq 0) "(exit $code, $($out.Count) line(s))"
        Ok 'no bash error' (@($out | Where-Object { $_ -match 'command not found|syntax error|No such file' }).Count -eq 0)
        $bad = @(& powershell -NoProfile -File (Join-Path $repo 'scripts\ossctl.ps1') logs proxy 2>&1 | ForEach-Object { "$_" })
        Ok 'logs <unknown> exits 2' ($LASTEXITCODE -eq 2) ($bad -join ' ')
    }
}

if ($script:Fail) { Write-Host "test_ossctl_logs: $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'test_ossctl_logs: all checks passed' -ForegroundColor Green
exit 0
