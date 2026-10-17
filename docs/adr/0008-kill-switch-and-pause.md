# ADR-0008: Kill switch and pause semantics

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §13

## Context
Need `/kill` on Telegram and `scripts/kill.ps1` that works even with the bot down, persisted until resume, plus anomaly auto-pause. If Telegram `/kill` stopped the gatekeeper, Telegram `/resume` would be impossible.

## Decision
- Levels: pause (finish to checkpoint, nothing new, no outbound), soft kill (Telegram/ossctl/Hermes/anomaly: kill containers, stop runner/sandboxd/Hermes/proxy, gatekeeper stays up in killed mode answering only /status /resume /logs /help), full kill (`kill.ps1`: also stops gatekeeper), hard kill (`kill.ps1 -Hard`: also disables keepalive and `wsl --terminate ossbot`).
- Authoritative flag `/var/lib/ossbot/control/killed` (ext4). A root `systemd .path` unit triggers the enforcer within seconds; resume uses a `resume-request` file + a second root path unit. Every component also checks the flag before acting; a 30 s root watchdog re-enforces.
- Asymmetric: anyone may stop; only the human may go (Telegram confirm, or ossadmin at an interactive TTY / `resume.ps1`). Resume never changes dry-run.
- `ossbot-kill` is a root shell script with no Python dependency.

## Consequences
Soft kill keeps a minimal network-facing gatekeeper; acceptable because every outbound path checks the flag. Refines the brief's "stop Hermes+gatekeeper" for Telegram kill only.
