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

## Implementation addendum (FEAT-004, 2026-10-18)
Verified live on this laptop (WSL 3.0.3.0, kernel 6.18.54.1-1, Windows build 26100) and on the WSL settings page (learn.microsoft.com/windows/wsl/wsl-config), DESIGN §23 items 1, 2 and 25:
- `.wslconfig` is honoured after `wsl --shutdown`: `nproc` 20, MemTotal 19.5 GiB, SwapTotal 8 GiB (`scripts/test-isolation.ps1`). `autoMemoryReclaim` and `sparseVhd` are still documented under `[experimental]`; `instanceIdleTimeout` under `[general]` (-1 disables the auto shutdown).
- `instanceIdleTimeout=-1` keeps the distro and its systemd units running after the launching `wsl.exe` exits (still listed by `wsl -l --running`, every unit active long after the last `wsl.exe`), so the Scheduled Task action stays `wsl.exe -d ossbot -u ossnobody --exec /bin/true` (run through `conhost.exe --headless` so no window flashes; trigger at logon + 60 s, stored by Task Scheduler as `PT1M`).
- `localhostForwarding=false` under NAT blocks Windows -> distro `127.0.0.1`: a listener on 127.0.0.1:18642 in the distro is reachable from inside, not from Windows `127.0.0.1`/`localhost`.
- `wsl --manage ossbot --set-sparse true` is the correct spelling and succeeded on the stopped VM (recorded once in `.ossbot-local\sparse.state`).
- Item 25: `wsl --help` on 3.0.3 lists `--install [Distro] --name --location --no-launch`, `--install --no-distribution` (fresh-machine WSL install), `--import-in-place <Distro> <FileName>` and `--manage --set-default-user`. The from-zero path is implemented in `bootstrap.ps1` and exercised by `tests/system/test_bootstrap_zero.ps1` (manual, test distro names only); it was not run live here because every WSL distro shares the VM and its network namespace with `ossbot`.
- Item 2 (Hyper-V firewall): not applied. The session was not elevated, so bootstrap records `step hyperv: WARN` and doctor reports WARN; `scripts/hyperv-firewall.ps1` (and `-Remove`) is the elevated follow-up. Whether `DefaultOutboundAction Block` applies to NAT traffic stays pending that elevated run.
