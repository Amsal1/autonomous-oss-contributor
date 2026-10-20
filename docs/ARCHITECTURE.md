# Architecture

A concise map of DESIGN §7–§16. DESIGN is normative; this file is the overview.

## Platform (§7)

Everything runs in the WSL distro `ossbot` (Ubuntu 24.04, systemd on, VHD under `.wsl\` in the repo).
The distro sees only the repo folder (`/mnt/d/Work/autonomous-oss-contributor`), has Windows interop
and Windows `$PATH` entries disabled, and does not forward its localhost listeners to Windows. The
default WSL login user is `ossnobody`. Docker Engine runs inside the distro; no Docker Desktop.

Trust order: the human (Telegram) > gatekeeper > root helpers > runner and sandboxd > Hermes and all
LLM sessions > upstream content (hostile by assumption).

| Unit | User | Role |
|---|---|---|
| `ossbot-boot-gate` | root | turns the Windows kill marker into the `killed` flag before anything starts |
| `ossbot-firewall`, `ossbot-firewall-docker` | root | per-uid owner-match egress rules, `DOCKER-USER` rules; every service checks them at start |
| `ossbot-dockerfilter` | root | the only Docker API path for sandboxd; allowlisted create/start shapes; refuses while killed or paused |
| `ossbot-proxy` | root (container) | squid; one port per caller, hostname allowlists per category and egress mode |
| `ossbot-gatekeeper` | `gatekeeper` | the trusted service (below) |
| `ossbot-sandboxd` | `ossbox` | sandbox jobs |
| `ossbot-runner` | `ossrunner` | the pipeline |
| `ossbot-hermes` | `hermes` | Hermes gateway (API server only) + the V3 ACP Kiro provider |
| `ossbot-watchdog.timer` | root | every 30 s: invariants, firewall check, audit anchors, health, kill re-enforcement |
| path units | root | kill/resume enforcement, egress re-render, Hermes tier re-render, skill activation, lifecycle |

All non-root units are hardened (`ProtectSystem=strict`, empty capability set, no `AF_VSOCK`, no
io_uring) and get secrets only from `/etc/ossbot/secrets/<svc>.env`.

## Components

- **Gatekeeper** (`src/ossbot/gatekeeper/`, §12, §14). Control state (running, paused, killed,
  dry-run, lifecycle), config snapshot, contribution state machine, the three decision services
  (approvals, proposal approvals, skill approvals), policy checks, rate limits, the hash-chained audit
  log with root anchors, per-contribution bare mirrors and review-artifact derivation, the Telegram
  front-end (one bot, long polling, outbox), the chat relay to Hermes, the models writer and the only
  GitHub writer (`submit.py` → `github_writer.py`). It runs no LLM and executes no repo code.
- **Runner** (`src/ossbot/runner/`, §9). Schedules and runs the stages
  `discover → vet → scout → PROPOSAL (gate 1) → fetch → plan → implement → validate → self-review →
  PREVIEW (gate 2) → submit → watch`, talks only to the gatekeeper API and sandboxd, and holds the
  GitHub read token for discovery and watch reads.
- **sandboxd** (`src/ossbot/sandboxd/`, §10). Turns job specs into containers through the Docker
  filter: profiles `research`, `agent-llm`, `catalog` (hold the sandbox Kiro key, never run repo code,
  never mount attacker-writable paths) and `fetch`, `agent-exec`, `validate`, `package`, `code` (run
  code, hold no credential). Kiro jobs run only after the safety probes passed for the current
  `(kiro_cli_version, image digests, agent configs, settings, trust mode)` tuple. Outputs are key-scanned.
- **Split coder** (§10.4). The coder's Kiro session runs in `agent-llm`; its shell and file tools reach
  the repo only through an exec relay into a credential-free `agent-exec` container.
- **Hermes** (`hermes/`, `vendor/hermes-kiro-provider/`, §15). The general assistant behind the
  gatekeeper's relay. Its tool surface is fixed by the capability tier; the gatekeeper checks
  `GET /v1/toolsets` and soft-kills on any extra tool. Skills are staged and activated only by the
  two-tap skill card through a root hasher.
- **ossctl** (`src/ossbot/ctl/`). Operator CLI over the gatekeeper socket (role from `SO_PEERCRED`),
  doctor, backup and restore, lints and the README command table.

## Pipeline and gates (§9)

Gate 1: the scout reads a shallow, read-only snapshot of the repo and the issue (plus read-only GitHub
MCP tools) and proposes a plan with a size class; nothing from the repo runs before Start work. Gate 2:
after implement, validate and self-review, the package job creates exactly one commit with the fixed
identity; the gatekeeper re-derives the patch from its own mirror, runs policy (ceiling, forbidden
paths, secrets, identity, AI disclosure, competing PRs), and shows that preview. Each approval is a
two-tap Telegram flow bound to the exact content hash and mode; `submit.py` recomputes the binding
before every write. Watch handles review feedback as new fix rounds through the same gates.

Size (§9.14): classes `xs`…`xxl` count non-test lines; the autonomous preference ranks and filters what
the bot picks; the hard ceiling (`policy.diff`) refuses runaway diffs for everything.

## Egress (§11)

Every non-root uid and every sandbox network reaches the internet only through squid. Each caller has
its own proxy port and category list (`config/egress.yaml`); DNS is refused to everything but the proxy.
The egress mode (`allowlist`, `research`, `open` default) changes only what LLM components get; `fetch`
and `validate` never get the public web, and gatekeeper and runner egress are the same in every mode.

## Kill, pause and lifecycle (§13)

Kill (soft or full) and pause are control flags written only by the gatekeeper and root, checked by
every layer, enforced by a root path unit, and persisted across reboot by the boot gate. Only the human
undoes them. The lifecycle (`up`, `draining`, `down`) is separate: it stops new work, drains running
work (120 s), re-queues the rest and stops units in order; a shutdown blocker and an event-triggered
Scheduled Task run it on Windows shutdown or restart.

## Telegram (§14)

One bot owned by the gatekeeper; every update must come from the owner in the private chat. Topics
(Control, Reviews, OSS feed, Assistant) when enabled, a pinned dashboard, proposal and preview cards
with an explorer, file diffs, comments and interdiff, a streaming assistant with Stop, notification
priorities, quiet hours and a digest. All views come from the UI kit (`telegram/ui/`), and every
untrusted text passes one redactor before it is sent.

## Models (§16)

`config/models.yaml` maps each role to a catalog model; the gatekeeper is its only writer
(`/model`, `ossctl model set`), validated against the Kiro catalog. Live checks use the `test` role.

## Data and recovery (§18.8)

State outside git (profile, secrets, the gatekeeper DB, anchors, snapshots, open bundles, Hermes state)
is moved by `ossctl backup`/`restore` through age-encrypted archives under `backups/`. See
[`SETUP.md`](SETUP.md).
