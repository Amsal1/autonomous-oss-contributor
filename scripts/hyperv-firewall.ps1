<#
.SYNOPSIS
  Hyper-V firewall for the WSL VM (DESIGN 7.8, ADR-0013). Requires an elevated PowerShell; idempotent.
.DESCRIPTION
  Default inbound Block, default outbound Block, allow outbound TCP 443 and TCP 80 (apt).
  Coarse VM-level layer below the in-distro squid/iptables allowlist.
  Exactly the two ossbot rules are required (Outbound, Allow, Enabled, TCP, 443/80, the WSL
  VMCreatorId): a missing or drifted ossbot rule is re-created (Phase 1 review P1-16). Other Allow
  rules for the WSL VM are reported, not deleted (they are yours); scripts\ossctl.ps1 doctor fails on them.
  -Remove restores the Allow defaults and deletes the ossbot-* rules.
#>
[CmdletBinding()]
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')
$wsl = $script:OssHyperVCreatorId

if (-not (Test-OssElevated)) {
    Write-Host 'hyperv-firewall.ps1 needs an elevated PowerShell (it never self-elevates).'
    exit 2
}
if (-not (Get-Command Set-NetFirewallHyperVVMSetting -ErrorAction SilentlyContinue)) {
    Write-Host 'step hyperv: WARN (Hyper-V firewall cmdlets not available on this build; recorded as unsupported)'
    exit 3
}

$rules = $script:OssHyperVRules

if ($Remove) {
    Set-NetFirewallHyperVVMSetting -Name $wsl -DefaultInboundAction Allow -DefaultOutboundAction Allow
    foreach ($r in $rules) {
        if (Get-NetFirewallHyperVRule -Name $r.Name -ErrorAction SilentlyContinue) { Remove-NetFirewallHyperVRule -Name $r.Name }
    }
    Write-Host 'step hyperv: changed (ossbot rules removed, defaults Allow)'
    exit 0
}

$changed = $false
$setting = Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name $wsl
if ($setting.Enabled -ne 'True' -or $setting.DefaultInboundAction -ne 'Block' -or $setting.DefaultOutboundAction -ne 'Block') {
    Set-NetFirewallHyperVVMSetting -Name $wsl -Enabled True -DefaultInboundAction Block -DefaultOutboundAction Block
    $changed = $true
}
foreach ($r in $rules) {
    $existing = @(Get-NetFirewallHyperVRule -Name $r.Name -ErrorAction SilentlyContinue)
    $drift = @()
    if ($existing.Count -eq 1) { $drift = @(Get-OssHyperVRuleProblems -Rule $existing[0] -Port $r.Port) }
    if ($existing.Count -eq 1 -and $drift.Count -eq 0) { continue }
    if ($existing.Count -gt 0) {
        Write-Host ("  {0}: drifted ({1}); re-creating" -f $r.Name, ($(if ($drift.Count) { $drift -join ', ' } else { "$($existing.Count) copies" })))
        Remove-NetFirewallHyperVRule -Name $r.Name
    }
    New-NetFirewallHyperVRule -Name $r.Name -DisplayName $r.DisplayName -Direction Outbound -VMCreatorId $wsl `
        -Protocol TCP -RemotePorts $r.Port -Action Allow -Enabled True | Out-Null
    $changed = $true
}
$foreign = @(Get-OssHyperVForeignAllows -AllRules @(Get-NetFirewallHyperVRule -ErrorAction SilentlyContinue))
foreach ($f in $foreign) {
    Write-Host ("  WARN: other Allow rule for the WSL VM: {0} ({1}); remove it unless you added it on purpose" -f $f.Name, $f.DisplayName)
}
Write-Host ("step hyperv: {0}" -f $(if ($changed) { 'changed' } else { 'unchanged' }))
exit 0
