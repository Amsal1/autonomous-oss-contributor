# ADR-0010: Secrets on ext4 inside the VHD, split per component

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §18.3

## Context
The brief suggested a gitignored 0600 env file. A file on NTFS in the repo folder is readable by any Windows process, including editors and IDE AI agents, and drvfs permissions are advisory to Windows.

## Decision
- `scripts/setup-secrets.ps1` reads secrets with `Read-Host -AsSecureString` and streams them over stdin to `setup-secrets.sh` in the distro; nothing is written on Windows.
- Files `/etc/ossbot/secrets/{gatekeeper,runner,sandboxd,hermes}.env`, root:<service-group> 0640, loaded by systemd `EnvironmentFile`. Each service sees only its own secrets (write PAT and bot token only in gatekeeper; Kiro sandbox key only in sandboxd; read PAT only in runner).
- Non-secret identity in git-tracked `config/identity.yaml`.

## Consequences
Secrets still physically live inside the repo folder (the VHD) but outside git and outside Windows' view. Backups of the VHD contain secrets; documented in SETUP.md.
