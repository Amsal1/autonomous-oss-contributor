# Contributing to ossbot

Thanks for helping. ossbot publishes to GitHub from its owner's account, so its safety rules come first:
read [AGENTS.md](AGENTS.md) (invariants I1-I13 and the recipes) before you change code. The spec is
[docs/DESIGN.md](docs/DESIGN.md), decisions are in [docs/adr/](docs/adr/), and the component overview is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). By taking part you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md), never in a public issue.

## Dev setup

- Full setup on a Windows laptop with WSL: [docs/SETUP.md](docs/SETUP.md) (`scripts\bootstrap.ps1`
  creates the dedicated `ossbot` distro, the users, the dev venv and the services).
- Python 3.12 with uv 0.13.1. Dependencies are pinned with `==` and locked in `uv.lock`; always use
  `uv run --frozen`. A new dependency is pinned exactly and justified in the PR.
- Command prefixes used in the docs (see `docs/PLAN.md`, "Conventions"): `DEV` runs a command in the
  ossadmin dev venv inside the distro, `ROOTDEV` in root's venv, `ROOT` runs it as root. The hermetic
  suites also run in any Linux checkout with `uv sync --frozen` (that is what CI does).
- Install the pre-commit hook once: `powershell -NoProfile -File scripts\install-hooks.ps1`. It runs
  gitleaks on the staged changes and `ossctl lint personal --staged`.
- Never put a personal value or a secret into a tracked file. Owner values live only in the gitignored
  `config/profile.yaml` (template: `config/profile.example.yaml`); tests use
  `tests/fixtures/profile.test.yaml` with fake values.
- Line endings are LF, except `*.ps1` (CRLF, PowerShell 5.1).

## Tests and lint

| What | Command |
|---|---|
| Lint gate | `DEV ruff check src tests ; DEV ruff format --check src tests ; DEV mypy ; DEV lint-imports` |
| Shell | `ROOT shellcheck <file>` for every changed shell file |
| Hermetic suites | `DEV pytest -m "unit or telegram or acp or e2e" -n auto` |
| Sandbox and Docker e2e | `ROOTDEV pytest -m "sandbox or e2e_docker"` |
| System tests | `ROOT bash tests/system/test_<name>.sh` |
| Hygiene | `DEV ossctl lint personal ; DEV ossctl docs commands --check ; ROOT gitleaks git --redact --config .gitleaks.toml .` |
| Demo media | `powershell -NoProfile -File scripts\dev\make-demo.ps1` (`-Check`: are `docs/media` current?) |

Regenerate demo media with scripts/dev/make-demo.ps1 when the cards change. It renders `docs/media`
(the README GIF, MP4 and stills) from the Telegram payloads the hermetic e2e capture
(`tests/e2e/test_demo_capture.py`) records, so the pictures never drift from the real renderers. It
needs Windows Chrome 132 or newer; the pinned ffmpeg is downloaded into `tools\bin\ffmpeg` and
sha256-checked (`config/versions.lock.yaml`).

Tests mirror the source tree (`tests/unit/<package>/test_<module>.py`). Every test has a 120 s timeout.
Tests never talk to the real GitHub write API: use `tests/fakes/fake_github.py`.

### Test cadence

1. While iterating, run only the tests for the files you touched
   (`DEV pytest tests/unit/<area> -x -k <name>`).
2. Fix lint before running suites.
3. Run the hermetic suite once at the end of a round, in parallel (`-n auto`), with its output in a log
   file you tail, not piped through `Select-Object`.
4. Live checks run against the deployed copy: redeploy once with
   `scripts\bootstrap.ps1 -Only provision` first.

## Commits and sign-off

- Conventional Commits: `feat(scope): …`, `fix(scope): …`, `docs: …`, `test(scope): …`, `chore: …`,
  `refactor: …`. One logical change per commit; the subject says what changed.
- Every commit carries a Developer Certificate of Origin sign-off
  ([developercertificate.org](https://developercertificate.org)): commit with `git commit -s`, which adds
  `Signed-off-by: Your Name <you@example.org>`. It must match the commit author. (ossbot's own upstream
  commits already end with `Signed-off-by`.)
- Contributions are accepted under the Apache License 2.0 (section 5 of [LICENSE](LICENSE)).
- An ADR (or an "Implementation addendum" of an existing one) for every decision change, and a
  `config/versions.lock.yaml` entry (version, URL, sha256, source, date) for every new external pin.

## AI-assisted contributions

AI-assisted contributions are welcome, provided they are disclosed. Say in the PR which tools you used
and for what (the PR template has a checkbox). You, the human submitter, are responsible for every line:
you have read it, you understand it, you ran the tests and you can sign off the DCO for it. PRs that are
plainly unreviewed agent output will be closed.

## How to extend

The step-by-step recipes (files to touch and tests to add) are in [AGENTS.md, "How to"](AGENTS.md#how-to):

- **A target repository:** "Add a target" (`config/targets.yaml`, `ossctl targets verify`, `/reload`).
- **A Telegram command:** "Add a Telegram command" (`@command(...)` with a safety class, a golden card
  test, then `ossctl docs commands` to regenerate the README table).
- **A Telegram feature:** "Add a Telegram feature" (a `FeatureSpec` package with read/personal actions
  only).
- **A model role:** "Add a model role" (`config/models.yaml`, an agent JSON, a prompt, a `TRUST_TOOLS`
  entry; model ids only in `config/models.yaml`).
- **A model backend:** the section below.

## Adding a model backend

Claude, Codex, OpenAI-compatible or other backends plug in through the interface in
[ADR-0022](docs/adr/0022-model-backend-interface.md). The foundations do not move: the gatekeeper,
approvals, state machine, sandbox profiles, Docker filter rules, egress model and the probe list stay as
they are. Open a "New model backend" issue first.

Requirements:

- **The official CLI or public API only**, under the user's own account and its terms. No ToS-violating
  proxies, MITM model routers, reused desktop sessions, scraped web endpoints or other products'
  credentials.
- **Pipeline side** (headless role jobs in sandboxd):
  - a `BackendSpec` in `src/ossbot/common/backends.py` (entrypoints, `key_profiles`, `credential_env`,
    `credential_patterns`, `required_probes = PROBES`);
  - the name in `ModelsConfig.backend` (`common/config.py`) and in `IMPLEMENTATIONS`
    (`runner/backends/__init__.py`), with an implementation of `runner/backends/base.py ModelBackend`;
  - an image entrypoint that feeds the same probe prompts. The backend must pass all five safety probes
    (`research_deny`, `symlink`, `coder_surface`, `settings`, `workspace`) for its exact
    CLI/image/agent-config/settings/trust-mode tuple before any job runs;
  - its credential is key-scanned through `credential_env`, passed by name only, and gets an optional,
    documented entry in `scripts/setup-secrets.ps1`;
  - a reviewed change to the Docker filter's `KEY_PAIRS` and env-name check plus `sandboxd/argv.py`
    (root TCB), and an egress category in `config/egress.yaml` for the `research`, `agent-llm` and
    `catalog` profiles only (never `fetch` or `validate`);
  - model ids only in `config/models.yaml`.
- **Assistant side** (Hermes): a Hermes model-provider plugin vendored under `vendor/` with its
  upstream commit and license, `model.provider` in `hermes/config/config.yaml.j2`, a `BackendSpec` of
  kind `assistant`, the ADR-0018 tool-surface allowlist, and a root probe like `ctl/hermes_probe.py`
  whose failure closes the relay.
- **Tests:** `tests/unit/common/test_backends.py`, `tests/unit/runner/test_backends.py`,
  `tests/unit/sandboxd/test_backend_surface.py`, `tests/unit/gatekeeper/test_models_admin_backend.py`,
  `tests/acp/` for an assistant provider, the hermetic suite, and live `ossctl doctor --probes` PASS.
- **No new GitHub write path**, approval path, host mount or route around the probe gate.

## Non-negotiable invariants

The full list, with what enforces each one, is in [AGENTS.md](AGENTS.md#invariants-that-must-never-be-broken)
(DESIGN §8.3 I1-I13). In short: only the gatekeeper holds the write token and only
`gatekeeper/submit.py` imports `gatekeeper/github_writer.py`; approvals are two human taps on Telegram,
bound to exact content and mode, and never LLM-mediated; dry-run defaults on; kill and pause are checked
everywhere; no container both holds a credential and runs repo code; LLM output is data, never control;
no exposure.

## What will not be accepted

- Anything that weakens an invariant I1-I13 (a change of that kind needs a new ADR and the maintainer's
  agreement first).
- A second GitHub write path, or a GitHub write outside `gatekeeper/submit.py` → `github_writer.py`.
- Exposure features: a Mini App, a tunnel (cloudflared, ngrok, Tailscale Funnel/serve), a webhook or an
  inbound port.
- LLM-mediated approvals, an approval API endpoint, or raising the size ceiling from Telegram.
- Backends that use unofficial or ToS-violating access to a model.
- Personal values or secrets in tracked files.
