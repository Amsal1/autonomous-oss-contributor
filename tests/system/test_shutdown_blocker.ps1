<#
.SYNOPSIS
  Shutdown blocker (CR-001 6a, PLAN item 40, ADR-0021), live and non-destructive: Windows is not shut
  down; the test sends WM_QUERYENDSESSION to the blocker's own window. The real restart is FEAT-011 item 57.
.DESCRIPTION
  1 the blocker runs (started here when it does not) with one visible, top-level, off-screen tool
    window (not in the taskbar); a second start exits 0 and leaves one instance.
  2 WM_QUERYENDSESSION (SendMessageTimeout from a background job): while down.ps1 runs the blocker
    reports the block reason registered (ShutdownBlockReasonQuery on its own thread, state file and
    log); the message returns TRUE only after down.ps1 finished; the reason is destroyed afterwards;
    lifecycle.log has the blocker's graceful down; the distro is stopped.
  3 WM_ENDSESSION(FALSE) (the shutdown was cancelled): logged, the blocker keeps running.
  4 up.ps1 brings ossbot back; the last shutdown type is graceful.
  A blocker started by the test is closed again at the end. Telegram: the "Going down" / "Back up"
  summaries to the owner only (product code).
#>
[CmdletBinding()]
param([int]$DownTimeoutSec = 400)
$ErrorActionPreference = 'Continue'
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

if ((Test-Path (Join-Path $repo '.ossbot-local\KILL')) -or -not (Test-OssDistroRunning)) {
    Write-Host 'precondition: the distro must run and not be killed (scripts\up.ps1 / resume.ps1 first)'
    exit 2
}

Write-Host '== 1: the blocker and its window'
$startedByTest = $false
if (-not (Test-OssBlockerRunning)) {
    $startedByTest = Start-OssBlocker
    Ok 'blocker started by the test' $startedByTest
}
$h = Find-OssBlockerWindow
Ok 'blocker window found' ($h -ne [IntPtr]::Zero) ('(0x{0:x})' -f $h.ToInt64())
Ok 'window visible (WS_VISIBLE: Windows asks it, does not just end it)' ([OssbotWin32.Native]::IsWindowVisible($h))
Ok 'window is top-level (no parent)' ([OssbotWin32.Native]::GetParent($h) -eq [IntPtr]::Zero)
$ex = [OssbotWin32.Native]::GetWindowLong($h, -20)
Ok 'tool window, not in the taskbar or Alt+Tab' ((($ex -band 0x80) -ne 0) -and (($ex -band 0x40000) -eq 0)) ('(exstyle 0x{0:x})' -f $ex)
$rect = New-Object OssbotWin32.Native+RECT
[void][OssbotWin32.Native]::GetWindowRect($h, [ref]$rect)
Ok 'window off-screen' ($rect.Right -le 0 -and $rect.Bottom -le 0) "($($rect.Left),$($rect.Top))"
$st = State
Ok 'state file: idle with the window handle' ((Prop $st 'phase') -eq 'idle' -and (Prop $st 'hwnd') -eq ('0x{0:x}' -f $h.ToInt64())) "($(Prop $st 'phase') $(Prop $st 'hwnd'))"
$p2 = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-File', (Join-Path $scripts 'windows\ossbot-shutdown-blocker.ps1')) -PassThru -WindowStyle Hidden
$null = $p2.Handle
Ok 'a second start exits' ($p2.WaitForExit(60000)) "(exit $($p2.ExitCode))"
Ok 'the second start exits 0' ($p2.ExitCode -eq 0)
Ok 'still the same window' ((Find-OssBlockerWindow) -eq $h)

Write-Host '== 2: WM_QUERYENDSESSION'
$blkBefore = (Lines $blkLog).Count
$lcBefore = (Lines $lcLog).Count
$sw = [Diagnostics.Stopwatch]::StartNew()
$job = Start-Job -ArgumentList $h.ToInt64(), ($DownTimeoutSec * 1000) -ScriptBlock {
    param([long]$Hwnd, [int]$TimeoutMs)
    Add-Type -Namespace T -Name N -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, IntPtr w, IntPtr l, uint f, uint t, out IntPtr r);'
    $r = [IntPtr]::Zero
    $ok = [T.N]::SendMessageTimeout([IntPtr]$Hwnd, 0x0011, [IntPtr]::Zero, [IntPtr]::Zero, 0, [uint32]$TimeoutMs, [ref]$r)
    return @($ok.ToInt64(), $r.ToInt64())
}
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
Ok 'WM_QUERYENDSESSION returned TRUE after down.ps1' ($res.Count -eq 2 -and $res[0] -ne 0 -and $res[1] -eq 1) "($($res -join '/'), $secs s)"
$st = State
Ok 'after: block reason destroyed (state file)' ((Prop $st 'phase') -eq 'released' -and (Prop $st 'reason_active') -eq $false) "($(Prop $st 'phase'), down exit $(Prop $st 'down_exit'))"
Ok 'after: down.ps1 exit 0' ((Prop $st 'down_exit') -eq 0)
$newBlk = @(Lines $blkLog | Select-Object -Skip $blkBefore)
$newBlk | ForEach-Object { Write-Host "  blocker: $_" }
Ok 'log: reason created and queried on the window thread' (@($newBlk | Where-Object { $_ -match "block reason created=True query='ossbot: stopping safely" }).Count -eq 1)
Ok 'log: reason destroyed, query empty' (@($newBlk | Where-Object { $_ -match 'block reason destroyed=True query=none' }).Count -eq 1)
$newLc = @(Lines $lcLog | Select-Object -Skip $lcBefore)
Ok 'lifecycle.log: graceful down by the blocker ended exit 0' (@($newLc | Where-Object { $_ -match 'down kind=graceful caller=blocker .*end exit=0' }).Count -eq 1)
Ok 'distro stopped' (-not (Test-OssDistroRunning))

Write-Host '== 3: WM_ENDSESSION(FALSE): the shutdown was cancelled'
$blkBefore = (Lines $blkLog).Count
$r = [IntPtr]::Zero
[void][OssbotWin32.Native]::SendMessageTimeout($h, 0x0016, [IntPtr]::Zero, [IntPtr]::Zero, 0, 10000, [ref]$r)
Start-Sleep -Milliseconds 500
Ok 'log: cancelled session noted' (@(Lines $blkLog | Select-Object -Skip $blkBefore | Where-Object { $_ -match 'did not end \(cancelled\)' }).Count -eq 1)
Ok 'blocker keeps running' ((Test-OssBlockerRunning) -and (Find-OssBlockerWindow) -eq $h)
Ok 'state back to idle' ((Prop (State) 'phase') -eq 'idle')

Write-Host '== 4: up.ps1'
& powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller blockerTest | Out-Host
Ok 'up.ps1 exit 0' ($LASTEXITCODE -eq 0)
$out = @(& powershell -NoProfile -File (Join-Path $scripts 'status.ps1') 2>&1 | ForEach-Object { "$_" })
$ls = @($out | Where-Object { $_ -match '^last shutdown\s+' }) | Select-Object -First 1
Ok 'last shutdown graceful' ($ls -match '^last shutdown\s+graceful ') "($ls)"

if ($startedByTest) { Ok 'blocker started by the test closed again' (Stop-OssBlocker) }
if ($script:Fail) { Write-Host "test_shutdown_blocker: $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'test_shutdown_blocker: all checks passed' -ForegroundColor Green
exit 0
