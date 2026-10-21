# AGENTS.md

For humans and AI agents extending this harness. The spec is [`docs/DESIGN.md`](docs/DESIGN.md)
(revision 5); decisions live in [`docs/adr/`](docs/adr/); the component overview is
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). When this file and DESIGN disagree, DESIGN wins and
this file is fixed.

## Invariants that must never be broken

The short list first:

- Only the gatekeeper holds the GitHub write token, and only `gatekeeper/submit.py` imports
  `gatekeeper/github_writer.py` (import-linter). Never add a GitHub write path anywhere else.
- Approvals are never LLM-mediated and have no API endpoint. There are **two human gates**: gate 1
  (proposal, before implementation compute) and gate 2 (the public action), both two-tap and bound to
  exact content and mode.
- Dry-run defaults ON; any toggle invalidates every open approval.
- The kill switch is checked at every stage, job and outbound action, and survives reboot.
- No container both holds a credential and runs repo code. Repo code (`fetch`, `validate`) reaches the
  public web only in egress mode `sandbox_open` (owner-chosen, not the default), behind the always-denied
  floors and the per-job anomaly guard, and never for an `egress: strict` repo (ADR-0023). An anomaly
  holds only that contribution; only the human resumes it (Telegram two-tap or `ossctl egress release`).
- Everything the human reviews is derived by the gatekeeper from its own mirror.
- **The size ceiling is not Telegram-raisable.** `policy.diff.max_files / max_changed_lines /
  max_test_lines` change only by editing `config/policy.yaml` plus a human-confirmed `/reload`.
  `/size`, `/filters` and `/repos … size` are preferences for autonomous discovery only
  (`gatekeeper/discovery_prefs.py` is their only writer and rejects ceiling keys).
- **Lifecycle is not kill.** `up`, `draining` and `down` only stop new work; they never invalidate
  approvals and never disable autostart. Kill always overrides them (`up` is refused while killed).
  Drain timeout is capped at 120 s (`TimeoutStopSec=150` in every unit).
- **No exposure.** No Mini App, tunnel (cloudflared, ngrok, Tailscale Funnel/serve), webhook or inbound
  port. Telegram uses long polling only (`tests/unit/test_no_tunnels.py`, `tests/system/test_no_exposure.sh`).
- No personal values or secrets in tracked files outside `docs/` (`ossctl lint personal`, gitleaks).

DESIGN §8.3 I1–I13 (the boundary rules; the work login is `profile.github_login`):

| # | Invariant | Enforced by |
|---|---|---|
| I1 | No GitHub write (fork, push, PR, comment, issue claim, PR update) happens unless an approval row is `confirmed`, unexpired, single-use, and its binding hash equals the hash recomputed at execution time from the gatekeeper's own mirror, the current config snapshot, and the current dry-run flag. | `submit.py`; `github_writer.py` importable only from `submit.py` |
| I2 | Within the distro's non-root users, only the gatekeeper can read the write token. Root and root helpers (dockerfilter, enforcer, watchdog) are TCB. sandboxd has no route to root: it is not in `docker`, and the Docker filter refuses any request that could mount host paths or escalate. | `gatekeeper.env` root:gatekeeper 0640 + Docker filter |
| I3 | An approval is accepted only from a Telegram `callback_query` whose `from.id` equals the configured user ID, in the private chat with that user, on the exact message ID the gatekeeper sent, carrying an unused, unexpired opaque token, followed by a second confirm tap whose text states the mode (LIVE or DRY RUN). | gatekeeper Telegram handlers |
| I4 | Kill/pause blocks: stage starts (runner), container creation and start (sandboxd and Docker filter), every outbound write (gatekeeper, checked again before each individual write of a submission; an interrupted submission is never resumed automatically). A root enforcer stops processes and containers. Killed state survives reboot via the boot gate. | every layer + systemd path unit + boot gate |
| I5 | Sandboxes get no host mounts, no Docker/filter/gatekeeper sockets, no GitHub tokens, egress only through the proxy with a per-profile allowlist ([ADR-0023](docs/adr/0023-egress-mode-sandbox-open.md): in egress mode `sandbox_open` `fetch`/`validate` also get `web_public`, behind the always-denied floors and the per-job anomaly guard; `egress: strict` repos keep the base list in every mode; every fetch/validate job states its egress, fail closed). Repo code runs only in containers without any credential. Key-holding containers (`research`, `agent-llm`) never mount an attacker-writable path. A Kiro job runs only if the live safety probes passed for the exact CLI/image/agent-config/settings/trust-mode tuple. | sandboxd + Docker filter + iptables + squid + agents + probe gate + egress meter and contribution holds |
| I6 | Commits are created by a deterministic package job in a fresh git repo with the fixed identity; the LLM never authors commits. The gatekeeper re-verifies that `base..head` is exactly one commit with parent `base_sha`, `base_sha` is in upstream history, and author == committer == last `Signed-off-by` == `profile.yaml`. Note (ADR-0006 addendum): with the optional `GITHUB_COMMIT_SIGNING_KEY` (gatekeeper.env only) the gatekeeper SSH-signs that commit in its mirror (identical tree, parent, identities, message; `git verify-commit`), and the signed SHA is what is shown, bound and pushed. | package job + `gitops.py`/`policy.py`/`signing.py` |
| I7 | Dry-run defaults ON at first start and after any DB reset; turning it off requires a human (Telegram confirm or ossadmin TTY). Any dry-run toggle invalidates every open approval. | gatekeeper control state |
| I8 | LLM output is data, never control: it is parsed only through pydantic schemas into fields that cannot express approve/resume/limit changes; it is never executed on the host. Commands the planner suggests run only inside credential-free sandboxes. | runner schemas; sandboxd |
| I9 | Asymmetric control: any component may request **stop** (pause/kill) via the gatekeeper API; only the human may request **go** (approve, resume, dry-run off, config reload, raising limits, enabling stack validation). Non-human components cannot write, rename, or delete control files. | authz roles + control dir 0750/0640 |
| I10 | The work identity is never used: token login must equal `profile.github_login` (checked at startup and before each write, cached 1 h); outbound text (title, body, comment, commit message) is scanned for the confidentiality floor + denylist. | gatekeeper policy (profile ∪ monotonic `policy_floor`) |
| I11 | Audit log is append-only, hash-chained, and its head is anchored by root in an append-only file. | `audit.py` + root watchdog |
| I12 | Everything the human reviews (diffstat, patch, commit message, author, title, body) is generated by the gatekeeper from its per-contribution mirror, i.e. from the exact objects it will push. Sandbox-provided renderings are only compared, never shown. | `gitops.py` |
| I13 | State transitions that concern approval or submission are driven only by the gatekeeper itself or by the human. | `state_machine.ALLOWED` actor table |

Never weaken an invariant without a new ADR.

## Rules for agents working in this repo

- Never commit secrets or personal values. Owner values live only in the gitignored
  `config/profile.yaml`; tests use `tests/fixtures/profile.test.yaml`.
- Never push, add remotes or rewrite history unless the user asks. The user pushes `origin` himself.
- Do not overwrite pre-existing `.kiro/` files.
- Never run `wsl -u root` or `sudo` for anything other than the documented scripts, never read
  `/etc/ossbot/secrets/**`, never touch `/srv/ossbot/gatekeeper/**` or `/var/lib/ossbot/control/**`,
  and never edit `%USERPROFILE%\.wslconfig` directly (edit `config/wsl/wslconfig` and re-run bootstrap).
- Never turn dry-run off, and never call the GitHub write API outside `tests/fakes/fake_github.py`.

## Architecture map

| Component | Directory | Unix user | May touch |
|---|---|---|---|
| Gatekeeper (TCB) | `src/ossbot/gatekeeper/` | `gatekeeper` | `gatekeeper.env` (write token, bot token), `/srv/ossbot/gatekeeper` (DB, mirrors), config snapshot, control dir (owner), `config/models.yaml` (only writer) |
| Runner | `src/ossbot/runner/` | `ossrunner` | read token, config snapshot, artifacts (read), gatekeeper and sandboxd sockets |
| sandboxd | `src/ossbot/sandboxd/` | `ossbox` | sandbox Kiro key, artifacts, its DB, Docker only via the filter socket |
| Docker filter | `src/ossbot/dockerfilter/` | root | the Docker socket, behind an allowlist of create/start shapes |
| Watchdog, enforcer, boot gate | `src/ossbot/watchdog/`, `scripts/bin/` | root | everything; no LLM, no network decisions |
| Hermes | `hermes/`, `vendor/hermes-kiro-provider/` | `hermes` | `/home/hermes`, `hermes.env`; the gatekeeper only through its API |
| Sandbox tools | `src/ossbot/sandbox_tools/` | `agent` (10001) in containers | the container only; stdlib-only |
| ossctl | `src/ossbot/ctl/` | root or `ossadmin` | the gatekeeper socket with its own uid; root helpers via sudo |
| Config | `config/` | read-only to services (snapshot) | `policy.yaml`, `targets.yaml`, `egress.yaml`, `models.yaml`, `telegram.yaml`, `versions.lock.yaml` |

Trust order: the human (Telegram) > gatekeeper > root helpers > runner and sandboxd > Hermes and all
LLM sessions > upstream content (hostile by assumption).

## Conventions

- Python 3.12, uv (`uv run --frozen`), dependencies pinned with `==` in `uv.lock`.
- pydantic models with `extra="forbid"` for every config and every LLM or Hermes payload.
- Structured logging; argv lists only, never `shell=True`; LLM output is data.
- LF line endings (`.gitattributes`), CRLF only for `*.ps1`.
- An ADR (or an "Implementation addendum" of an existing one) for every decision change.
- A `[VERIFY]` item is checked (web search or live probe) before the code that depends on it, and the
  result goes into `config/versions.lock.yaml` or the ADR.
- Every new privileged writer module gets its own import-linter contract (see `pyproject.toml`).

## How to

Each recipe lists the files to touch and the tests to add.

**Add a Telegram command.** Decorate the handler with `@command(name, help=..., safety=..., effect=...)`
from `gatekeeper/telegram/commands/registry.py` in a module listed in
`gatekeeper/telegram/commands/__init__.py` `BUILTIN_MODULES` (or an entry point in the group
`ossbot.telegram_commands`). Pick the safety class (`read`, `personal`, `tune`, `request`, `stop`,
`config`, `go`): `go` and `config` always produce a confirm button. Mark `effect="io"` for anything that
awaits more than SQLite. Add a golden card test under `tests/telegram/`, then run
`ossctl docs commands` to regenerate the README table (CI runs `--check`).

**Add an integration.** A Telegram command plugin (entry point group `ossbot.telegram_commands`) or a
new `ChatBackend`. Never a new GitHub write path. A new external provider needs an optional, documented
key in `scripts/setup-secrets.ps1` and an egress category in `config/egress.yaml`.

**Add a Telegram feature** (DESIGN §14.18.3).
1. Package `src/ossbot/gatekeeper/features/<name>/` with `FEATURE = FeatureSpec(name, version,
   schema_models, store_migrations, cards, actions)`, registered under the entry point group
   `ossbot.telegram_features` in `pyproject.toml` and listed in `config/telegram.yaml features.enabled`.
2. Data: pydantic models (`extra="forbid"`, bounded strings and numbers) for every payload Hermes may
   send; validation errors return 422 to the plugin and are never shown raw.
3. Store: SQLite at `/srv/ossbot/gatekeeper/features/<name>.db` with numbered migrations; no other DB.
4. Cards: `View <- (data, prefs)` built only with `telegram.ui.kit`; golden tests in
   `tests/telegram/features/test_<name>_cards.py` (classic and rich, limits).
5. Actions: `read` or `personal` only. Features cannot import `approvals`, `submit`, `github_writer`,
   `review_mirror_writer`, `state_machine` or `control` (import-linter).
6. Hermes side: a skill (approved through the skill card) or a plugin tool calling
   `ossbot_feature_card(feature, kind, data)`.
7. Docs and tests: one line in README's feature list; run `pytest -m "unit or telegram"`.

**Add a target.** An entry in `config/targets.yaml` (category, priority, AI policy), then
`ossctl targets verify` (read-only, as ossrunner) and `/reload` (human-confirmed). The AI policy is
read by vet (`common/ai_policy.py`); an optional `ai_policy: {unstated: skip, unclear: skip}` on the
entry overrides `policy.vet.ai_policy` (default: allow both). An explicit ban always skips and can't be
configured away. `ossctl targets facts <repo>…` (≤ 5, read-only, no LLM, no contribution row) prints
the deterministic AI-policy status and the CI-on-PR facts (`runner/ci_detect.py`) that vet and
discovery would see; CI that ran on merged PRs is a ranking boost (`policy.discovery.ci_on_pr_weight`),
never a filter. A config entry is also where per-repo facts go: `egress_extra` hosts, `egress: strict`
for a repo that must never get a widened egress, `cla_signed: true`. A repo that needs none of that
can instead be kept in the gatekeeper DB (DESIGN §17.3): ⭐ Keep on its proposal card,
`ossctl targets add <repo>` (TTY confirm) or `/work` on it; such rows are written only through
`gatekeeper/discovered_targets_writer.py`, imported only by `gatekeeper/discovered_targets.py`
(import-linter contract), and code never writes `config/targets.yaml`. `ossctl targets search
[--top N] [--topic T]` is the read-only look at what open discovery would find (GET only, no row,
no Telegram). Tests: `tests/unit/common/test_config.py` for a config entry,
`tests/unit/gatekeeper/test_discovered_targets.py` for a DB-kept repo.

**Add a pipeline stage.** A state in the gatekeeper state enum and its `ALLOWED` edges with actors
(`gatekeeper/state_machine.py`), a stage module in `src/ossbot/runner/stages/`, the sandbox profile
choice (`sandboxd/profiles.py`), kill/pause checks before the stage and at each heartbeat, and an e2e
fixture under `tests/e2e/`.

**Add a model role.** A role in `config/models.yaml` and `common/config.py` (`RoleName`,
`ROLE_NAMES`), resolver coverage (`tests/unit/common/test_models_resolve.py`), an agent JSON in
`.kiro/agents/ossbot-<role>.json`, a prompt in `prompts/<role>.md`, and a `TRUST_TOOLS` entry in
`sandboxd/profiles.py` (`tests/unit/sandboxd/test_kiro_settings.py`). Never name a model id outside
`config/models.yaml` (`tests/unit/test_no_hardcoded_models.py`).

**Add a model backend** ([ADR-0022](docs/adr/0022-model-backend-interface.md); official CLI or API
only, never a ToS-violating proxy). Pipeline: a `BackendSpec` in `common/backends.py` (entrypoints,
`key_profiles`, `credential_env`, `credential_patterns`, `required_probes = PROBES`), the name in
`ModelsConfig.backend` (`common/config.py`) and `IMPLEMENTATIONS` (`runner/backends/__init__.py`), an
implementation of `runner/backends/base.py ModelBackend`, an image entrypoint that feeds the same
probe prompts, a reviewed Docker-filter `KEY_PAIRS` and env-name change plus `sandboxd/argv.py`, an
egress category in `config/egress.yaml`, and an optional key in `scripts/setup-secrets.ps1`. Assistant:
a Hermes provider plugin under `vendor/`, `model.provider` in `hermes/config/config.yaml.j2`, an
`assistant` spec and a root probe like `ctl/hermes_probe.py`. Tests: `tests/unit/common/test_backends.py`,
`tests/unit/runner/test_backends.py`, `tests/unit/sandboxd/test_backend_surface.py`,
`tests/unit/gatekeeper/test_models_admin_backend.py`, `tests/acp/` for an assistant, then
`ossctl doctor --probes`. Never touch the gatekeeper, approvals, probes list or key-scan needles.

**Add an egress host.** A category in `config/egress.yaml` (or a host in an existing one), evidence
from learn mode or the proxy log (`ossctl egress jobs` shows per-job hosts and bytes), then `/reload`.
Categories are per profile; the egress modes (`allowlist`, `research`, `open`, `sandbox_open`) only add
`web_public`, and only to their closed set of grantees. `fetch` and `validate` get `web_public` only in
egress mode `sandbox_open`, with the anomaly guard; strict repos never (ADR-0023).

**Add a Telegram view.** Build it with `gatekeeper/telegram/ui/kit.py` (blocks, buttons, tokens via
`ui/send.py`), route every untrusted text through `ui/redact.py`, and add classic and rich goldens.
Non-approval tokens carry `approval_id = NULL` (§14.18.4 delta 1).

## How to test

Command prefixes (Windows PowerShell; see `docs/PLAN.md` "Conventions"): `DEV` runs in the ossadmin
dev venv inside the distro, `ROOTDEV` in root's, `ROOT` runs a command as root.

| What | Command |
|---|---|
| Lint | `DEV ruff check src tests ; DEV ruff format --check src tests ; DEV mypy ; DEV lint-imports` |
| Hermetic suites | `DEV pytest -m "unit or telegram or acp or e2e" -n auto` |
| Sandbox and Docker e2e | `ROOTDEV pytest -m "sandbox or e2e_docker"` |
| System tests | `ROOT bash tests/system/test_<name>.sh`, `powershell -NoProfile -File tests\system\test_kill.ps1` |
| WSL isolation | `powershell -NoProfile -File scripts\test-isolation.ps1` |
| Doctor | `ROOT /opt/ossbot/app/.venv/bin/ossctl doctor --probes`, `scripts\ossctl.ps1 doctor` |
| Hygiene | `DEV ossctl lint personal ; DEV ossctl docs commands --check ; ROOT gitleaks git --redact --config .gitleaks.toml .` |

Services run the deployed copy in `/opt/ossbot/app`; redeploy with
`scripts\bootstrap.ps1 -Only provision` before a live check against changed code.

## Where things live

| Topic | DESIGN section |
|---|---|
| Platform, users, units | §7 |
| Invariants, threats | §8 |
| Pipeline, size budget, user-directed work | §9, §9.14 |
| Sandboxes, egress | §10, §11 (modes §11.5) |
| Gatekeeper, approvals, policy | §12 |
| Kill, pause, lifecycle | §13 |
| Telegram workspace and UI kit | §14 (kit §14.18) |
| Kiro, Hermes, tiers | §15 (tiers §15.5) |
| Models | §16 |
| Targets | §17 |
| Harness, setup, backup | §18 |
| Tests | Part III |
