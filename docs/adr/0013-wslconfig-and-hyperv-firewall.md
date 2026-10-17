# ADR-0013: Machine-wide WSL configuration and Hyper-V firewall as VM-level defence in depth
Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §7.6-7.8, §13

## Context
User fact (2026-10-17): WSL on this laptop is used only for ossbot; `ossbot` is the default and only distro; there is no existing `%USERPROFILE%\.wslconfig`; host has 31.4 GB RAM and 24 logical CPUs. v1 avoided `.wslconfig` to protect other distros and kept the distro alive with a `sleep infinity` task. WSL docs (updated 2026-10-16): `[general] instanceIdleTimeout=-1` disables auto shutdown; `localhostForwarding` (default true) forwards distro localhost to Windows; `firewall=true` lets Windows Firewall and Hyper-V rules filter WSL traffic; `autoMemoryReclaim` and `sparseVhd` are `[experimental]`. Locally verified: WSL VMCreatorId `{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}` and the Hyper-V firewall cmdlets.

## Decision
- Track `config/wsl/wslconfig`; `bootstrap.ps1` installs it to `%USERPROFILE%\.wslconfig` (the only file ossbot writes outside the folder), backing up any existing file to `.ossbot-local\`, then `wsl --shutdown` and verifies `nproc`/memory/swap. `uninstall-host.ps1` restores.
- Values: `memory=20GB`, `processors=20`, `swap=8GB`, `swapFile=D:\\Work\\autonomous-oss-contributor\\.wsl\\swap.vhdx`, `networkingMode=nat`, `localhostForwarding=false`, `dnsTunneling=true`, `autoProxy=false`, `firewall=true`, `guiApplications=false`, `gpuSupport=false`, `nestedVirtualization=false`, `telemetry=false`, `[general] instanceIdleTimeout=-1`, `[experimental] autoMemoryReclaim=gradual`, `sparseVhd=true` (plus a one-time sparse conversion of the existing VHD).
- NAT, not mirrored: mirrored shares host interfaces and would expose Windows localhost services to the VM and vice versa.
- Autostart: per-user Scheduled Task boots the distro at logon; `instanceIdleTimeout=-1` keeps it up (fallback: `sleep infinity` task if that proves insufficient).
- Hyper-V firewall (elevated, idempotent `hyperv-firewall.ps1`): WSL VM default inbound Block, default outbound Block, allow outbound TCP 443 and 80 (apt). Coarse port-level layer that survives a compromised distro root; the in-distro hostname allowlist remains primary.
- Hard kill uses `wsl --shutdown`.

## Alternatives
- Leave `.wslconfig` untouched (v1): unnecessary now; loses resource caps, localhost isolation, and idle control.
- Mirrored networking: rejected (wider exposure both ways).
- Hyper-V firewall with per-IP allowlists: rejected (CDN IPs churn; hostnames are handled in squid).

## Consequences
Requires one elevated run for the firewall layer (WARN if skipped). **Verify:** every key honoured on WSL 3.0.3; `instanceIdleTimeout=-1` keeps systemd services up; outbound Block applies to NAT traffic and DNS tunnelling still works; `--set-sparse` flag spelling.
