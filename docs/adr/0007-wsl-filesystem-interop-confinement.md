# ADR-0007: Confine the ossbot distro to the repo folder, disable interop, unprivileged default user
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §7.2, §7.6, §22.8

## Context
Hard user requirement: the distro may only see `D:\Work\autonomous-oss-contributor` and must not launch Windows executables. The VHD lives in `.wsl\ossbot` inside that folder. Review H7: a plain `wsl.exe` landing as `ossadmin` carried the operator API role.

## Decision
- `/etc/wsl.conf`: `[automount] enabled=false, mountFsTab=true`; `[interop] enabled=false, appendWindowsPath=false`; `[boot] systemd=true`; `[user] default=ossnobody` (no `ossapi`, `osscfg`, or `sudo`; egress rejected). Scripts always pass `-u root` or `-u ossadmin`.
- `/etc/fstab` managed block with one drvfs line: `D:\134Work\134autonomous-oss-contributor /mnt/d/Work/autonomous-oss-contributor drvfs metadata,uid=1000,gid=1500,umask=002 0 0`.
- Windows scripts stream every shell script over stdin (bootstrap, provision, secrets, kill, resume), so a broken mount never bricks provisioning; bootstrap verifies after `wsl --shutdown` and rolls back on failure.
- Distro autostart via a per-user Scheduled Task; idle shutdown disabled machine-wide (ADR-0013).

## Consequences
No `/mnt/c`, no Windows PATH, no `.exe` execution from Linux. Windows → Linux (`wsl.exe -d ossbot -u root`) still works for kill/resume, which also means Windows-user processes are root-equivalent over the distro (out of scope, ADR-0010). **Verify:** fstab escaping form, metadata/gid behaviour after Windows-side edits, `default=ossnobody` honoured.
