# Shared helpers for the ossbot Windows scripts (PowerShell 5.1 compatible). Dot-source only:
#   . (Join-Path $PSScriptRoot 'lib\common.ps1')
# Everything stays inside the repo folder except the three documented host exceptions (NFR-3).

Set-StrictMode -Version 2.0

# $PSScriptRoot is this file's directory (scripts\lib) even when dot-sourced.
$script:OssRepo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:OssLocal = Join-Path $script:OssRepo '.ossbot-local'
$script:OssLinuxRepo = '/mnt/d/Work/autonomous-oss-contributor'
if (-not (Get-Variable -Name OssDistro -Scope Script -ErrorAction SilentlyContinue)) {
    $script:OssDistro = 'ossbot'
}

# wsl.exe prints UTF-16 for its own messages unless WSL_UTF8 is set; scripts stream UTF-8 to bash.
$env:WSL_UTF8 = '1'
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Get-OssRepoRoot { return $script:OssRepo }

function Write-OssStep {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Status, [string]$Detail = '')
    $line = "step ${Name}: $Status"
    if ($Detail) { $line += " ($Detail)" }
    $color = switch -Regex ($Status) {
        '^changed' { 'Yellow' } '^unchanged' { 'Green' } '^PASS' { 'Green' }
        '^WARN' { 'DarkYellow' } '^FAIL' { 'Red' } default { 'Gray' }
    }
    Write-Host $line -ForegroundColor $color
}

function New-OssLocalDir {
    param([string]$Sub = '')
    $dir = if ($Sub) { Join-Path $script:OssLocal $Sub } else { $script:OssLocal }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return $dir
}

function Test-OssElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Hyper-V firewall (DESIGN 7.8, ADR-0013, Phase 1 review P1-16): exactly these two ossbot rules.
$script:OssHyperVCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'   # WSL VMCreatorId
$script:OssHyperVRules = @(
    @{ Name = 'ossbot-out-https'; DisplayName = 'ossbot: WSL outbound TCP 443'; Port = 443 },
    @{ Name = 'ossbot-out-http'; DisplayName = 'ossbot: WSL outbound TCP 80 (apt)'; Port = 80 }
)

function Get-OssHyperVRuleProblems {
    # How one Hyper-V rule differs from its ossbot spec (Outbound, Allow, Enabled, TCP, the port,
    # the WSL VMCreatorId). An empty list means the rule is as required.
    param([Parameter(Mandatory)][object]$Rule, [Parameter(Mandatory)][int]$Port)
    $p = New-Object System.Collections.Generic.List[string]
    if ([string]$Rule.Direction -ne 'Outbound') { $p.Add("direction $($Rule.Direction)") }
    if ([string]$Rule.Action -ne 'Allow') { $p.Add("action $($Rule.Action)") }
    if ([string]$Rule.Enabled -ne 'True') { $p.Add("enabled $($Rule.Enabled)") }
    if (@('TCP', '6') -notcontains [string]$Rule.Protocol) { $p.Add("protocol $($Rule.Protocol)") }
    $ports = (@($Rule.RemotePorts) | ForEach-Object { [string]$_ }) -join ','
    if ($ports -ne [string]$Port) { $p.Add("remote ports $ports") }
    if ([string]$Rule.VMCreatorId -ne $script:OssHyperVCreatorId) { $p.Add("VMCreatorId $($Rule.VMCreatorId)") }
    # Unrolled on purpose: callers wrap the call in @(), so an empty result has Count 0.
    return $p.ToArray()
}

function Get-OssHyperVForeignAllows {
    # Enabled Allow rules for the WSL VMCreatorId that are not the two ossbot rules. WSL's own inbound
    # defaults (WslCore-Allow-Inbound-*: ICMP and mDNS from the host) are left out; an outbound one
    # widens the VM egress layer.
    param([object[]]$AllRules)
    $names = @($script:OssHyperVRules | ForEach-Object { $_.Name })
    return @($AllRules | Where-Object {
        [string]$_.VMCreatorId -eq $script:OssHyperVCreatorId -and [string]$_.Action -eq 'Allow' -and
        [string]$_.Enabled -eq 'True' -and $names -notcontains $_.Name -and
        -not ([string]$_.Direction -eq 'Inbound' -and [string]$_.Name -like 'WslCore-Allow-Inbound-*')
    })
}

function ConvertFrom-OssWslText {
    # wsl.exe management output may still contain NULs (UTF-16 read as bytes); strip them.
    param([object[]]$Lines)
    $out = @()
    foreach ($l in @($Lines)) {
        if ($null -eq $l) { continue }
        $s = ([string]$l) -replace "`0", ''
        $out += $s.TrimEnd("`r")
    }
    return $out
}

function Get-OssDistros {
    param([switch]$Running)
    $wslArgs = @('-l', '-q')
    if ($Running) { $wslArgs = @('-l', '--running', '-q') }
    $ErrorActionPreference = 'Continue'
    $raw = & wsl.exe @wslArgs 2>$null
    return @(ConvertFrom-OssWslText $raw | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-OssDistroRunning {
    param([string]$Distro = $script:OssDistro)
    return @(Get-OssDistros -Running) -contains $Distro
}

function Invoke-OssWsl {
    # Run one command in the distro with explicit user, args passed intact via --exec.
    # Returns @{ Code; Out } ; -Quiet suppresses echoing.
    param(
        [string]$User = 'root',
        [Parameter(Mandatory)][string[]]$Command,
        [string]$Distro = $script:OssDistro,
        [string]$Cd = ''
    )
    $ErrorActionPreference = 'Continue'
    # Windows PowerShell 5.1 does not escape embedded double quotes on native command lines, so
    # wsl.exe would mangle such args (JSON bodies, awk programs): pass them base64-encoded through
    # a streamed one-line script instead.
    if (@($Command | Where-Object { $_ -match '"' }).Count) {
        $parts = foreach ($a in $Command) {
            $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($a))
            '"$(printf %s ' + $b64 + ' | base64 -d)"'
        }
        $line = ''
        if ($Cd) { $line = "cd '$Cd' && " }
        $line += 'exec ' + ($parts -join ' ') + " </dev/null`n"
        return Invoke-OssStreamCapture -Text $line -User $User -Distro $Distro
    }
    $wslArgs = @('-d', $Distro, '-u', $User)
    if ($Cd) { $wslArgs += @('--cd', $Cd) }
    $wslArgs += '--exec'
    $wslArgs += $Command
    $out = & wsl.exe @wslArgs 2>&1
    $code = $LASTEXITCODE
    return [pscustomobject]@{ Code = $code; Out = @(ConvertFrom-OssWslText $out) }
}

function Get-OssScriptText {
    # Read a repo shell script (or several, concatenated) with CR stripped, for streaming.
    param([Parameter(Mandatory)][string[]]$Paths)
    $parts = @()
    foreach ($p in $Paths) {
        $full = if ([IO.Path]::IsPathRooted($p)) { $p } else { Join-Path $script:OssRepo $p }
        $parts += ([IO.File]::ReadAllText($full, [Text.Encoding]::UTF8) -replace "`r", '')
    }
    return ($parts -join "`n")
}

function Set-OssStdinEncoding {
    # Process.StandardInput is a StreamWriter over Console.InputEncoding with AutoFlush on, so it
    # writes that encoding's preamble at Process.Start. With code page 65001 (UTF-8 with BOM, which
    # the console switches to after some wsl.exe runs) bash then reads a BOM before '#!'. A
    # preamble-free UTF-8 encoding avoids it.
    try { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
}

function Invoke-OssStdin {
    # Run `wsl.exe <args>` with Text written to its stdin as raw UTF-8 bytes (no BOM, LF only).
    # Piping a string into a native command lets PowerShell 5.1 pick the encoding, which can
    # prepend a BOM to the script. -Capture returns @{ Code; Out } (stdout+stderr); otherwise the
    # child inherits this console's output handles and only the exit code is returned.
    param([Parameter(Mandatory)][string]$Text, [Parameter(Mandatory)][string[]]$WslArgs, [switch]$Capture)
    $quoted = foreach ($a in $WslArgs) {
        if ($a -eq '') { '""' } elseif ($a -match '[\s"]') { '"' + ($a -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' } else { $a }
    }
    Set-OssStdinEncoding
    $psi = New-Object System.Diagnostics.ProcessStartInfo 'wsl.exe'
    $psi.Arguments = ($quoted -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    if ($Capture) {
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    }
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $null; $outTask = $null
    if ($Capture) { $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync() }
    $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes(($Text -replace "`r", ''))
    $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $p.StandardInput.BaseStream.Flush()
    $p.StandardInput.Close()
    $p.WaitForExit()
    if (-not $Capture) { return [pscustomobject]@{ Code = $p.ExitCode; Out = @() } }
    $text = $outTask.Result + $errTask.Result
    return [pscustomobject]@{ Code = $p.ExitCode; Out = @(ConvertFrom-OssWslText ($text -split "`n")) }
}

function Invoke-OssStream {
    # Stream a script over stdin into `bash -s -- <args>` as the given user (DESIGN §7.2, N4).
    # Output goes straight to this console; returns the exit code.
    param(
        [Parameter(Mandatory)][string]$Text,
        [string[]]$Arguments = @(),
        [string]$User = 'root',
        [string]$Distro = $script:OssDistro
    )
    $wslArgs = @('-d', $Distro, '-u', $User, '--exec', 'bash', '-s', '--') + $Arguments
    return (Invoke-OssStdin -Text $Text -WslArgs $wslArgs).Code
}

function Invoke-OssStreamCapture {
    # Like Invoke-OssStream but returns @{ Code; Out } instead of printing.
    param(
        [Parameter(Mandatory)][string]$Text,
        [string[]]$Arguments = @(),
        [string]$User = 'root',
        [string]$Distro = $script:OssDistro
    )
    $wslArgs = @('-d', $Distro, '-u', $User, '--exec', 'bash', '-s', '--') + $Arguments
    return Invoke-OssStdin -Text $Text -WslArgs $wslArgs -Capture
}
function Start-OssDistro {
    # Boot the distro (systemd starts the ossbot units unless killed) and wait for systemd.
    param([string]$Distro = $script:OssDistro, [int]$TimeoutSec = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-OssWsl -Distro $Distro -User root -Command @('/bin/true')
        if ($r.Code -eq 0) { break }
        Start-Sleep -Seconds 2
    }
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-OssWsl -Distro $Distro -User root -Command @('systemctl', 'is-system-running')
        $state = ($r.Out -join '').Trim()
        if ($state -in @('running', 'degraded')) { return $state }
        Start-Sleep -Seconds 2
    }
    return 'timeout'
}

function Restart-OssWsl {
    # wsl --shutdown (WSL is dedicated to ossbot), wait, start again. Settings apply only at VM start.
    param([string]$Distro = $script:OssDistro, [int]$WaitSec = 10)
    & wsl.exe --shutdown | Out-Null
    Start-Sleep -Seconds $WaitSec
    return Start-OssDistro -Distro $Distro
}

# ------------------------------------------------------------------ lifecycle (CR-001 5/6, DESIGN 13.5)
# down.ps1, up.ps1 and the shutdown blocker's channel drain (6a) serialise on a per-user named mutex,
# so two downs never run at once. 'Local\' is the session namespace: the blocker and a console run in
# the same session.
$script:OssLifecycleMutex = 'Local\ossbot-lifecycle'
$script:OssBlockerMutex = 'Local\ossbot-shutdown-blocker'

function Write-OssLifecycleLog {
    # One line in .ossbot-local\lifecycle.log (UTC ISO timestamp first).
    param([Parameter(Mandatory)][string]$Text)
    $file = Join-Path (New-OssLocalDir) 'lifecycle.log'
    $line = '{0} {1}' -f (Get-Date).ToUniversalTime().ToString('o'), $Text
    for ($i = 0; $i -lt 5; $i++) {
        # two scripts may append at the same moment; a sharing violation is retried
        try { [IO.File]::AppendAllText($file, $line + "`r`n", (New-Object System.Text.UTF8Encoding $false)); return }
        catch [System.IO.IOException] { Start-Sleep -Milliseconds 100 }
    }
}

function Enter-OssLifecycleLock {
    # Wait up to TimeoutSec for the lifecycle mutex. Returns the held mutex, or $null on timeout. A
    # mutex abandoned by a killed holder counts as acquired (its run is over).
    param([int]$TimeoutSec = 0)
    $m = New-Object System.Threading.Mutex($false, $script:OssLifecycleMutex)
    $got = $false
    try { $got = $m.WaitOne([Math]::Max(0, $TimeoutSec) * 1000) }
    catch [System.Threading.AbandonedMutexException] { $got = $true }
    if ($got) { return $m }
    $m.Dispose()
    return $null
}

function Exit-OssLifecycleLock {
    param([object]$Mutex)
    if ($null -eq $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch { }
    $Mutex.Dispose()
}

function Test-OssBlockerRunning {
    # The shutdown blocker (6a) holds Local\ossbot-shutdown-blocker for as long as it runs.
    $m = $null
    try {
        if ([System.Threading.Mutex]::TryOpenExisting($script:OssBlockerMutex, [ref]$m)) { $m.Dispose(); return $true }
        return $false
    } catch [System.UnauthorizedAccessException] { return $true }
}

function Get-OssBlockerState {
    # The blocker's own state file (.ossbot-local\shutdown-blocker.state, JSON), or $null.
    $file = Join-Path $script:OssLocal 'shutdown-blocker.state'
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    try { return ([IO.File]::ReadAllText($file) | ConvertFrom-Json) } catch { return $null }
}

function Test-OssTaskDisabled {
    # A disabled task reports State 'Running' while an instance still runs (the autostart task's shutdown
    # blocker keeps running after kill.ps1 -Hard), so Settings.Enabled is the truth, not State.
    param([object]$Task)
    if (-not $Task) { return $false }
    return ($Task.State -eq 'Disabled') -or ($Task.Settings -and $Task.Settings.Enabled -eq $false)
}

$script:OssAutostartTask = 'ossbot-autostart'
$script:OssHookTask = 'ossbot-shutdown-hook'
$script:OssBlockerTitle = 'ossbot shutdown blocker'   # ShutdownBlockerWindow.WindowTitle

function Add-OssWin32 {
    # The few user32 calls the scripts and tests need (FindWindow, PostMessage, SendMessageTimeout...).
    if ('OssbotWin32.Native' -as [type]) { return }
    Add-Type -Namespace OssbotWin32 -Name Native -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
[DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam, uint flags, uint timeoutMs, out IntPtr result);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
[DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hWnd);
[DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hWnd, int nIndex);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
'@
}

function Find-OssBlockerWindow {
    Add-OssWin32
    return [OssbotWin32.Native]::FindWindow([NullString]::Value, $script:OssBlockerTitle)
}

function Get-OssBlockerCommand {
    # What the autostart task's second action runs: the blocker, hidden, from the repo (6a).
    $script = Join-Path $script:OssRepo 'scripts\windows\ossbot-shutdown-blocker.ps1'
    return "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
}

function Start-OssBlocker {
    # Start the blocker now (bootstrap, tests), the way the autostart action does. True once its window exists.
    if (Test-OssBlockerRunning) { return $true }
    $conhost = Join-Path $env:WINDIR 'System32\conhost.exe'
    Start-Process -FilePath $conhost -ArgumentList (Get-OssBlockerCommand) -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if ((Test-OssBlockerRunning) -and (Find-OssBlockerWindow) -ne [IntPtr]::Zero) { return $true }
    }
    return $false
}

function Stop-OssBlocker {
    # Close the blocker's window (WM_CLOSE); it releases its mutex and exits. True when it is gone.
    if (-not (Test-OssBlockerRunning)) { return $true }
    $h = Find-OssBlockerWindow
    if ($h -ne [IntPtr]::Zero) { [void][OssbotWin32.Native]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Test-OssBlockerRunning)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Get-OssAutostartActions {
    # ossbot-autostart (DESIGN 7.6, 13.5): boot the distro at logon, then start the shutdown blocker (6a).
    param([switch]$WithoutBlocker)
    $conhost = Join-Path $env:WINDIR 'System32\conhost.exe'
    $actions = @(@{ Execute = $conhost; Arguments = '--headless wsl.exe -d ossbot -u ossnobody --exec /bin/true' })
    if (-not $WithoutBlocker) { $actions += @{ Execute = $conhost; Arguments = (Get-OssBlockerCommand) } }
    return $actions
}

function Get-OssAutostartProblems {
    # How the registered ossbot-autostart differs from its spec (empty = as required).
    param([object]$Task, [switch]$WithoutBlocker)
    $p = New-Object System.Collections.Generic.List[string]
    if ($null -eq $Task) { $p.Add('missing'); return $p.ToArray() }
    $want = @(Get-OssAutostartActions -WithoutBlocker:$WithoutBlocker)
    $have = @($Task.Actions)
    if ($have.Count -ne $want.Count) { $p.Add("$($have.Count) action(s), want $($want.Count)") }
    for ($i = 0; $i -lt [Math]::Min($have.Count, $want.Count); $i++) {
        if ($have[$i].Execute -ne $want[$i].Execute -or $have[$i].Arguments -ne $want[$i].Arguments) { $p.Add("action $($i + 1) differs") }
    }
    $t = @($Task.Triggers) | Select-Object -First 1
    if ($null -eq $t -or @('PT60S', 'PT1M') -notcontains [string]$t.Delay) { $p.Add('logon trigger delay') }
    # The blocker runs for the whole session: no execution time limit (PT0S), or Task Scheduler ends it.
    if (-not $WithoutBlocker -and [string]$Task.Settings.ExecutionTimeLimit -ne 'PT0S') { $p.Add("time limit $($Task.Settings.ExecutionTimeLimit)") }
    return $p.ToArray()
}

function Install-OssAutostartTask {
    # Register (or re-register on drift) ossbot-autostart. Returns 'unchanged', 'changed' or 'WARN: ...'.
    param([switch]$WithoutBlocker)
    $existing = Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue
    if ($existing -and @(Get-OssAutostartProblems -Task $existing -WithoutBlocker:$WithoutBlocker).Count -eq 0) { return 'unchanged' }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $actions = @(Get-OssAutostartActions -WithoutBlocker:$WithoutBlocker | ForEach-Object { New-ScheduledTaskAction -Execute $_.Execute -Argument $_.Arguments })
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT60S'
    $limit = if ($WithoutBlocker) { New-TimeSpan -Minutes 5 } else { [TimeSpan]::Zero }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit $limit -Hidden
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $enabled = -not (Test-OssTaskDisabled -Task $existing)   # kill.ps1 -Hard keeps it disabled
    try {
        Register-ScheduledTask -TaskName $script:OssAutostartTask -Action $actions -Trigger $trigger -Settings $settings -Principal $principal -Description 'ossbot: boot the ossbot WSL distro at logon and start the shutdown blocker (DESIGN 7.6, 13.5)' -Force | Out-Null
        if (-not $enabled) { Disable-ScheduledTask -TaskName $script:OssAutostartTask | Out-Null }
        return 'changed'
    } catch {
        return "WARN: could not register $($script:OssAutostartTask): $($_.Exception.Message)"
    }
}

function Get-OssHostTaskStates {
    # status.ps1: the autostart task (with the blocker action, 6a), the blocker process with its drain
    # channel, and the retired shutdown-hook task (6b), one line each.
    $a = Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue
    $auto = 'missing (scripts\bootstrap.ps1 -Only autostart)'
    if ($a) {
        $hasBlocker = @($a.Actions | Where-Object { [string]$_.Arguments -match 'ossbot-shutdown-blocker\.ps1' }).Count -gt 0
        $auto = "$($a.State)" + $(if ($hasBlocker) { ', also starts the shutdown blocker at logon' } else { ', no blocker action' })
    }
    $h = Get-ScheduledTask -TaskName $script:OssHookTask -ErrorAction SilentlyContinue
    $hook = if ($h) { "retired (control channel), but still registered: scripts\bootstrap.ps1 -Only shutdownhooks removes it" } else { 'retired (control channel)' }
    $blk = 'not running (starts at logon; now: scripts\bootstrap.ps1 -Only shutdownhooks)'
    if (Test-OssBlockerRunning) {
        $blk = 'running'
        $st = Get-OssBlockerState
        if ($null -ne $st) {
            $bp = $st.PSObject.Properties['pid']; $ph = $st.PSObject.Properties['phase']
            if ($bp -and $ph) { $blk += " (pid $($bp.Value), $($ph.Value), $(Get-OssBlockerChannelText -State $st))" }
        }
    }
    return [pscustomobject]@{ Autostart = $auto; Blocker = $blk; Hook = $hook }
}

function Format-OssLastShutdown {
    # status.ps1 'last shutdown' from the boot gate's record {type, marker_ts, detected_at}. An unclean
    # record has no marker by definition; only a missing record gets the -Missing text.
    param([object]$Record, [string]$Missing)
    if ($null -eq $Record) { return $Missing }
    $get = { param($n) $p = $Record.PSObject.Properties[$n]; if ($p -and "$($p.Value)" -ne '') { "$($p.Value)" } else { $null } }
    $type = & $get 'type'; $ts = & $get 'marker_ts'; $det = & $get 'detected_at'
    $detText = if ($det) { "detected $det" } else { 'detection time not recorded' }
    switch ($type) {
        { $_ -in 'graceful', 'hook' } { return "$type (marker $(if ($ts) { $ts } else { 'time not recorded' }), $detText)" }
        'unclean' { return "unclean (no shutdown marker: ossctl down did not run; $detText)" }
        'none' { return 'none recorded (first start)' }
        $null { return $Missing }
        default { return "$type ($detText)" }
    }
}

function Get-OssBlockerChannelText {
    # The drain channel from the blocker's state file: 'channel up pid N', 'channel down', ...
    param([object]$State)
    $c = $State.PSObject.Properties['channel']
    if (-not $c) { return 'no channel (old blocker; restart it: scripts\bootstrap.ps1 -Uninstall -Only shutdownhooks, then -Only shutdownhooks)' }
    $cp = $State.PSObject.Properties['channel_pid']
    switch ([string]$c.Value) {
        'up' { if ($cp -and [int]$cp.Value -gt 0) { return "channel up pid $($cp.Value)" }; return 'channel up' }
        'skipped' { return 'channel skipped (distro stopped)' }
        default { return "channel $($c.Value)" }
    }
}

# ------------------------------------------- 6b: the retired shutdown-hook task (ADR-0021 addendum)
# The User32 1074 task started a new process in the ending session, which Windows refuses
# (0xC000026B, STATUS_DLL_INIT_FAILED_LOGOFF). It is retired in favour of the blocker's drain
# channel; bootstrap removes a registered one and status/doctor report "retired".
function Remove-OssHookTask {
    # Remove ossbot-shutdown-hook when registered. Returns 'unchanged', 'changed' or 'WARN: ...'.
    $existing = Get-ScheduledTask -TaskName $script:OssHookTask -ErrorAction SilentlyContinue
    if (-not $existing) { return 'unchanged' }
    try {
        Unregister-ScheduledTask -TaskName $script:OssHookTask -Confirm:$false -ErrorAction Stop
        return 'changed'
    } catch {
        return "WARN: could not remove $($script:OssHookTask): $($_.Exception.Message)"
    }
}

# ------------------------------------------------- Start-menu shortcuts (CR-001 5, bootstrap -Shortcuts)
function Get-OssShortcutDir { return (Join-Path ([Environment]::GetFolderPath('Programs')) 'ossbot') }

function Get-OssShortcutSpecs {
    $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $specs = @()
    foreach ($s in @(@('Up', 'up.ps1'), @('Down', 'down.ps1'), @('Status', 'status.ps1'), @('Kill', 'kill.ps1'))) {
        $file = Join-Path $script:OssRepo "scripts\$($s[1])"
        $specs += [pscustomobject]@{
            Path = Join-Path (Get-OssShortcutDir) "ossbot $($s[0]).lnk"; Target = $ps
            Arguments = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$file`""; Description = "ossbot $($s[0]) (scripts\$($s[1]))"
        }
    }
    return $specs
}

function Install-OssShortcuts {
    # Creates or fixes the four shortcuts; returns 'unchanged' or 'changed'.
    $shell = New-Object -ComObject WScript.Shell
    $dir = Get-OssShortcutDir
    $changed = $false
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null; $changed = $true }
    foreach ($s in Get-OssShortcutSpecs) {
        if (Test-Path -LiteralPath $s.Path) {
            $have = $shell.CreateShortcut($s.Path)
            if ($have.TargetPath -eq $s.Target -and $have.Arguments -eq $s.Arguments -and $have.WorkingDirectory -eq $script:OssRepo) { continue }
        }
        $lnk = $shell.CreateShortcut($s.Path)
        $lnk.TargetPath = $s.Target
        $lnk.Arguments = $s.Arguments
        $lnk.WorkingDirectory = $script:OssRepo
        $lnk.Description = $s.Description
        $lnk.Save()
        $changed = $true
    }
    if ($changed) { return 'changed' }
    return 'unchanged'
}

function Remove-OssShortcuts {
    # Removes only the four ossbot shortcuts, and the folder when it is then empty. Returns the count.
    $n = 0
    foreach ($s in Get-OssShortcutSpecs) {
        if (Test-Path -LiteralPath $s.Path) { Remove-Item -LiteralPath $s.Path -Force; $n++ }
    }
    $dir = Get-OssShortcutDir
    if ((Test-Path -LiteralPath $dir) -and @(Get-ChildItem -LiteralPath $dir -Force).Count -eq 0) { Remove-Item -LiteralPath $dir -Force }
    return $n
}

function Get-OssSha256Hex {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}
