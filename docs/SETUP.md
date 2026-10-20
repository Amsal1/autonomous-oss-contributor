# SETUP: install, daily use and disaster recovery

This runbook takes a fresh Windows install plus this repository to a working ossbot (DESIGN §18.7,
FR-15, NFR-10). Everything that is not in git is listed in "State outside git" and is either
re-created by these steps or restored from an encrypted backup. Commands run from an ordinary
PowerShell in `D:\Work\autonomous-oss-contributor` unless a step says elevated.

## 1. Prerequisites

- Windows 11, admin rights once (WSL install, optional Hyper-V firewall rules).
- About 150 GB free on `D:` (the distro VHD, swap and Docker data live under `.wsl\` in the repo).
- A Telegram account, the GitHub account named in `config/profile.yaml`, a Kiro subscription with
  API keys enabled.
- A password manager for the age backup identity.

## 2. Install WSL

From an elevated PowerShell: `wsl --install --no-distribution` (or the MSI from
github.com/microsoft/WSL/releases), reboot, then check `wsl --version` reports WSL 3.0 or newer.
Docker Desktop is not needed; ossbot runs Docker Engine inside its own distro. WSL on this machine is
treated as ossbot's: `%USERPROFILE%\.wslconfig` is written from `config/wsl/wslconfig` (the previous
file is backed up to `.ossbot-local\`).

## 3. Clone and install the hooks

```powershell
git clone https://github.com/<owner>/autonomous-oss-contributor.git D:\Work\autonomous-oss-contributor
cd D:\Work\autonomous-oss-contributor
powershell -NoProfile -File scripts\install-hooks.ps1
```

The path is fixed in v1 (the drvfs fstab line and the `.wslconfig` swap path depend on it).
`install-hooks.ps1` downloads the pinned gitleaks Windows binary to `tools\bin\gitleaks.exe`
(sha256-verified against `config/versions.lock.yaml`) and points this clone's `core.hooksPath` at
`.githooks\`. The pre-commit hook then runs `gitleaks git --staged --redact --config .gitleaks.toml`
(falling back to the distro's gitleaks) and `ossctl lint personal` in the distro once it exists.

## 4. Profile

`copy config\profile.example.yaml config\profile.yaml` and replace every placeholder: GitHub login,
commit name and verified email, forbidden (work) logins, confidentiality floor, Telegram user id,
time zone and quiet hours, the age public key (`backup.age_recipients`), interests. The file is
gitignored. Skip this step if you restore a backup in step 7.

## 5. Bootstrap

From an elevated PowerShell (needed only for the Hyper-V firewall step):

```powershell
powershell -NoProfile -File scripts\bootstrap.ps1
```

Non-elevated works too; run `scripts\hyperv-firewall.ps1` elevated later. Bootstrap creates the
`ossbot` distro at `.wsl\ossbot` if it is missing (`wsl --install Ubuntu-24.04 --name ossbot
--location … --no-launch`), installs `.wslconfig`, provisions users, Docker behind the API filter,
the egress proxy and firewall, kiro-cli, Hermes, the services and the sandbox images, and starts with
**dry-run ON**. A second run must print only `unchanged`. Bootstrap never unregisters or deletes a
distro. `scripts\bootstrap.ps1 -Only provision` redeploys the code after a change.

## 6. Credentials

Create, then enter them with `powershell -NoProfile -File scripts\setup-secrets.ps1` (values go
straight to `/etc/ossbot/secrets/*.env` inside the distro, never to a Windows file; an empty answer
keeps the current value):

| Key | Where to get it | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather `/newbot` | the gatekeeper is the only poller |
| Telegram user id | @userinfobot or similar | not a secret: goes into `profile.yaml telegram.user_id` |
| `GITHUB_WRITE_TOKEN` | classic PAT, scope `public_repo` only | gatekeeper only; fork, push, PR, comment |
| `GITHUB_READ_TOKEN` | fine-grained PAT, "Public repositories (read-only)" | runner reads, scout/self-review GitHub MCP, Hermes MCP |
| `KIRO_API_KEY_SANDBOX` | Kiro console, key "ossbot-sandbox" | sandboxd's key-holding containers only |
| `KIRO_API_KEY_HERMES` | Kiro console, key "ossbot-hermes" | Hermes only |
| `GITHUB_REVIEW_MIRROR_TOKEN` | optional fine-grained PAT, only your private mirror repo | ADR-0019 C, off by default |
| `REVERSE_IMAGE_SEARCH_KEY` | optional; no provider wired in v1 | leave empty |

Also: verify the commit email on your GitHub account (Settings → Emails), and create the backup
identity with `age-keygen` (keep the `AGE-SECRET-KEY-…` line in your password manager; put the
`age1…` public key into `profile.yaml`). Apply profile changes with `ossctl config reload`.

### Telegram topics (recommended)

In @BotFather, open the bot's settings (the Mini App bot settings) and turn **topics** on, and allow
users to create topics. The gatekeeper then creates 🚦 Control, ✅ Reviews, 📈 OSS feed and
💬 Assistant, pins the dashboard in Control, and treats every topic you create as a new Hermes
conversation. With topics off everything still works in one chat with section headers. Topic routing
never changes who may approve: only your user id in the private chat.

## 7. Or restore instead of steps 4 and 6

```powershell
powershell -NoProfile -File scripts\restore.ps1 -Archive backups\ossbot-<ts>.tar.age -Identity <path to age identity>
```

The archive must be inside the repo folder (the distro sees nothing else). Restore asks for
`Type RESTORE`, kills ossbot, decrypts and verifies the archive, moves the current state aside to
`/srv/ossbot/restore-previous-<ts>/` (never deletes), installs the archive, runs the migrations and the
audit chain check, forces dry-run ON, invalidates every open approval and **stays killed** until you
run doctor and `/resume`. A passphrase-protected identity needs a terminal; use an unprotected copy for
the restore and delete it afterwards. In the distro the same is `ossctl restore <archive>`.

## 8. Doctor and the first dry run

```powershell
powershell -NoProfile -File scripts\ossctl.ps1 doctor
```

Repeat until every check is PASS or a documented WARN (items waiting for a secret say so). Inside the
distro, `ossctl doctor --probes` adds the live per-uid egress probes. Then send `/status` to the bot,
run `ossctl discover --repo <owner/repo>` (or `/work <issue URL>`), review the proposal card, tap
Start work, review the preview card and approve it in DRY RUN.

## 9. Turning live

Only when you trust the previews: `/dryrun off` and confirm. Every toggle invalidates open approvals.
`/dryrun on` is immediate.

## 10. Daily use

- **Lifecycle.** ossbot starts with Windows (Scheduled Task `ossbot-autostart`, which also starts the
  per-user shutdown blocker) and goes down gracefully on Windows shutdown or restart (the blocker plus
  the event-triggered task `ossbot-shutdown-hook`). By hand: `scripts\up.ps1`, `scripts\down.ps1
  [-Timeout S]`, `scripts\status.ps1`; in the distro `ossctl up|down|status`; on Telegram `/up`,
  `/down`, `/drain`, `/undrain`. `down` drains running work (120 s by default), re-queues the rest,
  sends a summary and stops the units in order; it never invalidates approvals.
- **Stop.** `/pause` (no new stages), `/kill` or `scripts\kill.ps1` (everything stops, survives
  reboot). Undo only with `/resume` or `scripts\resume.ps1` (typed confirm).
- **Work selection.** `/size`, `/filters` and `/repos` tune autonomous discovery; `/work <url>` asks for
  something specific. The ceiling in `config/policy.yaml` changes only by editing the file plus `/reload`.
- **Egress and tiers.** `/egress allowlist|research|open` and `/hermes tier locked|research|self_improve`
  (both confirmed); `ossctl egress status`, `ossctl hermes tier` show the active values.
- **Models.** `/model` or `ossctl model set <role> <id>`; `ossctl model list`.
- **Notifications.** `/quiet`, `/notify`, `/digest`, quiet hours in `/settings` or `profile.yaml`.

## 11. Backups and routine

- `powershell -NoProfile -File scripts\backup.ps1 [-IncludeSecrets] [-IncludeArtifacts]
  [-IncludeAttachments]`, or `ossctl backup now` in the distro, writes
  `backups\ossbot-<yyyyMMdd-HHmmss>.tar.age`, encrypted to `profile.backup.age_recipients` (the newest
  8 are kept; with no recipient nothing is written). It pauses ossbot briefly, verifies the audit chain
  on the DB copy and lifts only its own pause.
- `ossctl backup schedule on|off` enables or disables the weekly `ossbot-backup.timer` (installed
  disabled; writes without secrets).
- Upgrades: bump `config/versions.lock.yaml`, then `ossctl kiro upgrade` and `ossctl hermes upgrade`.
  Kiro jobs stay blocked until the safety probes pass for the new tuple.

## State outside git

| State | Location | Recovery |
|---|---|---|
| Personal profile | `config/profile.yaml` | backup, or re-create from the example |
| Secrets | `/etc/ossbot/secrets/*.env` | backup with `-IncludeSecrets`, or re-run setup-secrets |
| Gatekeeper DB (contributions, approvals, audit, outbox, catalog, policy floor) | `/srv/ossbot/gatekeeper/ossbot.db` | backup |
| Audit anchors | `/srv/ossbot/logs/audit-anchors.log` | backup (verified against the DB on restore) |
| Config snapshots | `/srv/ossbot/config-snapshot/` | backup (re-creatable from the files) |
| Bundles and import bundles of open contributions | `/srv/ossbot/artifacts/…`, `/srv/ossbot/gatekeeper/imports/…` | backup |
| Hermes memory, sessions, skills, config; feature stores; `config/telegram.yaml` | `/home/hermes/.hermes/`, `/srv/ossbot/gatekeeper/features/` | backup |
| sandboxd DB, probe results, caches, mirrors, Docker data | `/srv/ossbot/…` | rebuilt (probes re-run automatically) |
| Kiro login state for Hermes (auth option c only) | `/home/hermes/.kiro/` | log in again (`ossctl hermes login`) |
| Distro and swap VHD | `.wsl\` | not backed up by ossbot; copy the VHD while the VM is stopped if you want |
| `.wslconfig` backups, kill marker | `.ossbot-local\` | not backed up |
| Windows registrations (Scheduled Tasks, Hyper-V rules, `.wslconfig`) | Windows | re-created by bootstrap |

## 12. Uninstall

`powershell -NoProfile -File scripts\uninstall-host.ps1` (or `scripts\bootstrap.ps1 -Uninstall`)
restores `.wslconfig` and removes the Scheduled Tasks and Hyper-V rules. Deleting the distro
(`wsl --unregister ossbot`, which deletes the VHD) is yours to do by hand; no script does it.

## 13. Troubleshooting

- **Mounts.** The distro must see only `/mnt/d/Work/autonomous-oss-contributor`.
  `scripts\test-isolation.ps1` checks the mount, interop, `$PATH`, the default user and the resources;
  after a WSL update run `wsl --terminate ossbot` and the test again.
- **`.wslconfig` not applied.** `wsl --shutdown`, wait 8 seconds, start the distro, re-run the
  isolation test. Edit `config/wsl/wslconfig`, never `%USERPROFILE%\.wslconfig`, and re-run bootstrap.
- **Hermes auth.** Option a/b use `KIRO_API_KEY_HERMES`; option c is the device-flow login
  `ossctl hermes login` with `profile.kiro.idc_start_url`/`idc_region`. `ossctl hermes probe` checks it.
- **Kill or resume from Windows.** `scripts\kill.ps1` works even with a broken venv (the kill script is
  streamed over stdin); `scripts\resume.ps1` asks for `Type RESUME`.
- **Doctor FAIL on a token.** Re-enter it with `setup-secrets.ps1`, then `ossctl config reload`.
