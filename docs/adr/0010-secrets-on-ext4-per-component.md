# ADR-0010: Secrets on ext4 inside the VHD, split per component; Windows-user processes out of scope
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §8.2 (T7), §18.3

## Context
The brief suggested a gitignored 0600 env file in the repo. A file on NTFS is visible to every Windows tool: workspace indexers, sync/backup, IDE agents that read or `git add` files. Review H7: v1 claimed Windows processes "cannot" read the secrets or approve. That is false: any process running as the Windows user can run `wsl.exe -d ossbot -u root` and read anything in the distro.

## Decision
- `scripts/setup-secrets.ps1` reads secrets with `Read-Host -AsSecureString` and streams the Linux script and the values over stdin; nothing is written on Windows.
- Files `/etc/ossbot/secrets/{gatekeeper,runner,sandboxd,hermes}.env`, `root:<service-group> 0640`, loaded by systemd `EnvironmentFile`. Each service sees only its own secrets. The gatekeeper also gets `KIRO_KEY_SHA256` (hashes only) for output checks.
- Threat scope stated plainly: processes running as the Windows user are root-equivalent over the distro and are not defended against. Ext4 storage defends only against passive and accidental exposure. Supporting measures: unprivileged default WSL user, AGENTS.md/steering rules forbidding dev agents from `wsl -u root`, secrets, and gatekeeper state, Telegram-only approval, and a Telegram FYI for every local operator go action.
- Non-secret personal values (identity, Telegram user ID) in the gitignored `config/profile.yaml` (ADR-0015); secrets and profile are both recoverable from encrypted backups (ADR-0016).

## Consequences
Secrets still live inside the repo folder (the VHD) but outside git and outside Windows' file view. VHD backups contain secrets; documented in SETUP.md.
