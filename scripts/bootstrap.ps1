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
    autostart  per-user Scheduled Task 'ossbot-autostart' (7.6): boots the distro at logon, then starts
               the shutdown blocker (CR-001 6a, ADR-0021), which is also started now if it does not run.
    shutdownhooks  the autostart blocker action as above (the blocker keeps a drain channel into the
               distro); removes the retired 6b task 'ossbot-shutdown-hook' when it is still registered
               (ADR-0021 addendum post-v1).
    shortcuts  Start-menu shortcuts 'ossbot Up/Down/Status/Kill' (only with -Shortcuts; off by default).
  Never unregisters, deletes or overwrites a distro or VHD.
.PARAMETER Only
  Comma-separated subset of phases (preflight always runs), e.g. -Only wsl. With -Uninstall: the
  parts to remove (shortcuts, shutdownhooks, autostart, hyperv, wsl; see scripts\uninstall-host.ps1).
.PARAMETER Uninstall
  Remove ossbot's host registrations instead (scripts\uninstall-host.ps1): only the ossbot-* tasks,
  the blocker, the shortcuts and the documented host files; never the distro, its VHD or the repo.
.PARAMETER Shortcuts
  Also create the Start-menu shortcuts (phase 'shortcuts').
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
    [string]$Steps = '',
    [switch]$Uninstall,
    [switch]$Shortcuts
)
# Native stderr must not become terminating errors under PowerShell 5.1; failures are explicit.
$ErrorActionPreference = 'Continue'
$script:OssDistro = $DistroName
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\wslconfig.ps1')

if ($Uninstall) {
    if ($DistroName -ne 'ossbot') { Write-Host 'refused: -Uninstall removes the host registrations of the ossbot distro only'; exit 2 }
    $uargs = @('-NoProfile', '-File', (Join-Path $PSScriptRoot 'uninstall-host.ps1'))
    if ($Only) { $uargs += @('-Only', $Only) }
    & powershell @uargs
    exit $LASTEXITCODE
}

$repo = Get-OssRepoRoot
$phases = @('wsl', 'hyperv', 'provision', 'autostart', 'shutdownhooks', 'shortcuts')
$selected = @($phases | Where-Object { $_ -ne 'shortcuts' })
if ($Only) {
    $selected = @($Only -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($p in $selected) {
        if ($p -notin ($phases + 'preflight')) { Write-Host "unknown phase '$p' (known: preflight, $($phases -join ', '))"; exit 2 }
    }
}
# The shortcuts live outside the project folder: only with -Shortcuts (CR-001 5, NFR-3).
if ($Shortcuts -and $selected -notcontains 'shortcuts') { $selected += 'shortcuts' }
if (-not $Shortcuts -and $selected -contains 'shortcuts') { Write-Host "phase 'shortcuts' needs -Shortcuts"; exit 2 }
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
    # ossbot-autostart (7.6) with its second action, the shutdown blocker (CR-001 6a, ADR-0021); the
    # blocker is started now too when it does not run yet (otherwise only at the next logon).
    if ($DistroName -ne 'ossbot') { Write-OssStep 'autostart' 'unchanged' "skipped for test distro $DistroName"; return }
    $r = Install-OssAutostartTask
    if ($r -like 'WARN*') { Write-OssStep 'autostart' 'WARN' ($r -replace '^WARN: ', ''); return }
    $task = Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue
    $state = if ($task) { $task.State } else { 'missing' }
    Write-OssStep 'autostart' $r "$($script:OssAutostartTask) ($state): at logon + 60 s boot the distro, then the shutdown blocker"
    if (Test-OssBlockerRunning) { Write-OssStep 'shutdown-blocker' 'unchanged' 'running'; return }
    if (Start-OssBlocker) { Write-OssStep 'shutdown-blocker' 'changed' 'started (scripts\windows\ossbot-shutdown-blocker.ps1)' }
    else { Write-OssStep 'shutdown-blocker' 'WARN' 'did not start; see .ossbot-local\shutdown-blocker.log' }
}

# ---------------------------------------------------------------------- 7. shutdown hooks
function Invoke-ShutdownHooksPhase {
    # CR-001 6a (the blocker with its drain channel, through the autostart task), ADR-0021. 6b (the
    # User32 1074 task) is retired (ADR-0021 addendum post-v1): a registered one is removed.
    if ($DistroName -ne 'ossbot') { Write-OssStep 'shutdownhooks' 'unchanged' "skipped for test distro $DistroName"; return }
    if ($selected -notcontains 'autostart') { Invoke-AutostartPhase }
    $r = Remove-OssHookTask
    if ($r -like 'WARN*') { Write-OssStep 'shutdown-hook' 'WARN' ($r -replace '^WARN: ', ''); return }
    $detail = if ($r -eq 'changed') { "$($script:OssHookTask) removed (retired, ADR-0021 addendum)" } else { "$($script:OssHookTask) retired (control channel), not registered" }
    Write-OssStep 'shutdown-hook' $r $detail
}

# --------------------------------------------------------------------------- 8. shortcuts
function Invoke-ShortcutsPhase {
    if ($DistroName -ne 'ossbot') { Write-OssStep 'shortcuts' 'unchanged' "skipped for test distro $DistroName"; return }
    Write-OssStep 'shortcuts' (Install-OssShortcuts) "ossbot Up/Down/Status/Kill in $(Get-OssShortcutDir)"
}

Invoke-Preflight
foreach ($phase in $phases) {
    if ($selected -notcontains $phase) { continue }
    switch ($phase) {
        'wsl' { Invoke-WslPhase }
        'hyperv' { Invoke-HyperVPhase }
        'provision' { Invoke-ProvisionPhase }
        'autostart' { Invoke-AutostartPhase }
        'shutdownhooks' { Invoke-ShutdownHooksPhase }
        'shortcuts' { Invoke-ShortcutsPhase }
    }
}
Write-Host 'bootstrap: done' -ForegroundColor Green
exit 0
