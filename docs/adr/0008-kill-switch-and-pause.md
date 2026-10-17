# ADR-0008: Kill switch and pause semantics
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §13

## Context
Need `/kill` on Telegram and `scripts/kill.ps1` that works even with the bot down or the distro stopped, persisted across reboots until resume, plus anomaly auto-pause. Review H5: the v1 control dir (2770, group-writable) let non-human components delete flags. Review M13: killing a stopped distro booted it with services racing the flag.

## Decision
- Levels: pause; soft kill (Telegram/ossctl/Hermes/anomaly: kill containers, remove task networks, stop runner/sandboxd/Hermes; gatekeeper and proxy stay up so `/resume` works); full kill (`kill.ps1`: also gatekeeper, proxy, dockerfilter); hard kill (`kill.ps1 -Hard`: also disables autostart and runs `wsl --shutdown`, acceptable because WSL is dedicated to ossbot).
- Authoritative flags in `/var/lib/ossbot/control/` (`gatekeeper:osscontrol 0750`, files 0640): only gatekeeper and root write; runner, sandboxd, Hermes request stop through the API.
- `kill.ps1` first writes `.ossbot-local\KILL` on Windows. Root `ossbot-boot-gate.service`, ordered before Docker and every ossbot unit, converts the marker into the `killed` flag (and fails closed if the mount is missing). Marker absence never resumes; only `ossbot-resume` / the resume path unit remove flag and marker together after a human confirm.
- A root `.path` unit enforces kill within seconds; a 30 s watchdog re-enforces, never starts units while killed or paused, kills on permission drift, and pauses after 4 failed gatekeeper health ticks.
- Asymmetric: anyone may stop; only the human may go. Resume never changes dry-run. `ossbot-kill` is self-contained bash streamed over stdin.

## Consequences
Soft kill keeps a minimal network-facing gatekeeper (Telegram + GitHub via one proxy port); acceptable because every outbound path checks the flag. Refines the brief's "stop Hermes+gatekeeper" for Telegram kill only.
