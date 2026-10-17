# ADR-0016: Reproducibility: bootstrap from zero, state-outside-git inventory, encrypted backup/restore
Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §18.2, §18.7, §18.8

## Context
The user wants to rebuild from a fresh Windows install with `git clone` + scripts, and to keep history (audit log, contributions, Hermes memory) across reinstalls. v1 assumed the `ossbot` distro already existed and had no backup story. WSL 3.0.3 `wsl --help` (checked locally) offers `--install <Distro> --name --location --no-launch` and `--import-in-place <Distro> <vhdx>`.

## Decision
- `bootstrap.ps1` is idempotent from zero: if `ossbot` is not registered it runs `wsl --install Ubuntu-24.04 --name ossbot --location <repo>\.wsl\ossbot --no-launch` and provisions as root; if an orphaned `ext4.vhdx` exists it offers `--import-in-place` instead of creating a second disk. It never unregisters or deletes a distro or VHD.
- SETUP.md is the DR runbook (install WSL, clone, profile, bootstrap, credentials or restore, doctor, go live, routine, uninstall, troubleshooting).
- The design keeps an explicit inventory of state outside git with its recovery path.
- `ossctl backup` (root; `scripts/backup.ps1`): pauses, copies the DB with SQLite's online backup API, verifies the audit chain against anchors, builds a tar + manifest, encrypts with `age` to X25519 recipients from the profile (always encrypted; recipient keys let a weekly timer run without storing a secret), writes `backups/ossbot-<ts>.tar.age` (gitignored), keeps 8. Secrets and artifacts are opt-in flags.
- `ossctl restore`: kills first, decrypts with an identity streamed from Windows, verifies manifest and audit chain, moves current state aside (never deletes), installs with correct owners/modes, forces dry-run ON and stays killed until the human resumes; open approvals invalidated.
- Not backed up: Docker data/images (rebuilt), sandboxd subnet DB, the VHDs (the user may copy them while the VM is stopped), Kiro device-flow login state.

## Consequences
The user must keep the age identity safe outside the machine; losing it makes archives unrecoverable (documented). **Verify:** fresh-install WSL command, `--no-launch` distro usable as root before any OOBE user, `age -d -i -`, Hermes state layout for the include list.
