# ADR-0008: Kill switch and pause semantics
Status: Accepted (amended in design rev 5 on 2026-10-18; revised in design rev 1, rev 2 and rev 3) · Date: 2026-10-17 · Design: DESIGN.md §12.5, §13

## Context
Need `/kill` on Telegram and `scripts/kill.ps1` that works even with the bot down or the distro stopped, persisted across reboots until resume, plus anomaly auto-pause. Review H5: the v1 control dir (2770, group-writable) let non-human components delete flags. Review M13: killing a stopped distro booted it with services racing the flag.

## Decision
- Levels: pause; soft kill (Telegram/ossctl/Hermes/anomaly: kill containers, remove task networks, stop runner/sandboxd/Hermes; gatekeeper and proxy stay up so `/resume` works); full kill (`kill.ps1`: also gatekeeper, proxy, dockerfilter); hard kill (`kill.ps1 -Hard`: also disables autostart and runs `wsl --shutdown`, acceptable because WSL is dedicated to ossbot).
- Authoritative flags in `/var/lib/ossbot/control/` (`gatekeeper:osscontrol 0750`, files 0640): only gatekeeper and root write; runner, sandboxd, Hermes request stop through the API.
- `kill.ps1` first writes `.ossbot-local\KILL` on Windows. Root `ossbot-boot-gate.service`, ordered before Docker and every ossbot unit, converts the marker into the `killed` flag (and fails closed if the mount is missing). Marker absence never resumes; only `ossbot-resume` / the resume path unit remove flag and marker together after a human confirm.
- A root `.path` unit enforces kill within seconds; a 30 s watchdog re-enforces, never starts units while killed or paused, kills on permission drift, and pauses after 4 failed gatekeeper health ticks.
- Asymmetric: anyone may stop; only the human may go. Resume never changes dry-run. `ossbot-kill` is self-contained bash streamed over stdin.

## Revision 2 amendments (design review round 2, M5/N8/N9/N12)
- The `killed` flag carries `level: soft|full`. A missing or unparseable level counts as full. Levels only escalate. The boot gate writes `full` when the Windows marker exists.
- Unit behaviour while killed:
  - Firewall units, watchdog and the Docker filter always run. The filter refuses every create. Stopping them would remove controls.
  - Gatekeeper and proxy use `ExecCondition=ossbot-boot-gate --allow minimal`: they run after a soft kill (also across reboots) and stay stopped after a full kill.
  - sandboxd, runner and Hermes use `ConditionPathExists=!killed`.
- Resume (path unit, `ossbot-resume`) starts the gatekeeper first, then proxy, filter, sandboxd, runner, Hermes.
- `kill.ps1` waits at most 30 s for the in-distro kill, then runs `wsl --terminate ossbot`. The marker is already written, so the next boot is killed.
- Enforce-kill disconnects the proxy from each task network before removing it.
- On start, the gatekeeper reconciles both `killed` and `paused` files into its DB state, never towards running.
## Revision 3 amendments (design review round 3, M5/N4/N5)
- M5: submission checks control state (`control_checkpoint()`) before every individual GitHub write: fork, push, PR call, and each retry. If a pause or kill landed, the action ends `failed` `interrupted` and the contribution `SUBMIT_FAILED`. Only a fresh two-tap approval (Retry, possible only after resume) can write again. On start, `executing` actions are reconciled by GitHub reads only. Resume never re-enters a write.
- N4: `ossctl resume` uses the API when `/v1/health` answers. Otherwise (full kill) it does the TTY confirm and runs `sudo ossbot-resume`. `ossbot-resume` appends to `/srv/ossbot/logs/resume.log`, and the gatekeeper emits the audit event and Telegram FYI on its next start.
- N5: the Docker filter refuses `containers/{id}/start` as well as create while `killed` or `paused` exists.

## Consequences
Soft kill keeps a minimal network-facing gatekeeper (Telegram + GitHub via one proxy port); acceptable because every outbound path checks the flag. Refines the brief's "stop Hermes+gatekeeper" for Telegram kill only.

## Implementation addendum (Phase 1 integration, 2026-10-18)
- Observed on this laptop: an unattended-upgrades run of the systemd package (255.4-1ubuntu8.18) re-ran `systemd-tmpfiles --create`. The `d /run/ossbot 1755` line re-chmods the directory, which narrows the ACL mask to `r-x`, and the `a+ u:gatekeeper:rwx,u:ossbox:rwx` line does not widen it again (live probe: fresh create 1775, second create 1755). The watchdog's 30 s permission check saw `/run/ossbot` at 1755 instead of 1775 and soft-killed, as designed (fail closed). With the narrowed mask the gatekeeper and sandboxd could no longer recreate their sockets after a restart.
- Fix: `systemd/tmpfiles.d/ossbot.conf` sets the mask explicitly (`m::rwx`); re-runs now keep 1775. The provision `systemd` step re-applies tmpfiles whenever `/run/ossbot` is not 1775, and `tests/system/test_control_perms.sh` re-runs tmpfiles and asserts no drift.

## Revision 5 amendment (consolidation, 2026-10-18)

Merged from CR-001 and CR-003 (DESIGN §13.5, §14.18.4.2).

- Lifecycle (`up` / `draining` / `down`, scopes `distro` and `workers`) is separate from kill and pause: it stops only new work, never invalidates approvals and never disables the autostart. Kill always overrides it, and `up` is refused while killed. Windows shutdown runs a graceful `down` through a per-user shutdown blocker and a User32 1074 event task (both required, both inside the repo).
- A kill now invalidates the armed and confirmed approvals of every decision service (PR, proposal, skill), and typed `/kill` is handled within 2 s even while an assistant answer streams (update-handler budget, DESIGN §14.18.5).
## Implementation addendum (FEAT-005 item 24, 2026-10-18): invalidation fan-out
- The four kill paths in `control.py` (Telegram/API kill, resume applying a root-written kill before reconcile, the resume-log path, the flag reconcile) call `ControlService._kill_invalidate`, which runs `invalidate_armed_confirmed("killed")` on PR, proposal and skill approvals inside the same transaction as before. Both reload branches (`SnapshotManager._invalidate_all`) do the same with `invalidate_all_open("config_snapshot_changed")`. The dry-run toggle still reaches only PR approvals (proposal and skill bindings carry no mode).
- `housekeeping_once` runs `expire_due` and `disarm_expired` on all three services each tick, then `ctx.card_recover()`. Tests: `tests/unit/gatekeeper/test_decision_fanout.py`, `test_disarm_expired.py`.
