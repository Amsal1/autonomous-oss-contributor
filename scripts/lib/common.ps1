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

function Get-OssSha256Hex {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}
