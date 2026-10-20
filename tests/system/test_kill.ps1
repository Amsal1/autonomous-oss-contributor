<#
.SYNOPSIS
  Kill switch end to end from Windows (DESIGN 22.6 test_kill.ps1, acceptance criterion 9).
.DESCRIPTION
  Cases (each ends with a resume and a fully running system):
    1 soft   ossctl kill (the API path Telegram /kill uses): sandbox containers gone <= 10 s,
             sandboxd stopped, gatekeeper answers /status and refuses outbound with 423.
    2 soft+reboot  wsl --shutdown + start: killed.level soft, gatekeeper + proxy active in killed mode,
             sandboxd inactive.
    3 full   kill.ps1: containers gone <= 10 s, gatekeeper + proxy stopped, filter/firewall/watchdog
             up, the filter refuses a create; after a restart: level full, gatekeeper + proxy skipped
             by ExecCondition; resume.ps1 starts gatekeeper, then proxy, then sandboxd.
    4 stopped kill.ps1 with the distro stopped writes only the marker; the next boot is killed (full).
    5 hung   kill.ps1 with the in-distro kill hanging (test hook) returns in ~40 s via wsl --terminate.
    6 hard   kill.ps1 -Hard leaves WSL stopped and the autostart task disabled; resume.ps1 restores.
  Telegram itself is not configured in phase 1; /kill and ossctl kill call the same gatekeeper
  ControlService.kill (soft), which the unit tests drive through the Telegram handlers.
.PARAMETER Cases
  Comma-separated subset, e.g. -Cases 1,3 (a string, so it also works with powershell -File).
#>
[CmdletBinding()]
param([string]$Cases = '1,2,3,4,5,6')
$ErrorActionPreference = 'Continue'
$Selected = @($Cases -split ',' | ForEach-Object { [int]$_.Trim() })
. (Join-Path $PSScriptRoot '..\..\scripts\lib\common.ps1')
$repo = Get-OssRepoRoot
$scripts = Join-Path $repo 'scripts'
$ossctl = '/opt/ossbot/app/.venv/bin/ossctl'
$script:Fail = 0
$Units = @('ossbot-boot-gate', 'ossbot-firewall', 'ossbot-firewall-docker', 'ossbot-dockerfilter',
    'ossbot-gatekeeper', 'ossbot-proxy', 'ossbot-sandboxd', 'ossbot-runner', 'ossbot-hermes',
    'ossbot-hermes-probe.socket', 'ossbot-watchdog.timer')

function Ok([string]$Name, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "PASS $Name $Detail" -ForegroundColor Green }
    else { Write-Host "FAIL $Name $Detail" -ForegroundColor Red; $script:Fail++ }
}
function Root([string[]]$Cmd) { return Invoke-OssWsl -User root -Command $Cmd }
function Active([string]$Unit) { return ((Root @('/usr/bin/systemctl', 'is-active', $Unit)).Out -join '').Trim() }
function Level {
    $r = Root @('/usr/bin/jq', '-r', '.level', '/var/lib/ossbot/control/killed')
    if ($r.Code -ne 0) { return 'none' }
    return ($r.Out -join '').Trim()
}
function Wait-Until([scriptblock]$Cond, [int]$Seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
        if (& $Cond) { return $sw.Elapsed.TotalSeconds }
        Start-Sleep -Milliseconds 500
    }
    return -1
}
function All-Active { foreach ($u in $Units) { if ((Active $u) -ne 'active') { return $false } }; return $true }
function Sandbox-Count {
    $r = Root @('/usr/bin/docker', 'ps', '-q', '--filter', 'label=ossbot.sandbox=1')
    return @($r.Out | Where-Object { $_ -match '^[0-9a-f]+$' }).Count
}
function Start-Dummy {
    # A labelled sandbox-like container (root, real socket: test setup only) for the <= 10 s check.
    $digest = ((Root @('/usr/bin/awk', '/^  sandbox_full:/{f=1} f && /digest:/{gsub(/"/,"",$2); print $2; exit}', '/etc/ossbot/versions.lock.yaml')).Out -join '').Trim()
    $name = "ossbot-killtest-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $r = Root @('/usr/bin/docker', 'run', '-d', '--rm', '--name', $name, '--network', 'none', '--label', 'ossbot.sandbox=1',
        '--label', 'ossbot.profile=probe', '--label', 'ossbot.cid=killtest', '--label', 'ossbot.run=killtest',
        "ossbot/sandbox-full@$digest", 'sleep', '300')
    Ok 'dummy sandbox container started' ($r.Code -eq 0) (($r.Out -join ' ').Trim())
}
function Outbound-Status {
    # A worker's outbound-action request must be refused with 423 while killed.
    $body = '{"cid":"c_0000000000","kind":"comment","target_number":1,"body":"kill test"}'
    $r = Root @('/usr/sbin/runuser', '-u', 'ossrunner', '--', '/usr/bin/curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
        '--unix-socket', '/run/ossbot/gatekeeper.sock', '-X', 'POST', '-H', 'content-type: application/json',
        '--data', $body, 'http://ossbot/v1/actions')
    return ($r.Out -join '').Trim()
}
function Filter-Create {
    $r = Root @('/usr/sbin/runuser', '-u', 'ossbox', '--', '/usr/bin/curl', '-s', '--unix-socket', '/run/ossbot/docker-filter.sock',
        '-X', 'POST', '-H', 'content-type: application/json', '--data', '{}', 'http://d/v1.44/containers/create?name=ossbot-killtest')
    return ($r.Out -join ' ')
}
function Resume-All([string]$Case) {
    & powershell -NoProfile -File (Join-Path $scripts 'resume.ps1') -AssumeYes | Out-Host
    $t = Wait-Until { All-Active } 120
    Ok "$Case resume: every unit active" ($t -ge 0) ("({0:N1} s)" -f $t)
    Ok "$Case resume: flag removed" ((Level) -eq 'none')
    Ok "$Case resume: marker removed" (-not (Test-Path (Join-Path $repo '.ossbot-local\KILL')))
}
function Restart-Distro { & wsl.exe --shutdown | Out-Null; Start-Sleep -Seconds 8; return Start-OssDistro }

# Preconditions.
Start-OssDistro | Out-Null
if ((Level) -ne 'none' -or (Test-Path (Join-Path $repo '.ossbot-local\KILL'))) { Resume-All 'pre' }
if (-not (All-Active)) { Write-Host 'ossbot units are not all active; provision first'; exit 2 }

if ($Selected -contains 1) {
    Write-Host '== case 1: soft kill (ossctl kill / Telegram /kill path)'
    Start-Dummy
    $r = Root @($ossctl, 'kill', '--reason', 'test_kill_soft')
    $t = Wait-Until { (Sandbox-Count) -eq 0 } 10
    Ok 'case1 sandbox containers gone <= 10 s' ($t -ge 0) ("({0:N1} s)" -f $t)
    Ok 'case1 level soft' ((Level) -eq 'soft')
    $t = Wait-Until { (Active 'ossbot-sandboxd') -ne 'active' } 15
    Ok 'case1 sandboxd stopped' ($t -ge 0)
    Ok 'case1 gatekeeper still active (killed mode)' ((Active 'ossbot-gatekeeper') -eq 'active')
    Ok 'case1 proxy still active' ((Active 'ossbot-proxy') -eq 'active')
    $st = Root @($ossctl, 'status')
    Ok 'case1 /status answers: killed' ((($st.Out -join ' ') -match '"state": "killed"'))
    Ok 'case1 outbound refused 423' ((Outbound-Status) -eq '423') (Outbound-Status)
    Ok 'case1 filter refuses a create' ((Filter-Create) -match 'killed') (Filter-Create)
    if ($Selected -contains 2) {
        Write-Host '== case 2: soft kill survives a reboot'
        Restart-Distro | Out-Null
        $t = Wait-Until { (Active 'ossbot-gatekeeper') -eq 'active' -and (Active 'ossbot-proxy') -eq 'active' } 90
        Ok 'case2 gatekeeper + proxy active after reboot' ($t -ge 0)
        Ok 'case2 level soft after reboot' ((Level) -eq 'soft')
        Ok 'case2 sandboxd inactive' ((Active 'ossbot-sandboxd') -ne 'active')
        # 'active' comes a moment before the API socket answers after a boot: poll the answer
        $t = Wait-Until { ((Root @($ossctl, 'status')).Out -join ' ') -match '"state": "killed"' } 30
        Ok 'case2 /status answers: killed' ($t -ge 0) ("({0:N1} s)" -f $t)
    }
    Resume-All 'case1'
}

if ($Selected -contains 3) {
    Write-Host '== case 3: kill.ps1 (full)'
    Start-Dummy
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & powershell -NoProfile -File (Join-Path $scripts 'kill.ps1') -Reason 'test_kill_full' | Out-Host
    $code = $LASTEXITCODE
    Ok 'case3 kill.ps1 exit 0' ($code -eq 0) "(exit $code, $([math]::Round($sw.Elapsed.TotalSeconds, 1)) s)"
    Ok 'case3 sandbox containers gone' ((Sandbox-Count) -eq 0)
    Ok 'case3 marker written' (Test-Path (Join-Path $repo '.ossbot-local\KILL'))
    Ok 'case3 level full' ((Level) -eq 'full')
    foreach ($u in 'ossbot-gatekeeper', 'ossbot-proxy', 'ossbot-sandboxd') { Ok "case3 $u stopped" ((Active $u) -ne 'active') }
    foreach ($u in 'ossbot-dockerfilter', 'ossbot-firewall', 'ossbot-firewall-docker', 'ossbot-watchdog.timer') { Ok "case3 $u still active" ((Active $u) -eq 'active') }
    Ok 'case3 filter refuses a create' ((Filter-Create) -match 'killed')
    Restart-Distro | Out-Null
    Start-Sleep -Seconds 10
    Ok 'case3 level full after reboot' ((Level) -eq 'full')
    foreach ($u in 'ossbot-boot-gate', 'ossbot-firewall', 'ossbot-firewall-docker', 'ossbot-dockerfilter', 'ossbot-watchdog.timer') { Ok "case3 reboot: $u active" ((Active $u) -eq 'active') }
    foreach ($u in 'ossbot-gatekeeper', 'ossbot-proxy', 'ossbot-sandboxd') { Ok "case3 reboot: $u not active" ((Active $u) -ne 'active') }
    $cond = (Root @('/usr/bin/journalctl', '-b', '-u', 'ossbot-gatekeeper', '--no-pager', '-o', 'cat')).Out -join ' '
    Ok 'case3 gatekeeper skipped by ExecCondition' ($cond -match 'level full|ExecCondition|skipped|condition')
    Ok 'case3 reboot: filter refuses a create' ((Filter-Create) -match 'killed')
    Resume-All 'case3'
    # Resume order: gatekeeper, then proxy, then sandboxd (ActiveEnterTimestampMonotonic).
    $ts = @{}
    foreach ($u in 'ossbot-gatekeeper', 'ossbot-proxy', 'ossbot-sandboxd') {
        $v = ((Root @('/usr/bin/systemctl', 'show', '-P', 'ActiveEnterTimestampMonotonic', $u)).Out -join '').Trim()
        $ts[$u] = [int64]$v
    }
    Ok 'case3 resume order gatekeeper < proxy < sandboxd' ($ts['ossbot-gatekeeper'] -lt $ts['ossbot-proxy'] -and $ts['ossbot-proxy'] -lt $ts['ossbot-sandboxd']) ("($($ts['ossbot-gatekeeper']) / $($ts['ossbot-proxy']) / $($ts['ossbot-sandboxd']))")
    $log = (Root @('/usr/bin/tail', '-n', '1', '/srv/ossbot/logs/resume.log')).Out -join ''
    Ok 'case3 resume.log has the offline resume line' ($log -match 'resume.ps1')
}

if ($Selected -contains 4) {
    Write-Host '== case 4: kill.ps1 with the distro stopped'
    & wsl.exe --terminate $script:OssDistro | Out-Null
    Start-Sleep -Seconds 3
    & powershell -NoProfile -File (Join-Path $scripts 'kill.ps1') -Reason 'test_kill_stopped' | Out-Host
    Ok 'case4 distro was not started by kill.ps1' (-not (Test-OssDistroRunning))
    Ok 'case4 marker written' (Test-Path (Join-Path $repo '.ossbot-local\KILL'))
    Start-OssDistro | Out-Null
    $t = Wait-Until { (Level) -eq 'full' } 30
    Ok 'case4 next boot is killed at level full (boot gate)' ($t -ge 0)
    Ok 'case4 gatekeeper not started' ((Active 'ossbot-gatekeeper') -ne 'active')
    Resume-All 'case4'
}

if ($Selected -contains 5) {
    Write-Host '== case 5: hung in-distro kill'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & powershell -NoProfile -File (Join-Path $scripts 'kill.ps1') -Reason 'test_kill_hung' -TestHangSeconds 120 | Out-Host
    $secs = $sw.Elapsed.TotalSeconds
    # 30 s timeout (N8) + wsl --terminate, which takes about 10 s with runner and Hermes running (measured
    # 10.1 s on 2026-10-19, FEAT-008; it was about 7 s before FEAT-006/007)
    Ok 'case5 kill.ps1 returned within ~40 s' ($secs -le 45) ("({0:N1} s, exit $LASTEXITCODE)" -f $secs)
    Ok 'case5 distro terminated' (-not (Test-OssDistroRunning))
    Start-OssDistro | Out-Null
    $t = Wait-Until { (Level) -eq 'full' } 30
    Ok 'case5 next boot is killed at level full' ($t -ge 0)
    Resume-All 'case5'
}

if ($Selected -contains 6) {
    Write-Host '== case 6: kill.ps1 -Hard'
    & powershell -NoProfile -File (Join-Path $scripts 'kill.ps1') -Hard -Reason 'test_kill_hard' | Out-Host
    Start-Sleep -Seconds 3
    Ok 'case6 WSL stopped (no running distro)' (@(Get-OssDistros -Running).Count -eq 0)
    $task = Get-ScheduledTask -TaskName 'ossbot-autostart' -ErrorAction SilentlyContinue
    if ($task) { Ok 'case6 autostart task disabled' (Test-OssTaskDisabled -Task $task) }
    Resume-All 'case6'
    $task = Get-ScheduledTask -TaskName 'ossbot-autostart' -ErrorAction SilentlyContinue
    if ($task) { Ok 'case6 autostart task re-enabled' (-not (Test-OssTaskDisabled -Task $task)) }
}

if ($script:Fail) { Write-Host "test_kill: $($script:Fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'test_kill: all checks passed' -ForegroundColor Green
exit 0
