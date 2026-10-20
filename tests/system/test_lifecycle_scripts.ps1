<#
.SYNOPSIS
  CR-001 Windows lifecycle scripts end to end (PLAN item 39, DESIGN 13.5). Live, no Windows restart.
.DESCRIPTION
  1 status.ps1 on the running system prints every CR-001 field with a real value.
  2 two down.ps1 started together (-Timeout 10) serialise on Local\ossbot-lifecycle: both exit 0,
    their start/end lines in .ossbot-local\lifecycle.log do not interleave, the second one logged
    that it waited and found the distro already stopped; the distro ends stopped.
  3 status.ps1 with the distro stopped still prints every field (the in-distro ones say so).
  4 up.ps1 exits 0; status.ps1 then shows lifecycle up and last shutdown graceful.
  5 up.ps1 while another holder keeps the lock gives up after -LockTimeout (exit 1), logged.
  0 (first, no distro needed) status.ps1 -StatusJsonFile renders unclean / graceful / none / missing
    last-shutdown records from their own fields (an unclean record is not "ossctl status gave no data").
    -StatusOnly stops after it.
  The "Going down" / "Back up" summaries go to the configured Telegram owner only (product code).
#>
[CmdletBinding()]
param([switch]$StatusOnly)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '..\..\scripts\lib\common.ps1')
$repo = Get-OssRepoRoot
$scripts = Join-Path $repo 'scripts'
$logFile = Join-Path $repo '.ossbot-local\lifecycle.log'
$script:Fail = 0
$Labels = @('distro', 'control', 'lifecycle', 'dry-run', 'egress mode', 'hermes tier', 'running work', 'approvals',
    'outbox', 'telegram poll', 'last shutdown', 'autostart task', 'shutdown blocker', 'shutdown hook', 'kill marker',
    'last lifecycle')
$InDistro = @('control', 'lifecycle', 'dry-run', 'egress mode', 'hermes tier', 'running work', 'approvals', 'outbox',
    'telegram poll', 'last shutdown')

function Ok([string]$Name, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "PASS $Name $Detail" -ForegroundColor Green }
    else { Write-Host "FAIL $Name $Detail" -ForegroundColor Red; $script:Fail++ }
}
function Status-Lines {
    return @(& powershell -NoProfile -File (Join-Path $scripts 'status.ps1') 2>&1 | ForEach-Object { "$_" })
}
function Value([string[]]$Lines, [string]$Label) {
    foreach ($l in $Lines) { if ($l -match ('^' + [regex]::Escape($Label) + '\s{2,}(\S.*)$')) { return $Matches[1] } }
    return $null
}
function Log-Lines { if (Test-Path -LiteralPath $logFile) { return @(Get-Content -LiteralPath $logFile) }; return @() }
function Start-Ps([string[]]$ArgList) {
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $ArgList -PassThru -WindowStyle Hidden
    $null = $p.Handle  # keeps ExitCode readable after the exit
    return $p
}

Write-Host '== 0: status.ps1 renders the last-shutdown record from its own fields (-StatusJsonFile)'
$tmp = New-OssLocalDir 'tmp'
$cases = @(
    @{ Json = '{"state": "running", "last_shutdown": {"type": "unclean", "marker_ts": "", "detected_at": "2026-10-20T14:48:03Z"}}'
       Want = '^unclean \(no shutdown marker: ossctl down did not run; detected 2026-10-20T14:48:03Z\)$' },
    @{ Json = '{"state": "running", "last_shutdown": {"type": "graceful", "marker_ts": "2026-10-20T14:46:30Z", "detected_at": "2026-10-20T14:48:03Z"}}'
       Want = '^graceful \(marker 2026-10-20T14:46:30Z, detected 2026-10-20T14:48:03Z\)$' },
    @{ Json = '{"state": "running", "last_shutdown": {"type": "none", "marker_ts": "", "detected_at": "2026-10-20T14:48:03Z"}}'
       Want = '^none recorded \(first start\)$' },
    @{ Json = '{"state": "running", "last_shutdown": null}'; Want = '^unknown \(ossctl status gave no data\)$' }
)
$n = 0
foreach ($c in $cases) {
    $n++
    $f = Join-Path $tmp "status-case-$n.json"
    [IO.File]::WriteAllText($f, $c.Json, (New-Object System.Text.UTF8Encoding $false))
    $lines0 = @(& powershell -NoProfile -File (Join-Path $scripts 'status.ps1') -StatusJsonFile $f 2>&1 | ForEach-Object { "$_" })
    $v = Value $lines0 'last shutdown'
    Ok "case ${n}: last shutdown rendered" ($v -match $c.Want) "($v)"
    Remove-Item -LiteralPath $f -Force
}
if ($StatusOnly) {
    if ($script:Fail) { Write-Host "test_lifecycle_scripts (-StatusOnly): $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
    Write-Host 'test_lifecycle_scripts (-StatusOnly): all checks passed' -ForegroundColor Green
    exit 0
}

if ((Test-Path (Join-Path $repo '.ossbot-local\KILL')) -or -not (Test-OssDistroRunning)) {
    Write-Host 'precondition: the distro must run and not be killed (scripts\up.ps1 / resume.ps1 first)'
    exit 2
}

Write-Host '== 1: status.ps1 on the running system (after the -StatusJsonFile cases above)'
$st = Status-Lines
$st | ForEach-Object { Write-Host "  $_" }
foreach ($l in $Labels) { Ok "status shows '$l'" ($null -ne (Value $st $l)) }
foreach ($l in $InDistro) {
    $v = Value $st $l
    Ok "status '$l' has a value while running" ($null -ne $v -and $v -notmatch 'distro stopped|unknown') "($v)"
}
Ok 'status: lifecycle up' ((Value $st 'lifecycle') -eq 'up')

Write-Host '== 2: two concurrent down.ps1 serialise'
$before = (Log-Lines).Count
$down = Join-Path $scripts 'down.ps1'
$sw = [Diagnostics.Stopwatch]::StartNew()
$pa = Start-Ps @('-NoProfile', '-File', $down, '-Timeout', '10', '-Caller', 'lsA')
Start-Sleep -Milliseconds 400
$pb = Start-Ps @('-NoProfile', '-File', $down, '-Timeout', '10', '-Caller', 'lsB')
$doneA = $pa.WaitForExit(300000); $doneB = $pb.WaitForExit(300000)
$secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
Ok 'both downs finished' ($doneA -and $doneB) "($secs s)"
Ok 'down A exit 0' ($pa.ExitCode -eq 0) "(exit $($pa.ExitCode))"
Ok 'down B exit 0' ($pb.ExitCode -eq 0) "(exit $($pb.ExitCode))"
Ok 'distro stopped after the downs' (-not (Test-OssDistroRunning))
$new = @(Log-Lines | Select-Object -Skip $before)
$events = @()
foreach ($l in $new) {
    if ($l -match 'down kind=\S+ caller=(ls[AB]) pid=\d+: (start|end)\b') { $events += "$($Matches[1]):$($Matches[2])" }
}
Write-Host "  lifecycle.log events: $($events -join ', ')"
$serial = $events.Count -eq 4 -and ($events[0] -replace ':start', '') -eq ($events[1] -replace ':end', '') -and
    $events[0] -like '*:start' -and $events[1] -like '*:end' -and $events[2] -like '*:start' -and $events[3] -like '*:end' -and
    ($events[2] -replace ':start', '') -eq ($events[3] -replace ':end', '')
Ok 'start/end of the two downs do not interleave' $serial
$second = if ($events.Count -ge 3) { $events[2] -replace ':start', '' } else { 'lsB' }
Ok 'the second down waited for the lock' (@($new | Where-Object { $_ -match "caller=$second .*waiting for the lifecycle lock" }).Count -eq 1)
Ok 'the second down found the distro stopped' (@($new | Where-Object { $_ -match "caller=$second .*distro not running" }).Count -eq 1)

Write-Host '== 3: status.ps1 with the distro stopped'
$st = Status-Lines
$st | ForEach-Object { Write-Host "  $_" }
foreach ($l in $Labels) { Ok "stopped: status shows '$l'" ($null -ne (Value $st $l)) }
Ok 'stopped: status says the distro is stopped' ((Value $st 'distro') -match 'stopped')
Ok 'stopped: status did not start the distro' (-not (Test-OssDistroRunning))

Write-Host '== 4: up.ps1'
$sw = [Diagnostics.Stopwatch]::StartNew()
& powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller lsUp | Out-Host
$code = $LASTEXITCODE
Ok 'up.ps1 exit 0' ($code -eq 0) ("(exit $code, {0:N1} s)" -f $sw.Elapsed.TotalSeconds)
$st = Status-Lines
$st | ForEach-Object { Write-Host "  $_" }
Ok 'after up: lifecycle up' ((Value $st 'lifecycle') -eq 'up')
Ok 'after up: last shutdown graceful' ((Value $st 'last shutdown') -match '^graceful ')

Write-Host '== 5: up.ps1 gives up while another run holds the lock'
$held = Enter-OssLifecycleLock -TimeoutSec 5
Ok 'test holds the lifecycle lock' ($null -ne $held)
$before = (Log-Lines).Count
& powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller lsWait -LockTimeout 3 | Out-Host
$code = $LASTEXITCODE
Exit-OssLifecycleLock $held
Ok 'up.ps1 exit 1 when the lock stays taken' ($code -eq 1) "(exit $code)"
Ok 'the give-up is logged' (@(Log-Lines | Select-Object -Skip $before | Where-Object { $_ -match 'caller=lsWait .*lock not free' }).Count -eq 1)

if ($script:Fail) { Write-Host "test_lifecycle_scripts: $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'test_lifecycle_scripts: all checks passed' -ForegroundColor Green
exit 0
