<#
.SYNOPSIS
  Windows front door for ossctl (DESIGN 18.3).
.DESCRIPTION
  ossctl.ps1 doctor [--quick] [--probes]   Windows checks (.wslconfig hash, Hyper-V firewall,
                                           localhost-forwarding probe, autostart task, kill marker),
                                           then the distro doctor as root.
  ossctl.ps1 <anything else>               runs /opt/ossbot/app/.venv/bin/ossctl <args> as ossadmin
                                           (operator role; TTY confirms happen in this console).
#>
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\wslconfig.ps1')
$ossctl = '/opt/ossbot/app/.venv/bin/ossctl'
$cmdArgs = @($args)

if ($cmdArgs.Count -eq 0 -or $cmdArgs[0] -ne 'doctor') {
    & wsl.exe -d $script:OssDistro -u ossadmin --exec $ossctl @cmdArgs
    exit $LASTEXITCODE
}

$results = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Status, [string]$Name, [string]$Detail, [string]$Hint = '') {
    $results.Add([pscustomobject]@{ Status = $Status; Name = $Name; Detail = $Detail; Hint = $Hint })
}

# .wslconfig hash vs the tracked template (7.7).
$cfg = Get-OssWslConfigState
if ($cfg.Have -eq $cfg.Want) { Add-Check 'PASS' '.wslconfig' "matches config/wsl/wslconfig ($($cfg.Want.Substring(0, 12)))" }
elseif ($cfg.Have -eq 'none') { Add-Check 'FAIL' '.wslconfig' 'missing' 'powershell -File scripts\bootstrap.ps1 -Only wsl' }
else { Add-Check 'WARN' '.wslconfig' 'drift from config/wsl/wslconfig' 'powershell -File scripts\bootstrap.ps1 -Only wsl' }

# Hyper-V firewall (7.8): optional, elevated. P1-16: exactly the two ossbot rules with the required
# properties, and no other enabled Allow rule for the WSL VM (outbound = FAIL, inbound = WARN).
$wslId = $script:OssHyperVCreatorId
$hvHint = 'optional: elevated powershell -File scripts\hyperv-firewall.ps1'
try {
    $hv = Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name $wslId -ErrorAction Stop
    $all = @(Get-NetFirewallHyperVRule -ErrorAction Stop)
    $drift = New-Object System.Collections.Generic.List[string]
    foreach ($spec in $script:OssHyperVRules) {
        $found = @($all | Where-Object { $_.Name -eq $spec.Name })
        if ($found.Count -ne 1) { $drift.Add("$($spec.Name): $($found.Count) rule(s)"); continue }
        foreach ($p in (Get-OssHyperVRuleProblems -Rule $found[0] -Port $spec.Port)) { $drift.Add("$($spec.Name): $p") }
    }
    $foreign = @(Get-OssHyperVForeignAllows -AllRules $all)
    $foreignOut = @($foreign | Where-Object { [string]$_.Direction -eq 'Outbound' })
    $foreignIn = @($foreign | Where-Object { [string]$_.Direction -ne 'Outbound' })
    $applied = $hv.DefaultOutboundAction -eq 'Block' -and $hv.DefaultInboundAction -eq 'Block'
    if (-not $applied) {
        Add-Check 'WARN' 'hyper-v firewall' "not applied (inbound $($hv.DefaultInboundAction), outbound $($hv.DefaultOutboundAction))" $hvHint
    } elseif ($drift.Count -gt 0) {
        Add-Check 'FAIL' 'hyper-v firewall' ("ossbot rules drifted: " + ($drift -join '; ')) 'elevated powershell -File scripts\hyperv-firewall.ps1 (re-creates them)'
    } elseif ($foreignOut.Count -gt 0) {
        Add-Check 'FAIL' 'hyper-v firewall' ("other outbound Allow rule(s) for the WSL VM: " + (($foreignOut | ForEach-Object { $_.Name }) -join ', ')) 'remove them (Remove-NetFirewallHyperVRule -Name <name>, elevated)'
    } elseif ($foreignIn.Count -gt 0) {
        Add-Check 'WARN' 'hyper-v firewall' ("other inbound Allow rule(s) for the WSL VM: " + (($foreignIn | ForEach-Object { $_.Name }) -join ', ')) 'remove them unless you added them on purpose'
    } else {
        Add-Check 'PASS' 'hyper-v firewall' 'inbound Block, outbound Block + exactly the ossbot TCP 80/443 rules (Outbound, Allow, Enabled, WSL VM)'
    }
} catch {
    $why = if (Test-OssElevated) { $_.Exception.Message } else { 'not elevated' }
    Add-Check 'WARN' 'hyper-v firewall' "not checked ($why)" $hvHint
}

# Localhost forwarding probe (7.6): a distro 127.0.0.1 listener must not be reachable from Windows.
$port = 18642
$listener = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $script:OssDistro, '-u', 'ossadmin', '--exec', 'timeout', '20', 'python3', '-m', 'http.server', '--bind', '127.0.0.1', "$port") -WindowStyle Hidden -PassThru
$inside = $false
for ($i = 0; $i -lt 20 -and -not $inside; $i++) {
    Start-Sleep -Milliseconds 500
    $r = Invoke-OssWsl -User ossadmin -Command @('python3', '-c', "import socket; socket.create_connection(('127.0.0.1', $port), 1).close()")
    $inside = ($r.Code -eq 0)
}
$reached = $false
$client = New-Object System.Net.Sockets.TcpClient
try { $t = $client.ConnectAsync('127.0.0.1', $port); if ($t.Wait(3000) -and $client.Connected) { $reached = $true } } catch { } finally { $client.Close() }
try { if (-not $listener.HasExited) { $listener.Kill() } } catch { }
if ($inside -and -not $reached) { Add-Check 'PASS' 'localhost forwarding' 'distro 127.0.0.1:18642 not reachable from Windows' }
elseif (-not $inside) { Add-Check 'FAIL' 'localhost forwarding' 'probe listener did not start in the distro' }
else { Add-Check 'FAIL' 'localhost forwarding' 'distro listener reachable from Windows localhost' 'check localhostForwarding=false in .wslconfig' }

# Autostart task (7.6) with the shutdown blocker action (CR-001 6a), the blocker process, the
# shutdown-hook task (6b) and Fast Startup (ADR-0021), then the kill marker.
$task = Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue
$autoProblems = @(Get-OssAutostartProblems -Task $task)
if (-not $task) { Add-Check 'FAIL' 'autostart task' 'ossbot-autostart missing' 'powershell -File scripts\bootstrap.ps1 -Only autostart' }
elseif ($task.State -eq 'Disabled') { Add-Check 'WARN' 'autostart task' 'disabled (kill.ps1 -Hard?)' 'powershell -File scripts\resume.ps1' }
elseif ($autoProblems.Count) { Add-Check 'WARN' 'autostart task' ('drift: ' + ($autoProblems -join '; ')) 'powershell -File scripts\bootstrap.ps1 -Only autostart' }
else { Add-Check 'PASS' 'autostart task' "ossbot-autostart ($($task.State)): distro boot + shutdown blocker actions, no time limit" }
if (Test-OssBlockerRunning) {
    $bst = Get-OssBlockerState
    $bpid = if ($bst -and $bst.PSObject.Properties['pid']) { " pid $($bst.pid)" } else { '' }
    $win = Find-OssBlockerWindow
    if ($win -ne [IntPtr]::Zero -and [OssbotWin32.Native]::IsWindowVisible($win)) { Add-Check 'PASS' 'shutdown blocker' "running$bpid, window visible off-screen (6a)" }
    else { Add-Check 'WARN' 'shutdown blocker' "running$bpid but its window is missing or hidden (cannot block)" 'powershell -File scripts\bootstrap.ps1 -Uninstall -Only shutdownhooks, then -Only shutdownhooks' }
} else {
    Add-Check 'WARN' 'shutdown blocker' 'not running (starts at logon)' 'powershell -File scripts\bootstrap.ps1 -Only shutdownhooks'
}
$hook = Get-ScheduledTask -TaskName $script:OssHookTask -ErrorAction SilentlyContinue
$hookProblems = @(Get-OssHookProblems -Task $hook)
if (-not $hook) { Add-Check 'FAIL' 'shutdown hook task' "$($script:OssHookTask) missing" 'powershell -File scripts\bootstrap.ps1 -Only shutdownhooks' }
elseif ($hook.State -eq 'Disabled') { Add-Check 'WARN' 'shutdown hook task' 'disabled' 'powershell -File scripts\bootstrap.ps1 -Only shutdownhooks' }
elseif ($hookProblems.Count) { Add-Check 'WARN' 'shutdown hook task' ('drift: ' + ($hookProblems -join '; ')) 'powershell -File scripts\bootstrap.ps1 -Only shutdownhooks' }
else { Add-Check 'PASS' 'shutdown hook task' "$($script:OssHookTask) ($($hook.State)): System User32 1074 only, non-elevated, PT2M (6b)" }
$hiber = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -ErrorAction SilentlyContinue)
$fast = if ($hiber) { [int]$hiber.HiberbootEnabled } else { -1 }
Add-Check 'PASS' 'fast startup' $(if ($fast -eq 1) { 'on: Shut down logs the session off first, so the blocker is still asked (ADR-0021)' } elseif ($fast -eq 0) { 'off' } else { 'unknown (registry value absent)' })
$marker = Join-Path (Get-OssRepoRoot) '.ossbot-local\KILL'
if (Test-Path -LiteralPath $marker) { Add-Check 'WARN' 'kill marker' '.ossbot-local\KILL present (killed at level full)' 'powershell -File scripts\resume.ps1' }
else { Add-Check 'PASS' 'kill marker' 'absent' }

Write-Host '== Windows part'
foreach ($r in $results) {
    $line = '{0,-15} {1}: {2}' -f $r.Status, $r.Name, $r.Detail
    if ($r.Hint -and $r.Status -ne 'PASS') { $line += "  (fix: $($r.Hint))" }
    $color = switch ($r.Status) { 'PASS' { 'Green' } 'WARN' { 'DarkYellow' } 'FAIL' { 'Red' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $color
}
Write-Host '== Distro part (as root)'
$rest = @()
if ($cmdArgs.Count -gt 1) { $rest = $cmdArgs[1..($cmdArgs.Count - 1)] }
& wsl.exe -d $script:OssDistro -u root --exec $ossctl doctor @rest
$distroCode = $LASTEXITCODE
$winFail = @($results | Where-Object { $_.Status -eq 'FAIL' }).Count
if ($winFail -gt 0 -or $distroCode -ne 0) { exit 1 }
exit 0
