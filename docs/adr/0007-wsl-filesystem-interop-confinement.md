# ADR-0007: Confine the ossbot distro to the repo folder and disable interop

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §7.2, §7.6, §22.5

## Context
Hard user requirement: the distro may only see `D:\Work\autonomous-oss-contributor`, and must not launch Windows executables. The distro VHD already lives in `.wsl\ossbot` inside that folder, so all state stays inside it.

## Decision
- `/etc/wsl.conf`: `[automount] enabled=false, mountFsTab=true`; `[interop] enabled=false, appendWindowsPath=false`; `[boot] systemd=true`; `[user] default=ossadmin`.
- `/etc/fstab` managed block with one drvfs line: `D:\134Work\134autonomous-oss-contributor /mnt/d/Work/autonomous-oss-contributor drvfs metadata,uid=1000,gid=1500,umask=002 0 0` (owner ossadmin, group osscfg = gatekeeper write; everyone else read-only).
- Windows scripts stream shell scripts over stdin so a broken mount never bricks provisioning; bootstrap verifies after `wsl --terminate` and rolls back on failure.
- Keepalive via a per-user Scheduled Task (`wsl.exe -d ossbot --exec sleep infinity`) instead of editing the global `.wslconfig` `instanceIdleTimeout`.

## Consequences
No `/mnt/c`, no Windows PATH, no `.exe` execution from Linux. Windows → Linux (`wsl.exe -d ossbot -u root`) still works for kill/resume. Linux listeners on 127.0.0.1 are still forwarded to Windows localhost (NAT default); sensitive APIs use Unix sockets. **Verify:** fstab escaping form, metadata/gid behaviour after Windows-side edits, keepalive effectiveness.
