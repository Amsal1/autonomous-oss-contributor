<#
.SYNOPSIS
  Hyper-V firewall for the WSL VM (DESIGN 7.8, ADR-0013). Requires an elevated PowerShell; idempotent.
.DESCRIPTION
  Default inbound Block, default outbound Block, allow outbound TCP 443 and TCP 80 (apt).
  Coarse VM-level layer below the in-distro squid/iptables allowlist.
  -Remove restores the Allow defaults and deletes the ossbot-* rules.
#>
[CmdletBinding()]
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$wsl = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'   # WSL VMCreatorId

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'hyperv-firewall.ps1 needs an elevated PowerShell (it never self-elevates).'
    exit 2
}
if (-not (Get-Command Set-NetFirewallHyperVVMSetting -ErrorAction SilentlyContinue)) {
    Write-Host 'step hyperv: WARN (Hyper-V firewall cmdlets not available on this build; recorded as unsupported)'
    exit 3
}

$rules = @(
    @{ Name = 'ossbot-out-https'; DisplayName = 'ossbot: WSL outbound TCP 443'; Port = 443 },
    @{ Name = 'ossbot-out-http'; DisplayName = 'ossbot: WSL outbound TCP 80 (apt)'; Port = 80 }
)

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
    $existing = Get-NetFirewallHyperVRule -Name $r.Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetFirewallHyperVRule -Name $r.Name -DisplayName $r.DisplayName -Direction Outbound -VMCreatorId $wsl `
            -Protocol TCP -RemotePorts $r.Port -Action Allow | Out-Null
        $changed = $true
    }
}
Write-Host ("step hyperv: {0}" -f $(if ($changed) { 'changed' } else { 'unchanged' }))
exit 0
