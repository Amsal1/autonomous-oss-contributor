# CR-001: Graceful lifecycle (up / down / drain) for laptop shutdown

**Merged into DESIGN.md §13.5 (with FR-17, AC 31–32, NFR-3 and §23 item 31) on 2026-10-18. Historical; do not edit.** The current spec is DESIGN.md; requirement rows are `CR1-*` in `docs/TRACEABILITY.md`.

Status: Required (user request, 2026-10-17). Applies on top of DESIGN.md; where it conflicts with an existing section, this CR wins and DESIGN.md must be updated to match.

## Problem
The design has emergency stops (soft/full/hard kill, Section 13) and autostart at logon (7.6), but no everyday, graceful way to bring the service down before the laptop shuts down and back up afterwards. Kill is the wrong tool for that: it is sticky, invalidates approvals and needs an explicit resume. Graceful shutdown of running work can take 60-90 s or more.

## Requirements
1. **Telegram transport stays long polling** (outbound HTTPS only, no webhook, no inbound port, no static IP). Survive IP changes, Wi-Fi switches, sleep/hibernate and network loss: reconnect with backoff, keep pending updates, log and report reconnects in `/status`. Add a test with the fake Bot API that drops the connection and verifies recovery.
2. **Lifecycle states** distinct from kill: `up`, `draining`, `down`. Kill state is unchanged and always overrides lifecycle.
3. **`down` (graceful):**
   - Enter `draining`: no new jobs or stages are scheduled; discovery stops.
   - Running stages get a stop signal and checkpoint. Wait up to a drain timeout (default **120 s**, configurable in config and via `-Timeout`). Stages that finish in time are recorded as done; stages that don't are marked `interrupted` and re-queued to restart from their last completed stage on next `up`.
   - An in-flight public write (fork/push/PR/comment) is never auto-resumed after `up` (existing M5 rule): it becomes `SUBMIT_FAILED (interrupted)` with a fresh-approval Retry.
   - Send a Telegram summary ("Going down: N finished, M re-queued").
   - Stop systemd units in dependency order. Every ossbot unit has `TimeoutStopSec` at least drain timeout + 30 s; gatekeeper and proxy stop last so the summary is delivered.
   - Then `wsl --terminate ossbot` (`-All` uses `wsl --shutdown`, acceptable because WSL is dedicated to ossbot).
   - Does **not** disable `ossbot-autostart` and does not invalidate armed approvals unless they expire normally.
4. **`up`:** start the distro, wait for units healthy (bounded, e.g. 180 s), run `doctor --quick`, resume re-queued stages, and send a Telegram summary ("Back up: X resumed, Y awaiting approval"). Refuses and explains if a kill is active.
5. **Entry points:**
   - Windows: `scripts/up.ps1`, `scripts/down.ps1 [-Timeout <s>] [-All]`, `scripts/status.ps1` (one-screen state: lifecycle, kill, dry-run, running stages, pending approvals, last poll time).
   - Distro: `ossctl up|down|status`.
   - Telegram: `/drain` (stop scheduling new work, let running work finish; reversible with `/undrain`) and `/down` (graceful down of the pipeline workers, Hermes and sandboxes; gatekeeper stays up so `/up` works from the phone). Full distro stop is Windows-side only.
   - Optional `bootstrap.ps1 -Shortcuts`: Start-menu shortcuts "ossbot Up", "ossbot Down", "ossbot Status", "ossbot Kill". Off by default (they live outside the project folder).
6. **Windows shutdown/restart hooks** (Windows 11 Home has no gpedit shutdown scripts). Use three layers. The user wants an automatic graceful stop on shutdown, so layers (a) and (b) are required, not optional:
   - (a) **Shutdown blocker (primary, user-initiated shutdown/restart/sign-out).** A small per-user background process, started by `ossbot-autostart` at logon, runs from the repo with no copies to AppData. It owns a top-level window and handles `WM_QUERYENDSESSION`: it calls `ShutdownBlockReasonCreate("ossbot: stopping safely…")`, runs `down.ps1 -Timeout 90`, then `ShutdownBlockReasonDestroy` and lets shutdown continue. Windows shows its "apps preventing shutdown" screen with this reason, and the user can still pick "Shut down anyway". Implement in PowerShell + WinForms or a tiny C# helper compiled at bootstrap. **[VERIFY by research and a real test on this Windows 11 Home 26100 laptop]:**
     - whether a hidden or off-screen top-level window still gets to block;
     - whether shutdown continues automatically once the block reason is destroyed;
     - behaviour with Fast Startup.

     Document the observed behaviour in an ADR.
   - (b) **Event-triggered Scheduled Task (backup)**, modelled on SpiralPool's `scripts/windows/wsl2-shutdown-hook.ps1` (BSD-3-Clause; credit it if code is adapted):
     - Trigger on System log `User32` Event ID 1074 (shutdown/restart initiated, including Windows Update restarts).
     - Run `down.ps1 -Timeout 20` with a hard cap. Only the stop phase runs, not the drain.
     - Run as the per-user task, non-elevated: `wsl -u root` needs no admin.
     - Script and log stay inside the project folder.
     - Do NOT trigger on sleep (Kernel-Power 42). The VM freezes and resumes, which is safe.
     - Document that Windows does not wait for scheduled tasks, so this is best-effort: it only gets the seconds before the VM is torn down.
   - (c) **Crash safety (always required)**, for battery death, power loss, force shutdown and BSOD:
     - SQLite in WAL mode with durable commits at stage boundaries.
     - Idempotent stages.
     - On-start reconciliation that is read-only for public writes.
     - An e2e test that SIGKILLs services and the sandbox mid-stage, restarts, and checks the task resumes or re-queues with no duplicate public writes.
   - `bootstrap.ps1` installs (a) and (b); `bootstrap.ps1 -Uninstall` removes them; `ossctl doctor` checks they are registered. `status.ps1` shows the last shutdown type, graceful (a), hook (b) or unclean (c), from a marker written by `down` and read at next `up`.
7. **Sleep/hibernate:** after resume, the scheduler catches up, timeouts measured across sleep don't falsely fail stages (use monotonic time where possible, and treat large wall-clock jumps as "slept"), and the poller reconnects.
8. **Docs:** README and docs/SETUP.md get a short "Daily use" section: up, down before shutdown, status, kill. AGENTS.md lists the lifecycle invariants.
