<#
.SYNOPSIS
  Shutdown-hook task (CR-001 6b), bootstrap shutdownhooks / -Uninstall / -Shortcuts (PLAN item 41,
  ADR-0021). Live and non-destructive: Windows is not shut down; the task is started by hand.
.DESCRIPTION
  1 definition: ossbot-shutdown-hook exists, registered by this non-elevated user, one trigger = the
    System log User32 1074 subscription (no sleep or Kernel-Power event), RunLevel Limited, InteractiveToken,
    PT2M, the action runs down.ps1 -Timeout 20 -Kind hook from the repo.
  2 Start-ScheduledTask ossbot-shutdown-hook: the distro stops, lifecycle.log has the hook's down, the
    task ends with result 0; up.ps1 then shows last shutdown 'hook'.
  3 bootstrap.ps1 -Uninstall -Only shutdownhooks removes the hook task, the blocker and its autostart
    action (the autostart task stays); bootstrap.ps1 -Only shutdownhooks puts all back; a second run
    prints only unchanged.
  4 bootstrap.ps1 -Only shortcuts -Shortcuts creates the four Start-menu shortcuts (second run
    unchanged); bootstrap.ps1 -Uninstall -Only shortcuts removes them again.
.PARAMETER Cases
  Comma-separated subset, e.g. -Cases 1,2.
#>
[CmdletBinding()]
param([string]$Cases = '1,2,3,4')
$ErrorActionPreference = 'Continue'
$Selected = @($Cases -split ',' | ForEach-Object { [int]$_.Trim() })
. (Join-Path $PSScriptRoot '..\..\scripts\lib\common.ps1')
$repo = Get-OssRepoRoot
$scripts = Join-Path $repo 'scripts'
$bootstrap = Join-Path $scripts 'bootstrap.ps1'
$lcLog = Join-Path $repo '.ossbot-local\lifecycle.log'
$script:Fail = 0

function Ok([string]$Name, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "PASS $Name $Detail" -ForegroundColor Green }
    else { Write-Host "FAIL $Name $Detail" -ForegroundColor Red; $script:Fail++ }
}
function Lines([string]$File) { if (Test-Path -LiteralPath $File) { return @(Get-Content -LiteralPath $File) }; return @() }
function Boot([string[]]$ArgList) {
    $out = @(& powershell -NoProfile -File $bootstrap @ArgList 2>&1 | ForEach-Object { "$_" })
    $out | ForEach-Object { Write-Host "  $_" }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out }
}
function Wait-Until([scriptblock]$Cond, [int]$Seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Seconds) { if (& $Cond) { return $sw.Elapsed.TotalSeconds }; Start-Sleep -Milliseconds 500 }
    return -1
}

if ((Test-Path (Join-Path $repo '.ossbot-local\KILL')) -or -not (Test-OssDistroRunning)) {
    Write-Host 'precondition: the distro must run and not be killed (scripts\up.ps1 / resume.ps1 first)'
    exit 2
}

if ($Selected -contains 1) {
    Write-Host '== 1: task definition'
    $t = Get-ScheduledTask -TaskName $script:OssHookTask -ErrorAction SilentlyContinue
    Ok 'ossbot-shutdown-hook registered' ($null -ne $t)
    if ($t) {
        Ok 'matches the spec (trigger, action, run level, time limit)' (@(Get-OssHookProblems -Task $t).Count -eq 0) ((@(Get-OssHookProblems -Task $t)) -join '; ')
        $sub = [string]@($t.Triggers)[0].Subscription
        Ok 'trigger: System log, User32, 1074 only' ($sub -match "Path=.System." -and $sub -match "@Name='User32'" -and $sub -match 'EventID=1074' -and $sub -notmatch 'Kernel-Power|EventID=42')
        Ok 'non-elevated principal (RunLevel Limited, InteractiveToken)' ([string]$t.Principal.RunLevel -eq 'Limited' -and [string]$t.Principal.LogonType -eq 'Interactive')
        Ok 'registered without elevation (this session is not elevated)' (-not (Test-OssElevated))
        Ok 'time limit PT2M' ([string]$t.Settings.ExecutionTimeLimit -eq 'PT2M')
        Ok 'action: down.ps1 -Timeout 20 -Kind hook from the repo' ([string]@($t.Actions)[0].Arguments -match [regex]::Escape((Join-Path $repo 'scripts\down.ps1')) -and [string]@($t.Actions)[0].Arguments -match '-Timeout 20 -Kind hook')
    }
}

if ($Selected -contains 2) {
    Write-Host '== 2: Start-ScheduledTask ossbot-shutdown-hook'
    $before = (Lines $lcLog).Count
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Start-ScheduledTask -TaskName $script:OssHookTask
    $t1 = Wait-Until { -not (Test-OssDistroRunning) } 150
    Ok 'distro stopped by the hook task' ($t1 -ge 0) ('({0:N1} s)' -f $t1)
    $t2 = Wait-Until { (Get-ScheduledTask -TaskName $script:OssHookTask).State -eq 'Ready' } 150
    $info = Get-ScheduledTaskInfo -TaskName $script:OssHookTask
    Ok 'task finished with result 0' ($t2 -ge 0 -and $info.LastTaskResult -eq 0) ('(0x{0:x}, {1:N1} s)' -f $info.LastTaskResult, $sw.Elapsed.TotalSeconds)
    $new = @(Lines $lcLog | Select-Object -Skip $before)
    $new | ForEach-Object { Write-Host "  $_" }
    Ok 'lifecycle.log: the hook down ended exit 0' (@($new | Where-Object { $_ -match 'down kind=hook caller=hook .*end exit=0' }).Count -eq 1)
    & powershell -NoProfile -File (Join-Path $scripts 'up.ps1') -Caller hookTest | Out-Host
    Ok 'up.ps1 exit 0' ($LASTEXITCODE -eq 0)
    $st = @(& powershell -NoProfile -File (Join-Path $scripts 'status.ps1') 2>&1 | ForEach-Object { "$_" })
    $ls = @($st | Where-Object { $_ -match '^last shutdown\s+' }) | Select-Object -First 1
    Ok 'last shutdown: hook' ($ls -match '^last shutdown\s+hook ') "($ls)"
}

if ($Selected -contains 3) {
    Write-Host '== 3: bootstrap -Uninstall -Only shutdownhooks, then -Only shutdownhooks'
    $r = Boot @('-Uninstall', '-Only', 'shutdownhooks')
    Ok 'uninstall exit 0' ($r.Code -eq 0)
    Ok 'hook task removed' ($null -eq (Get-ScheduledTask -TaskName $script:OssHookTask -ErrorAction SilentlyContinue))
    Ok 'blocker closed' (-not (Test-OssBlockerRunning))
    $a = Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue
    Ok 'autostart kept with only its distro-boot action' ($a -and @($a.Actions).Count -eq 1 -and @(Get-OssAutostartProblems -Task $a -WithoutBlocker).Count -eq 0)
    $r = Boot @('-Only', 'shutdownhooks')
    Ok 'install exit 0' ($r.Code -eq 0)
    Ok 'hook task back and as specified' (@(Get-OssHookProblems -Task (Get-ScheduledTask -TaskName $script:OssHookTask -ErrorAction SilentlyContinue)).Count -eq 0)
    Ok 'autostart has the blocker action again' (@(Get-OssAutostartProblems -Task (Get-ScheduledTask -TaskName $script:OssAutostartTask)).Count -eq 0)
    Ok 'blocker running again' (Test-OssBlockerRunning)
    $r = Boot @('-Only', 'shutdownhooks')
    $changed = @($r.Out | Where-Object { $_ -match '^step \S+: changed' })
    Ok 'second run: only unchanged' ($r.Code -eq 0 -and $changed.Count -eq 0) ($changed -join '; ')
}

if ($Selected -contains 4) {
    Write-Host '== 4: bootstrap -Shortcuts and -Uninstall -Only shortcuts'
    $hadBefore = @(Get-OssShortcutSpecs | Where-Object { Test-Path -LiteralPath $_.Path }).Count
    $r = Boot @('-Only', 'shortcuts', '-Shortcuts')
    Ok 'shortcuts exit 0' ($r.Code -eq 0)
    $specs = @(Get-OssShortcutSpecs)
    Ok 'four shortcuts exist' (@($specs | Where-Object { Test-Path -LiteralPath $_.Path }).Count -eq 4)
    $shell = New-Object -ComObject WScript.Shell
    $kill = $shell.CreateShortcut(($specs | Where-Object { $_.Path -like '*Kill.lnk' }).Path)
    Ok 'Kill runs scripts\kill.ps1 with Windows PowerShell' ($kill.TargetPath -like '*WindowsPowerShell\v1.0\powershell.exe' -and $kill.Arguments -match 'kill\.ps1')
    $r = Boot @('-Only', 'shortcuts', '-Shortcuts')
    Ok 'second run unchanged' (@($r.Out | Where-Object { $_ -match '^step shortcuts: unchanged' }).Count -eq 1)
    $r = Boot @('-Only', 'shortcuts')
    Ok "phase shortcuts refused without -Shortcuts" ($r.Code -eq 2)
    if ($hadBefore -eq 0) {
        $r = Boot @('-Uninstall', '-Only', 'shortcuts')
        Ok 'uninstall shortcuts exit 0' ($r.Code -eq 0)
        Ok 'shortcuts and their folder removed' (-not (Test-Path -LiteralPath (Get-OssShortcutDir)))
    }
}

if ($script:Fail) { Write-Host "test_shutdown_hook_task: $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'test_shutdown_hook_task: all checks passed' -ForegroundColor Green
exit 0
