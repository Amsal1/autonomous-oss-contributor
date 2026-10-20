<#
.SYNOPSIS
  Shutdown blocker (CR-001 6a, PLAN item 40, ADR-0021 and its post-v1 addendum), live and
  non-destructive: Windows is not shut down; the test sends WM_QUERYENDSESSION to the blocker's own
  window. The real restart is the owner's check R1 (docs/FINAL_REPORT.md).
.DESCRIPTION
  The blocker is restarted first so the current ShutdownBlocker.cs runs (it is left running at the end,
  as the autostart task would have it).
  1 one visible, top-level, off-screen tool window (not in the taskbar); a second start exits 0 and
    leaves one instance; the drain channel comes up (state file and log: channel up, pong).
  2 WM_QUERYENDSESSION (SendMessageTimeout from a background job): while the drain runs the block
    reason is registered; the message returns TRUE only after the listener answered "drained"; the
    reason is destroyed afterwards; the log shows the channel path and no process launch (no
    "started pid", no "spawn refused"); lifecycle.log has the blocker's graceful down marked
    "(channel)"; the ossbot units are stopped. The blocker itself never terminates the distro; the test
    then ends it, as Windows ends the VM after a real drain.
  7 a channel of the test's own is killed right after "ack": the listener still stops the units and
    writes the marker (last shutdown graceful after up.ps1). -Only 1,2,... runs a subset.
  3 WM_ENDSESSION(FALSE) (the shutdown was cancelled): logged, the blocker keeps running.
  4 up.ps1 brings ossbot back; the channel reopens; the last shutdown type is graceful.
  5 the channel's wsl.exe is killed: the blocker logs "channel died" and "channel restarted" within 70 s.
  6 the distro is stopped (wsl --terminate): "channel skipped: distro stopped"; WM_QUERYENDSESSION
    then returns TRUE at once with "no channel"; up.ps1 and the channel reopens within 70 s.
  Telegram: the "Going down" / "Back up" summaries to the owner only (product code).
#>
[CmdletBinding()]
param([int]$DownTimeoutSec = 400, [string]$Only = '1,2,3,4,5,6,7')
$ErrorActionPreference = 'Continue'
$sections = @($Only -split '[,\s]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })  # -File may join "2,3" as "2 3"
. (Join-Path $PSScriptRoot '..\..\scripts\lib\common.ps1')
Add-OssWin32
$repo = Get-OssRepoRoot
$scripts = Join-Path $repo 'scripts'
$blkLog = Join-Path $repo '.ossbot-local\shutdown-blocker.log'
$lcLog = Join-Path $repo '.ossbot-local\lifecycle.log'
$script:Fail = 0

function Ok([string]$Name, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "PASS $Name $Detail" -ForegroundColor Green }
    else { Write-Host "FAIL $Name $Detail" -ForegroundColor Red; $script:Fail++ }
}
function Lines([string]$File) { if (Test-Path -LiteralPath $File) { return @(Get-Content -LiteralPath $File -Encoding UTF8) }; return @() }
function State { return Get-OssBlockerState }
function Prop([object]$Obj, [string]$Name) {
    if ($null -eq $Obj) { return $null }
    $p = $Obj.PSObject.Properties[$Name]; if ($p) { return $p.Value }; return $null
}
function Wait-Log([int]$From, [string]$Pattern, [int]$Seconds) {
    # Seconds until a new blocker log line matches, or -1.
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
        if (@(Lines $blkLog | Select-Object -Skip $From | Where-Object { $_ -match $Pattern }).Count) { return $sw.Elapsed.TotalSeconds }
        Start-Sleep -Milliseconds 500
    }
    return -1
}
function Send-QueryEndSession([IntPtr]$Hwnd, [int]$TimeoutMs) {
    return Start-Job -ArgumentList $Hwnd.ToInt64(), $TimeoutMs -ScriptBlock {
        param([long]$Hwnd, [int]$TimeoutMs)
        Add-Type -Namespace T -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, IntPtr w, IntPtr l, uint f, uint t, out IntPtr r);'
        $r = [IntPtr]::Zero
        $ok = [T.N]::SendMessageTimeout([IntPtr]$Hwnd, 0x0011, [IntPtr]::Zero, [IntPtr]::Zero, 0, [uint32]$TimeoutMs, [ref]$r)
        return @($ok.ToInt64(), $r.ToInt64())
    }
}
function Unit-Active([string]$Unit) {
    $r = Invoke-OssWsl -User root -Command @('/usr/bin/systemctl', 'is-active', $Unit)
    return (@($r.Out) -join '').Trim()
}

if ((Test-Path (Join-Path $repo '.ossbot-local\KILL')) -or -not (Test-OssDistroRunning)) {
    Write-Host 'precondition: the distro must run and not be killed (scripts\up.ps1 / resume.ps1 first)'
    exit 2
}

Write-Host '== 0: restart the blocker so the current code runs'
Ok 'running blocker closed' (Stop-OssBlocker)
$blkBefore = (Lines $blkLog).Count
Ok 'blocker started' (Start-OssBlocker)
$h = Find-OssBlockerWindow

if ($sections -contains 1) {
    Write-Host '== 1: the blocker, its window and the drain channel'
    Ok 'blocker window found' ($h -ne [IntPtr]::Zero) ('(0x{0:x})' -f $h.ToInt64())
    Ok 'window visible (WS_VISIBLE: Windows asks it, does not just end it)' ([OssbotWin32.Native]::IsWindowVisible($h))
    Ok 'window is top-level (no parent)' ([OssbotWin32.Native]::GetParent($h) -eq [IntPtr]::Zero)
    $ex = [OssbotWin32.Native]::GetWindowLong($h, -20)
    Ok 'tool window, not in the taskbar or Alt+Tab' ((($ex -band 0x80) -ne 0) -and (($ex -band 0x40000) -eq 0)) ('(exstyle 0x{0:x})' -f $ex)
    $rect = New-Object OssbotWin32.Native+RECT
    [void][OssbotWin32.Native]::GetWindowRect($h, [ref]$rect)
    Ok 'window off-screen' ($rect.Right -le 0 -and $rect.Bottom -le 0) "($($rect.Left),$($rect.Top))"
    $t = Wait-Log $blkBefore 'channel up pid=\d+ pong=up' 60
    Ok 'log: channel up with a pong' ($t -ge 0) ('({0:N1} s)' -f $t)
    $st = State
    Ok 'state file: idle, channel up with its pid' ((Prop $st 'phase') -eq 'idle' -and (Prop $st 'channel') -eq 'up' -and [int](Prop $st 'channel_pid') -gt 0) "($(Prop $st 'phase') $(Prop $st 'channel') $(Prop $st 'channel_pid'))"
    $p2 = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-File', (Join-Path $scripts 'windows\ossbot-shutdown-blocker.ps1')) -PassThru -WindowStyle Hidden
    $null = $p2.Handle
    Ok 'a second start exits' ($p2.WaitForExit(60000)) "(exit $($p2.ExitCode))"
    Ok 'the second start exits 0' ($p2.ExitCode -eq 0)
    Ok 'still the same window' ((Find-OssBlockerWindow) -eq $h)
}

if ($sections -contains 2) {
    Write-Host '== 2: WM_QUERYENDSESSION through the channel'
    $null = Wait-Log $blkBefore 'channel up pid=' 60
    $blkBefore = (Lines $blkLog).Count
    $lcBefore = (Lines $lcLog).Count
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $job = Send-QueryEndSession $h ($DownTimeoutSec * 1000)
    $sawStopping = $false
    for ($i = 0; $i -lt 120 -and -not $sawStopping; $i++) {
        Start-Sleep -Milliseconds 250
        $st = State
        if ((Prop $st 'phase') -eq 'stopping' -and (Prop $st 'reason_active') -eq $true) { $sawStopping = $true }
    }
    Ok 'while stopping: block reason registered (state file)' $sawStopping ('({0:N1} s)' -f $sw.Elapsed.TotalSeconds)
    Ok 'while stopping: the message has not returned yet' ($job.State -eq 'Running')
    $null = Wait-Job $job -Timeout ($DownTimeoutSec + 30)
    $res = @(Receive-Job $job)
    Remove-Job $job -Force
    $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    Ok 'WM_QUERYENDSESSION returned TRUE after the drain' ($res.Count -eq 2 -and $res[0] -ne 0 -and $res[1] -eq 1) "($($res -join '/'), $secs s)"
    $st = State
    Ok 'after: block reason destroyed (state file)' ((Prop $st 'phase') -eq 'released' -and (Prop $st 'reason_active') -eq $false) "($(Prop $st 'phase'), down exit $(Prop $st 'down_exit'))"
    Ok 'after: drain exit 0' ((Prop $st 'down_exit') -eq 0)
    $newBlk = @(Lines $blkLog | Select-Object -Skip $blkBefore)
    $newBlk | ForEach-Object { Write-Host "  blocker: $_" }
    Ok 'log: reason created and queried on the window thread' (@($newBlk | Where-Object { $_ -match "block reason created=True query='ossbot: stopping safely" }).Count -eq 1)
    Ok 'log: down sent on the channel' (@($newBlk | Where-Object { $_ -match 'channel: sent down 90 blocker' }).Count -eq 1)
    Ok 'log: ack and drained exit=0' ((@($newBlk | Where-Object { $_ -match 'channel: ack' }).Count -eq 1) -and (@($newBlk | Where-Object { $_ -match 'channel: drained exit=0 ' }).Count -eq 1))
    Ok 'log: no process launched during the query' (@($newBlk | Where-Object { $_ -match 'started pid|spawn refused|down\.ps1' }).Count -eq 0)
    Ok 'log: reason destroyed, query empty' (@($newBlk | Where-Object { $_ -match 'block reason destroyed=True query=none' }).Count -eq 1)
    $newLc = @(Lines $lcLog | Select-Object -Skip $lcBefore)
    $newLc | ForEach-Object { Write-Host "  lifecycle: $_" }
    Ok 'lifecycle.log: blocker channel down start' (@($newLc | Where-Object { $_ -match 'down kind=graceful caller=blocker pid=\d+: start timeout=90 \(channel\)' }).Count -eq 1)
    Ok 'lifecycle.log: ended exit 0' (@($newLc | Where-Object { $_ -match 'down kind=graceful caller=blocker .*: ossctl-exit=0' }).Count -eq 1 -and @($newLc | Where-Object { $_ -match 'down kind=graceful caller=blocker .*: end exit=0' }).Count -eq 1)
    Ok 'gatekeeper unit stopped' ((Unit-Active 'ossbot-gatekeeper.service') -ne 'active')
    Ok 'runner unit stopped' ((Unit-Active 'ossbot-runner.service') -ne 'active')
    # A real shutdown ends the VM after the drain; the boot gate turns the marker into last-shutdown
    # at the next distro start, so the test ends the VM the same way before up.ps1 (step 4).
    & wsl.exe --terminate $script:OssDistro | Out-Null
    Ok 'distro ended after the drain (as Windows ends the VM)' (-not (Test-OssDistroRunning))
}

if ($sections -contains 3) {
    Write-Host '== 3: WM_ENDSESSION(FALSE): the shutdown was cancelled'
    $blkBefore = (Lines $blkLog).Count
    $r = [IntPtr]::Zero
    [void][OssbotWin32.Native]::SendMessageTimeout($h, 0x0016, [IntPtr]::Zero, [IntPtr]::Zero, 0, 10000, [ref]$r)
    Start-Sleep -Milliseconds 500
    Ok 'log: cancelled session noted' (@(Lines $blkLog | Select-Object -Skip $blkBefore | Where-Object { $_ -match 'did not end' }).Count -eq 1)
    Ok 'blocker keeps running' ((Test-OssBlockerRunning) -and (Find-OssBlockerWindow) -eq $h)
    Ok 'state back to idle' ((Prop (State) 'phase') -eq 'idle')
}

if ($sections -contains 4) {
    Write-Host '== 4: up.ps1'
    $blkBefore = (Lines $blkLog).Count
    & powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller blockerTest | Out-Host
    Ok 'up.ps1 exit 0' ($LASTEXITCODE -eq 0)
    $t = Wait-Log $blkBefore 'channel up pid=\d+ pong=' 75
    Ok 'channel reopened after up (within 70 s)' ($t -ge 0) ('({0:N1} s)' -f $t)
    $out = @(& powershell -NoProfile -File (Join-Path $scripts 'status.ps1') 2>&1 | ForEach-Object { "$_" })
    $ls = @($out | Where-Object { $_ -match '^last shutdown\s+' }) | Select-Object -First 1
    Ok 'last shutdown graceful' ($ls -match '^last shutdown\s+graceful ') "($ls)"
    $bl = @($out | Where-Object { $_ -match '^shutdown blocker\s+' }) | Select-Object -First 1
    Ok 'status: blocker with channel up' ($bl -match 'channel up pid \d+') "($bl)"
}

if ($sections -contains 5) {
    Write-Host '== 5: the channel dies and is restarted'
    $cpid = [int](Prop (State) 'channel_pid')
    Ok 'channel pid known' ($cpid -gt 0) "($cpid)"
    $blkBefore = (Lines $blkLog).Count
    if ($cpid -gt 0) { Stop-Process -Id $cpid -Force -ErrorAction SilentlyContinue }
    $t1 = Wait-Log $blkBefore 'channel died' 10
    Ok 'log: channel died' ($t1 -ge 0) ('({0:N1} s)' -f $t1)
    $t2 = Wait-Log $blkBefore 'channel restarted pid=\d+' 70
    Ok 'log: channel restarted within 70 s' ($t2 -ge 0) ('({0:N1} s)' -f $t2)
    $t3 = Wait-Log $blkBefore 'channel up pid=\d+ pong=' 30
    Ok 'log: restarted channel answers' ($t3 -ge 0)
}

if ($sections -contains 6) {
    Write-Host '== 6: distro stopped: no channel, the query returns at once'
    $blkBefore = (Lines $blkLog).Count
    & powershell -NoProfile -File (Join-Path $scripts 'down.ps1') -Timeout 10 -Caller blockerTest | Out-Host
    Ok 'distro stopped by down.ps1' (-not (Test-OssDistroRunning))
    $t = Wait-Log $blkBefore 'channel skipped: distro stopped' 70
    Ok 'log: channel skipped: distro stopped' ($t -ge 0) ('({0:N1} s)' -f $t)
    $blkBefore = (Lines $blkLog).Count
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $job = Send-QueryEndSession $h 30000
    $null = Wait-Job $job -Timeout 40
    $res = @(Receive-Job $job)
    Remove-Job $job -Force
    Ok 'WM_QUERYENDSESSION returned TRUE at once' ($res.Count -eq 2 -and $res[1] -eq 1 -and $sw.Elapsed.TotalSeconds -lt 10) ('({0:N1} s)' -f $sw.Elapsed.TotalSeconds)
    Ok 'log: no channel, nothing to drain' (@(Lines $blkLog | Select-Object -Skip $blkBefore | Where-Object { $_ -match 'no channel \(distro stopped or listener dead\)' }).Count -eq 1)
    Ok 'distro not started by the blocker' (-not (Test-OssDistroRunning))
    $r = [IntPtr]::Zero
    [void][OssbotWin32.Native]::SendMessageTimeout($h, 0x0016, [IntPtr]::Zero, [IntPtr]::Zero, 0, 10000, [ref]$r)
    $blkBefore = (Lines $blkLog).Count
    & powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller blockerTest | Out-Host
    Ok 'up.ps1 exit 0' ($LASTEXITCODE -eq 0)
    $t = Wait-Log $blkBefore 'channel up pid=\d+ pong=' 75
    Ok 'channel reopened within 70 s of up' ($t -ge 0) ('({0:N1} s)' -f $t)
}

if ($sections -contains 7) {
    Write-Host '== 7: the drain finishes when the channel''s wsl.exe ends right after the ack'
    # R1's open risk: Windows may end the channel's wsl.exe before "drained". A channel of the test's own
    # (same argv as the blocker's) is killed right after "ack"; the listener must finish the drain.
    $psi = New-Object System.Diagnostics.ProcessStartInfo (Join-Path $env:WINDIR 'System32\wsl.exe')
    $psi.Arguments = "-d $($script:OssDistro) -u root --exec /opt/ossbot/app/.venv/bin/ossctl drain-listen"
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $p.StandardInput.Write("ping`n"); $p.StandardInput.Flush()
    $pong = $p.StandardOutput.ReadLine()
    Ok 'own channel answers ping' ($pong -match '^pong up$') "($pong)"
    $p.StandardInput.Write("down 30 blockerTest`n"); $p.StandardInput.Flush()
    $ack = $p.StandardOutput.ReadLine()
    Ok 'down acknowledged' ($ack -eq 'ack') "($ack)"
    Stop-Process -Id $p.Id -Force
    $t = -1
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 90) {
        if ((Unit-Active 'ossbot-gatekeeper.service') -ne 'active' -and (Unit-Active 'ossbot-runner.service') -ne 'active') { $t = $sw.Elapsed.TotalSeconds; break }
        Start-Sleep -Seconds 1
    }
    Ok 'the units still stopped after the wsl.exe was killed' ($t -ge 0) ('({0:N1} s)' -f $t)
    Start-Sleep -Seconds 3
    & wsl.exe --terminate $script:OssDistro | Out-Null
    & powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller blockerTest | Out-Host
    Ok 'up.ps1 exit 0' ($LASTEXITCODE -eq 0)
    $out = @(& powershell -NoProfile -File (Join-Path $scripts 'status.ps1') 2>&1 | ForEach-Object { "$_" })
    $ls = @($out | Where-Object { $_ -match '^last shutdown\s+' }) | Select-Object -First 1
    Ok 'the marker was written: last shutdown graceful' ($ls -match '^last shutdown\s+graceful ') "($ls)"
}

if ($script:Fail) { Write-Host "test_shutdown_blocker: $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'test_shutdown_blocker: all checks passed' -ForegroundColor Green
exit 0
