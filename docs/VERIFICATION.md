# Verification log

Results of the phase verification bars (docs/PLAN.md) and of the live checks of docs/plan/NOTE-live-secrets.md. Newest first. Secret values are never recorded here, only key names and outcomes.

## Phase 1, after review round 1 (2026-10-18)

Fixes for `docs/review-phase1.json`: P1-1 (per-uid DNS refusal), P1-2 (kill invalidation on every kill path), P1-3 (CR-001 Phase 1 share, DESIGN §13.5), P1-4 (recorded for FEAT-005), P1-5 (exact token/callback regexes).

| Phase 1 bar item | Result |
|---|---|
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports`; `ROOT shellcheck` (scripts, bins, system tests) | clean / formatted / 101 files no issues / 4 contracts kept / clean |
| `DEV pytest -m "unit or telegram"` | 1530 passed (new: lifecycle, drain, crash recovery, polling reconnect, offline bot start, systemd stop timeouts, watchdog sleep, DNS render, reconcile kill invalidation, exact regexes) |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key`: Kiro host CONNECT, S-ws-1; live Kiro probes belong to Phase 2), incl. `test_crash_safety.py` (SIGKILL sandboxd mid-job, recover) |
| `ROOT bash tests/system/*.sh` (10 scripts, new: `test_lifecycle.sh`, `test_crash_safety.sh`) | all exit 0, 199 PASS, 0 FAIL; `test_egress_per_uid.sh` now probes UDP+TCP 53 to every resolver per uid (all refused, root control answers) |
| `tests\system\test_kill.ps1` | all checks passed (hung kill 37.4 s after the proxy `ExecStop` went from 5 s to 2 s) |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13 |
| second `scripts\bootstrap.ps1` | every step `unchanged` (kiro/hermes `skipped (phase 2)`) |
| `ROOT docker version` / `docker compose version` | 29.9.1 / v5.6.0 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer | all active |
| `ossbot-firewall --check` | 0 |
| `ossctl doctor` (root, full) | PASS 28, WARN 2, SKIP 3, exit 0 |
| `scripts\ossctl.ps1 doctor` | Windows part all PASS (Hyper-V firewall now applied), distro part as above |
| `ossctl audit verify` | ok |
| `gitleaks git --redact .` | no leaks |
| CR-001 live cycle: `down.ps1 -Timeout 10` → `status.ps1` (distro stopped, not started) → `up.ps1` → `status.ps1` | down 8 s (summary delivered, nothing interrupted), last shutdown `graceful`, up 5 s with doctor --quick clean |

Doctor WARNs (documented): "sandbox Kiro auth + catalog" (the catalog is fetched by the runner, FEAT-006) and "safety probes: not run yet for the current tuple" (they need catalog models; Kiro jobs stay refused until they pass). SKIPs: the three Hermes checks (FEAT-007).

Live checks run in this step (NOTE-live-secrets.md, read-only):

- GitHub write token: `GET /user` = `Amsal1`, `X-OAuth-Scopes: public_repo` (doctor). No write call of any kind was made.
- GitHub read token: `GET /user` 200 (doctor). Reading a public repo with it is left to the FEAT-006 vetting checks.
- Telegram: `getMe` 200 (doctor); the gatekeeper polls live; the CR-001 lifecycle summaries ("Draining", "Going down", "Back up") and one offline-resume FYI went to the configured user only.
- Dry-run stayed ON throughout.

Incident during this step: adding `lifecycle:` to `config/policy.yaml` before deploying the new schema made the previously deployed gatekeeper refuse to start (config validation, fail closed); the watchdog paused the system (`gatekeeper_unhealthy`) as designed. After the deploy the pause was lifted with the offline resume (`ossbot-resume --offline`, logged in `resume.log`). The same deploy exposed that the gatekeeper's start failed whenever Telegram was unreachable (proxy restarting); the bot now connects in the background with backoff (CR-001 item 1).

### CR-001 items still open (tracked for Phase 2)

| CR-001 item | Owner | Status |
|---|---|---|
| 6a shutdown blocker (`WM_QUERYENDSESSION` + `ShutdownBlockReasonCreate` → `down.ps1 -Timeout 90`), verified live on this Windows 11 Home 26100 laptop (hidden window, auto-continue, Fast Startup), ADR | FEAT-008 | **open (required)** |
| 6b event-triggered task on System/User32 1074 → `down.ps1 -Timeout 20 -Kind hook` (non-elevated, no sleep trigger, SpiralPool BSD-3 credit if adapted) | FEAT-008 | **open (required)**; `down.ps1 -Kind hook` and the `hook` shutdown type already exist |
| `bootstrap.ps1` installs 6a/6b, `-Uninstall` removes them, `ossctl.ps1 doctor` checks them; optional `-Shortcuts` | FEAT-008 | open |
| Stage re-queue on `up` and the real `resumed` count; runner honours the lifecycle flag; SIGKILL-runner e2e | FEAT-006 | open (sandbox side done: jobs end `interrupted`, task volumes kept) |
| Telegram `/drain`, `/undrain`, `/down`, `/up` | FEAT-005 | open (API and ControlService methods exist) |
| README/SETUP "Daily use", AGENTS.md lifecycle invariants | FEAT-008 | open |
| Done in Phase 1 | | lifecycle states + API (kill overrides), sandboxd drain + crash recovery, `TimeoutStopSec`, `ossctl up/down/status`, `scripts/{up,down,status}.ps1`, boot-gate shutdown classification (graceful/hook/unclean), watchdog sleep handling, monotonic job/drain timeouts, Telegram reconnect tracking + offline start, crash-safety tests |
