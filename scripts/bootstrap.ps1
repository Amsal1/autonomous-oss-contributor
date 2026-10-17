<#
.SYNOPSIS
  ossbot bootstrap (DESIGN 18.2): takes this machine from "WSL installed, repo cloned" to running.
.DESCRIPTION
  Idempotent; every step prints changed / unchanged (or WARN with a follow-up).
    preflight  Windows 11, WSL >= 3.0, repo path, free space, distro present (created from zero
               with `wsl --install Ubuntu-24.04 --name <d> --location <repo>\.wsl\<d> --no-launch`).
    wsl        %USERPROFILE%\.wslconfig (7.7) + wsl.conf/fstab confinement (7.2); restarts WSL when
               anything changed, then verifies resources and the single drvfs mount (rollback on failure).
    hyperv     Hyper-V firewall rules (7.8) only when this session is already elevated, else WARN.
    provision  streams scripts/provision.sh over stdin as root (never runs it from the mount, N4).
    autostart  per-user Scheduled Task 'ossbot-autostart' (7.6).
  Never unregisters, deletes or overwrites a distro or VHD.
.PARAMETER Only
  Comma-separated subset of phases (preflight always runs), e.g. -Only wsl.
.PARAMETER DistroName
  Distro to bootstrap (default ossbot). Other names are for tests/system/test_bootstrap_zero.ps1.
.PARAMETER Location
  Directory for a newly created distro's VHD (default <repo>\.wsl\<DistroName>).
.PARAMETER Steps
  Passed to provision.sh as --only (comma list), for partial provisioning.
#>
[CmdletBinding()]
param(
    [string]$Only = '',
    [string]$DistroName = 'ossbot',
    [string]$Location = '',
    [string]$Steps = ''
)
# Native stderr must not become terminating errors under PowerShell 5.1; failures are explicit.
$ErrorActionPreference = 'Continue'
$script:OssDistro = $DistroName
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\wslconfig.ps1')

$repo = Get-OssRepoRoot
$phases = @('wsl', 'hyperv', 'provision', 'autostart')
$selected = $phases
if ($Only) {
    $selected = @($Only -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($p in $selected) {
        if ($p -notin ($phases + 'preflight')) { Write-Host "unknown phase '$p' (known: preflight, $($phases -join ', '))"; exit 2 }
    }
}
if (-not $Location) { $Location = Join-Path $repo ".wsl\$DistroName" }
$script:Failed = $false

function Fail([string]$Message, [int]$Code = 1) {
    Write-Host "bootstrap: FAIL: $Message" -ForegroundColor Red
    exit $Code
}

# ------------------------------------------------------------------------------ 1. preflight
function Invoke-Preflight {
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 22000) { Fail "Windows 11 required (build $build)" 2 }
    $ver = $null
    try {
        $lines = ConvertFrom-OssWslText (& wsl.exe --version 2>$null)
        foreach ($l in $lines) { if ($l -match '^WSL[^:]*:\s*([0-9]+(\.[0-9]+)+)') { $ver = [version]$Matches[1]; break } }
    } catch { $ver = $null }
    if ($null -eq $ver -or $ver -lt [version]'3.0') {
        Write-Host 'WSL 3.0 or newer is required. Install it (elevated, then reboot):'
        Write-Host '  wsl --install --no-distribution   (or the MSI from https://github.com/microsoft/WSL/releases)'
        Write-Host '  See docs/SETUP.md.'
        exit 2
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repo 'scripts\provision.sh'))) { Fail "repo not found at $repo" 2 }
    $drive = (Split-Path -Qualifier $repo).TrimEnd(':')
    $freeGb = [math]::Floor((Get-PSDrive -Name $drive).Free / 1GB)
    if ($freeGb -lt 100) { Fail "free space on ${drive}: is $freeGb GB, need >= 100 GB" 2 }

    $distros = Get-OssDistros
    if ($distros -notcontains $DistroName) {
        $vhd = Join-Path $Location 'ext4.vhdx'
        if (Test-Path -LiteralPath $vhd) {
            Write-Host "Distro '$DistroName' is not registered but its disk exists at $vhd."
            Write-Host 'Bootstrap never creates a second disk. Re-register the existing one with:'
            Write-Host "  wsl --import-in-place $DistroName `"$vhd`""
            Write-Host 'then re-run scripts\bootstrap.ps1.'
            exit 3
        }
        Write-OssStep 'preflight' 'changed' "creating distro $DistroName at $Location"
        New-Item -ItemType Directory -Force -Path $Location | Out-Null
        & wsl.exe --install Ubuntu-24.04 --name $DistroName --location $Location --no-launch
        if ($LASTEXITCODE -ne 0) { Fail "wsl --install failed ($LASTEXITCODE)" }
        $deadline = (Get-Date).AddMinutes(5)
        while ((Get-Date) -lt $deadline) {
            $r = Invoke-OssWsl -User root -Command @('/bin/true')
            if ($r.Code -eq 0) { break }
            Start-Sleep -Seconds 3
        }
        if ($r.Code -ne 0) { Fail "distro $DistroName did not become usable as root" }
    }
    if ($DistroName -eq 'ossbot') {
        $default = $null
        foreach ($l in (ConvertFrom-OssWslText (& wsl.exe -l 2>$null))) { if ($l -match '^\s*(\S+)\s+\(') { $default = $Matches[1] } }
        if ($default -ne 'ossbot') {
            & wsl.exe --set-default ossbot | Out-Null
            Write-OssStep 'preflight' 'changed' 'wsl --set-default ossbot'
            return
        }
    }
    Write-OssStep 'preflight' 'unchanged' "WSL $ver, $freeGb GB free, distro $DistroName registered"
}

# ------------------------------------------------------------------------- 2+4. wsl config
function Invoke-WslConfine {
    param([int]$Form = 1)
    $text = (Get-OssScriptText @('scripts/lib/common.sh', 'scripts/lib/users.sh', 'scripts/lib/wsl.sh')) +
        "`nwsl_main `"`$@`"; exit `$?`n"
    return Invoke-OssStreamCapture -Text $text -Arguments @('apply', '--form', "$Form")
}

function Invoke-WslScript([string[]]$Arguments) {
    $text = (Get-OssScriptText @('scripts/lib/common.sh', 'scripts/lib/users.sh', 'scripts/lib/wsl.sh')) +
        "`nwsl_main `"`$@`"; exit `$?`n"
    return Invoke-OssStreamCapture -Text $text -Arguments $Arguments
}

function Set-OssSparse {
    # One-time sparse conversion of the existing VHD while the VM is stopped; failure is a WARN.
    if ($DistroName -ne 'ossbot') { return }
    $stateFile = Join-Path (New-OssLocalDir) 'sparse.state'
    if (Test-Path -LiteralPath $stateFile) { return }
    $out = ConvertFrom-OssWslText (& wsl.exe --manage $DistroName --set-sparse true 2>&1)
    if ($LASTEXITCODE -eq 0) {
        [IO.File]::WriteAllText($stateFile, "sparse true $(Get-Date -Format o)`r`n")
        Write-OssStep 'wsl-sparse' 'changed' 'wsl --manage --set-sparse true'
    } else {
        # Recorded once so re-runs do not restart WSL for it again (WARN, not fatal; ADR-0013 addendum).
        $msg = (($out -join ' ').Trim())
        [IO.File]::WriteAllText($stateFile, "failed $(Get-Date -Format o): $msg`r`n")
        Write-OssStep 'wsl-sparse' 'WARN' ("set-sparse failed: " + $msg)
    }
}

function Invoke-WslPhase {
    $cfgChanged = Install-OssWslConfig
    Write-OssStep 'wslconfig' $(if ($cfgChanged) { 'changed' } else { 'unchanged' }) (Get-OssWslConfigPath)

    $form = 1
    $r = Invoke-WslConfine -Form $form
    $r.Out | Where-Object { $_ -match 'changed:|ERROR|WARN' } | ForEach-Object { Write-Host "  $_" }
    if ($r.Code -ne 0) { $r.Out | ForEach-Object { Write-Host "  $_" }; Fail 'wsl.sh apply failed' }
    $confChanged = [bool]($r.Out -match '^wsl: changed')
    Write-OssStep 'wsl-confine' $(if ($confChanged) { 'changed' } else { 'unchanged' }) '/etc/wsl.conf + fstab block'

    $sparseDone = Test-Path -LiteralPath (Join-Path (New-OssLocalDir) 'sparse.state')
    if ($cfgChanged -or $confChanged -or (-not $sparseDone -and $DistroName -eq 'ossbot')) {
        & wsl.exe --shutdown | Out-Null
        Start-Sleep -Seconds 10
        Set-OssSparse
        $state = Start-OssDistro
        Write-Host "  restarted WSL (systemd: $state)"
    }

    # Leftover automount directories (e.g. an empty ext4 /mnt/c) can only be removed once nothing is
    # mounted on them, i.e. after the restart: apply again (idempotent), then verify the mount.
    $again = Invoke-WslConfine -Form $form
    $again.Out | Where-Object { $_ -match 'changed:' } | ForEach-Object { Write-Host "  $_" }
    $check = Invoke-WslScript @('check')
    # Exit 3 = repo not mounted: try the fallback source forms, then roll back (7.2).
    while ($check.Code -eq 3 -and $form -lt 3) {
        $check.Out | ForEach-Object { Write-Host "  $_" }
        $form++
        Write-Host "  mount missing; trying fstab form $form"
        $r = Invoke-WslConfine -Form $form
        if ($r.Code -ne 0) { break }
        Restart-OssWsl | Out-Null
        $check = Invoke-WslScript @('check')
    }
    if ($check.Code -ne 0) {
        $check.Out | ForEach-Object { Write-Host "  $_" }
        Write-Host '  fallback forms (DESIGN 7.2): D:\134Work\134autonomous-oss-contributor | D:/Work/autonomous-oss-contributor | D:\\Work\\autonomous-oss-contributor'
        $rb = Invoke-WslScript @('rollback')
        $rb.Out | ForEach-Object { Write-Host "  $_" }
        Restart-OssWsl | Out-Null
        Fail 'repo mount missing after confinement; previous fstab/wsl.conf restored'
    }
    $check.Out | ForEach-Object { Write-Host "  $_" }
    if ($form -ne 1) { Write-OssStep 'wsl-confine' 'changed' "fstab source form $form" }

    $problems = @(Test-OssWslResources)
    if ($problems.Count) { $problems | ForEach-Object { Write-Host "  $_" }; Fail '.wslconfig settings not in effect' }
    Write-OssStep 'wsl-verify' 'PASS' 'nproc 20, ~20 GB RAM, ~8 GB swap, single drvfs mount'
}

# ----------------------------------------------------------------------------- 3. hyperv
function Invoke-HyperVPhase {
    if (-not (Test-OssElevated)) {
        Write-OssStep 'hyperv' 'WARN' 'not elevated; optional follow-up in an elevated PowerShell: powershell -NoProfile -File scripts\hyperv-firewall.ps1'
        return
    }
    & powershell -NoProfile -File (Join-Path $PSScriptRoot 'hyperv-firewall.ps1')
    if ($LASTEXITCODE -ne 0) { Write-OssStep 'hyperv' 'WARN' "hyperv-firewall.ps1 exit $LASTEXITCODE (recorded as unsupported)" }
}

# -------------------------------------------------------------------------- 5. provision
function Invoke-ProvisionPhase {
    $text = Get-OssScriptText @('scripts/provision.sh')
    $provArgs = @()
    if ($Steps) { $provArgs = @('--only', $Steps) }
    $code = Invoke-OssStream -Text $text -Arguments $provArgs
    if ($code -ne 0) { Fail "provision.sh exit $code" }
}

# -------------------------------------------------------------------------- 6. autostart
function Invoke-AutostartPhase {
    if ($DistroName -ne 'ossbot') { Write-OssStep 'autostart' 'unchanged' "skipped for test distro $DistroName"; return }
    $name = 'ossbot-autostart'
    $exe = Join-Path $env:WINDIR 'System32\conhost.exe'
    $argLine = '--headless wsl.exe -d ossbot -u ossnobody --exec /bin/true'
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        $a = $existing.Actions | Select-Object -First 1
        $t = $existing.Triggers | Select-Object -First 1
        if ($a.Execute -eq $exe -and $a.Arguments -eq $argLine -and $t.Delay -in @('PT60S', 'PT1M')) {
            Write-OssStep 'autostart' 'unchanged' "$name ($($existing.State))"
            return
        }
    }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $exe -Argument $argLine
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT60S'
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -Hidden
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'ossbot: boot the ossbot WSL distro at logon (DESIGN 7.6)' -Force | Out-Null
        Write-OssStep 'autostart' 'changed' "$name registered (at logon + 60 s)"
    } catch {
        Write-OssStep 'autostart' 'WARN' "could not register ${name}: $($_.Exception.Message)"
    }
}

Invoke-Preflight
foreach ($phase in $phases) {
    if ($selected -notcontains $phase) { continue }
    switch ($phase) {
        'wsl' { Invoke-WslPhase }
        'hyperv' { Invoke-HyperVPhase }
        'provision' { Invoke-ProvisionPhase }
        'autostart' { Invoke-AutostartPhase }
    }
}
Write-Host 'bootstrap: done' -ForegroundColor Green
exit 0
