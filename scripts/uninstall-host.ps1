<#
.SYNOPSIS
  Undo ossbot's host-side changes (DESIGN 7.7 step 5, NFR-3, CR-001 6d); also `bootstrap.ps1 -Uninstall`.
.DESCRIPTION
  Removes only what bootstrap put on the host, part by part (-Only, default all):
    shortcuts      the Start-menu shortcuts 'ossbot Up/Down/Status/Kill' (bootstrap -Shortcuts)
    shutdownhooks  the shutdown blocker process (6a, closed through its window), its action in
                   'ossbot-autostart' (the task is kept with its first action unless 'autostart' is
                   removed too) and the retired 'ossbot-shutdown-hook' task (6b) if still registered
    autostart      the 'ossbot-autostart' task (and the blocker it starts)
    hyperv         the Hyper-V firewall rules (elevated sessions only; otherwise a hint)
    wsl            %USERPROFILE%\.wslconfig restored from the newest backup in .ossbot-local\ (or deleted
                   when the recorded state is 'none')
  Never touches the distro, its VHD or anything under .wsl\; the repo and its gitignored state stay.
.PARAMETER Only
  Comma-separated subset of the parts above.
#>
[CmdletBinding()]
param([string]$Only = '')
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\wslconfig.ps1')

$parts = @('shortcuts', 'shutdownhooks', 'autostart', 'hyperv', 'wsl')
$selected = $parts
if ($Only) {
    $selected = @($Only -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($p in $selected) {
        if ($parts -notcontains $p) { Write-Host "unknown part '$p' (known: $($parts -join ', '))"; exit 2 }
    }
}
$exit = 0

if ($selected -contains 'shortcuts') {
    $n = Remove-OssShortcuts
    Write-OssStep 'uninstall-shortcuts' $(if ($n) { 'changed' } else { 'unchanged' }) "$n shortcut(s) removed from $(Get-OssShortcutDir)"
}

if ($selected -contains 'shutdownhooks' -or $selected -contains 'autostart') {
    $was = Test-OssBlockerRunning
    if (Stop-OssBlocker) { Write-OssStep 'uninstall-blocker' $(if ($was) { 'changed' } else { 'unchanged' }) $(if ($was) { 'shutdown blocker closed' } else { 'not running' }) }
    else { Write-OssStep 'uninstall-blocker' 'WARN' 'the shutdown blocker did not close (see .ossbot-local\shutdown-blocker.log)'; $exit = 1 }
}

if ($selected -contains 'shutdownhooks') {
    $r = Remove-OssHookTask
    if ($r -like 'WARN*') { Write-OssStep 'uninstall-hook' 'WARN' ($r -replace '^WARN: ', ''); $exit = 1 }
    elseif ($r -eq 'changed') { Write-OssStep 'uninstall-hook' 'changed' "removed Scheduled Task $($script:OssHookTask) (retired 6b)" }
    else { Write-OssStep 'uninstall-hook' 'unchanged' "$($script:OssHookTask) not registered" }
    $auto = Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue
    if ($auto -and $selected -notcontains 'autostart') {
        $r = Install-OssAutostartTask -WithoutBlocker
        if ($r -like 'WARN*') { Write-OssStep 'uninstall-blocker-action' 'WARN' $r; $exit = 1 }
        else { Write-OssStep 'uninstall-blocker-action' $r "$($script:OssAutostartTask) keeps only its distro-boot action" }
    }
}

if ($selected -contains 'autostart') {
    if (Get-ScheduledTask -TaskName $script:OssAutostartTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $script:OssAutostartTask -Confirm:$false
        Write-OssStep 'uninstall-autostart' 'changed' "removed Scheduled Task $($script:OssAutostartTask)"
    } else { Write-OssStep 'uninstall-autostart' 'unchanged' "$($script:OssAutostartTask) not registered" }
}

if ($selected -contains 'hyperv') {
    if (Test-OssElevated) {
        & powershell -NoProfile -File (Join-Path $PSScriptRoot 'hyperv-firewall.ps1') -Remove
    } else {
        Write-OssStep 'uninstall-hyperv' 'WARN' 'not elevated: Hyper-V rules (if any) left in place; run elevated: scripts\hyperv-firewall.ps1 -Remove'
    }
}

if ($selected -contains 'wsl') {
    $path = Get-OssWslConfigPath
    $local = Join-Path (Get-OssRepoRoot) '.ossbot-local'
    $stateFile = Join-Path $local 'wslconfig.state'
    $backup = Get-ChildItem -LiteralPath $local -Filter 'wslconfig.backup-*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($backup) {
        Copy-Item -LiteralPath $backup.FullName -Destination $path -Force
        Write-OssStep 'uninstall-wslconfig' 'changed' "restored $path from $($backup.Name)"
    } elseif ((Test-Path -LiteralPath $stateFile) -and ((Get-Content -LiteralPath $stateFile -TotalCount 1) -eq 'none')) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force; Write-OssStep 'uninstall-wslconfig' 'changed' "deleted $path (state was 'none')" }
        else { Write-OssStep 'uninstall-wslconfig' 'unchanged' "$path absent" }
    } else {
        Write-OssStep 'uninstall-wslconfig' 'unchanged' "no backup or state recorded; leaving $path unchanged"
    }
    Write-Host 'WSL settings change on the next start: wsl --shutdown'
}
exit $exit
