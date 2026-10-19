# FEAT-007 build review, round 1: Kiro V3 provider fork, Hermes v0.22.2, capability tiers, assistant backend

Round 1 reviews `d2827d8..a6d6775` (base commit from the FEAT findings) and the uncommitted work in the tree. Items 33 to 36 are committed: the ADR-0002 research addendum, the unmodified upstream import, the V3 adapter with V2 still selectable, the Hermes install with its probe and surface check, and the tiers with the root-run skill gate. They match DESIGN revision 5 as amended in the same commits, and their suites, the lint gate and both Hermes system tests pass on a clean export of HEAD. Item 37 (assistant backend) exists only as uncommitted work in 31 paths. It fails the hermetic bar and the lint gate, and item 38 (live checks, close-out) has not started.

Watch for: the dirty tree and the unfinished items 37 and 38 (confirmed, HIGH; working rule 4 forbids approval). The `request_sandbox_job` plugin tool that DESIGN §15.5/§15.6 relies on is missing (confirmed). The fork is not declared as a uv path dependency, and no decision records why (confirmed). TRACEABILITY and VERIFICATION have no FEAT-007 entries, though items 33 to 36 close rows and ran live checks (confirmed). The owner's name and email are hard-coded in a tracked script (confirmed). The GHMCP rows assigned to FEAT-007 have not started (confirmed).

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 1 HIGH, 5 MEDIUM, 1 NIT)

## High-level view

The fork lands in two steps. `b595c28` holds the upstream tree byte for byte, plus `UPSTREAM_*`. After it, the only upstream file touched is a two-branch dispatch in `__init__.py`, which keeps V2 behind `HERMES_KIRO_ACP_ENGINE=v2`. Item 34 is missing the uv path dependency, and no decision explains the gap.

The Hermes install and probe keep root away from hermes-writable bytes. Root reads `.git/HEAD` as data, `uv sync` runs as hermes, and the probe loads `v3_core.py` by path from the root copy under `python -I`. The plugin has no `request_sandbox_job`, which DESIGN names as Hermes' only route to running code.

The tiers render into Hermes' root-owned managed scope `/etc/hermes/config.yaml` instead of `~hermes/.hermes/config.yaml`. That is stronger than the original design, because nothing hermes-writable carries the tool surface, and it is recorded in DESIGN §15.5 and the ADR addenda. Skills stage as v0.22.2's `pending/skills/` replay records, which root parses as data. Neither change, nor any row these items close, reached TRACEABILITY or VERIFICATION.

The assistant backend (item 37) is written but uncommitted and red, and item 38 has not run.

<details>
<summary>Issues (7)</summary>

1. **F007-R1-1 Unfinished items 37/38 in a dirty tree** (HIGH; files: the 31 WIP paths, `tests/unit/sandbox/test_gitcmd_image_env.py`) — 31 uncommitted paths fail `test_every_entrypoint_exists_and_matches_the_shared_constant`, ruff (8), format (2) and mypy (11), and most plan tests are absent. Finish item 37 with its tests, the dockerfilter corpus re-record and the sandbox-full rebuild, then commit and run item 38.
2. **F007-R1-2 `request_sandbox_job` missing from the Hermes plugin** (MEDIUM; `hermes/plugins/ossbot/tools.py`, a gatekeeper route) — item 35 and DESIGN §15.5/§15.6 route all code execution through it. Add the tool and its gatekeeper route, with role and tier checks and tests.
3. **F007-R1-3 Fork not a uv path dependency, undocumented** (MEDIUM; `pyproject.toml`, `uv.lock`, `docs/adr/0002-agent-runtime-hermes-kiro-acp.md`) — `pyproject.toml`/`uv.lock` were not changed, and nothing records the deviation. Add it, or record in the ADR-0002 addendum why loading by path from the root copy replaces it.
4. **F007-R1-4 TRACEABILITY and VERIFICATION untouched by FEAT-007** (MEDIUM; `docs/TRACEABILITY.md`, `docs/VERIFICATION.md`) — items 33 to 36 close or partly close rows and ran live checks, but the matrix and `## Phase 2 live checks` have nothing. Set the rows done with evidence and record the live results.
5. **F007-R1-5 Owner identity hard-coded in `scripts/dev/kiro-provider-bundle.sh`** (MEDIUM) — hard rule 8 forbids it. Read the name and email from `git var GIT_AUTHOR_IDENT` or the gitignored profile, and fail if unset.
6. **F007-R1-6 GHMCP-1..4 not started** (MEDIUM; `docs/TRACEABILITY.md` GHMCP rows, `config/versions.lock.yaml`, `sandbox/images/llm/Dockerfile`, `hermes/plugins/ossbot/`) — TRACEABILITY assigns the read-only GitHub MCP server to FEAT-007. Implement it per the plan addition, or move the rows with a recorded decision.
7. **F007-R1-7 Upstream bundle carries ossbot defaults** (NIT; `vendor/hermes-kiro-provider/kiro_acp/v3_core.py`, `scripts/dev/kiro-provider-bundle.sh`) — `v3_core.py` defaults to `/home/hermes` paths, proxy `10.212.0.2:3141` and mode `ossbot-chat`, and cites ossbot DESIGN. Make the defaults generic or env-driven before the contribution is imported.

</details>

<details>
<summary>Details</summary>

### Item 37 sits uncommitted and red (F007-R1-1, HIGH, confirmed)

`git status --porcelain` lists 20 modified and 11 untracked paths. The newest was written at 14:03:51, and nothing changed during the 35 minutes of this review, so the coder step stopped mid-item. Run on the tree, `DEV pytest -m "unit or telegram or acp or e2e"` gives 2626 passed, 1 failed. `sandbox/entrypoints/extract_text.sh` exists, but `SANDBOX_JOB_ENTRYPOINTS` has no `extract_text`. Also on the tree, `ruff check` reports 8 errors (E501 in `tests/unit/gatekeeper/chat/images.py` among them), `ruff format --check` 2 files (`media.py` among them), and `mypy` 11 errors in `common/config.py`, `chat/hermes_api.py`, `chat/backend.py`, `runner/chat_jobs.py` and `api/chat.py`. Of the plan's item 37 tests, only `test_imgmeta.py` and `test_imgmeta_fuzz.py` exist. Still missing: `test_backend`, `test_relay_gate`, `test_media` and `test_turns`, the unit and root `test_extract_text`, and the `fake_hermes` extension. The WIP changes `dockerfilter/rules.py` (new `extract` profile in `NETWORK_NONE_PROFILES`) and `sandbox/images/full/Dockerfile` (poppler). That calls for the `record_corpus` re-record and an images rebuild with the new digest in `config/versions.lock.yaml`. Doctor already WARNs "versions lock: installed copy differs from the repo" because of the uncommitted lock edit. Item 38 has not started: there is no selftest run, no live model switch, no sourced answer, no github.io/gist fetch, no food-photo estimate and no `docs/FINAL_REPORT.md`.

### Hermes has no way to ask for a sandbox job (F007-R1-2, MEDIUM, confirmed)

DESIGN §15.5 says Hermes "asks through the `ossbot` plugin for a `research` or `agent-llm` sandbox job, which the gatekeeper schedules (never in-process)", and §15.6 repeats it for code execution. Plan item 35 names `request_sandbox_job` in the plugin list. `hermes/plugins/ossbot/tools.py` at HEAD and in the WIP has `ossbot_status`, `_queue`, `_contribution`, `_logs`, `_request_discovery`, `_notify`, `_pause`, `_kill` and the new `ossbot_feature_*` pair. A repo-wide search finds no tool, route or test for sandbox job requests. Because `terminal` and `code_execution` are disabled in every tier, Hermes has no code-running path at all, and the image-tool and OCR features of §14.19 item 12 depend on one.

### The vendored fork is loaded, not installed (F007-R1-3, MEDIUM, confirmed)

Item 34 says "non-editable uv path dependency (`uv lock`)", and the FEAT step adds "sync DEV and ROOTDEV venvs". The FEAT-007 diff changes `pyproject.toml` only for ruff and mypy configuration and leaves `uv.lock` untouched. The tests put `vendor/hermes-kiro-provider` on `sys.path` in `tests/acp/conftest.py`, and `ctl/hermes_probe.py` loads `v3_core.py` by path from the root copy. A defensible reason exists: an installed copy in the app venv would be a second import origin for the probe. But the plan text was dropped without a note in the ADR addendum or the FEAT findings.

### Matrix and live-check log were skipped (F007-R1-4, MEDIUM, confirmed)

`git log d2827d8..HEAD -- docs/TRACEABILITY.md docs/VERIFICATION.md` is empty. Items 33 to 36 name DES-V-5c/10/11/12/13/28/29/30, CR2-3c, DES-FR-11, DES-AC-20, ADR-0002, CHAT-5/6, DES-FR-1b, DES-FR-19, DES-AC-25, ADR-0018, CR2-3a/b/d/e/f/g, CR2-6a and CHAT-25. Context step 1 requires closed rows set to done with evidence in the same commit, and working rule 2 says to update as you go. The live work so far is recorded only in the ADR-0002 item 35 addendum: the real V3 handshake, the probe pass with auth option (a), two redeploys, the hygiene and tier system tests, and the surface check. VERIFICATION `## Phase 2 live checks` still says "SKIP 3 (the Hermes checks belong to FEAT-007)". The FEAT-005 tier rows also still carry their "status stays missing until item 36" note. The managed-scope decision changed DESIGN and the ADRs but not the matrix rows that cite `~hermes/.hermes/config.yaml`.

### Personal identity in a tracked script (F007-R1-5, MEDIUM, confirmed)

`scripts/dev/kiro-provider-bundle.sh` (da6935a) sets `name="Amsal Khan"` and `email="md.amsalkhan@gmail.com"` for the upstream commit and its `Signed-off-by`. Hard rule 8 and the contribution requirements allow owner values in tracked files only under `docs/` and in the gitignored `config/profile.yaml`. The script runs as any user from the repo root, so `git var GIT_AUTHOR_IDENT`, or the profile's git identity if it has one, can supply the values. It should refuse to build a bundle without them.

### GitHub MCP rows (F007-R1-6, MEDIUM, confirmed)

TRACEABILITY GHMCP-1 to GHMCP-4 have phase `P2 FEAT-007` and status `missing`, with "not started" evidence. They cover the pinned v2.0.2 binary in sandbox-llm and the Hermes path, `--read-only` with the read toolsets, the token routing and key-scan entry, fail-closed on any write tool, and the Hermes plugin tool with a doctor `get_me` check. The FEAT findings list "GHMCP rows" as next, but no item of the FEAT file carries them. They need either an implementation in this loop or a recorded move to another FEAT.

### Upstream bundle defaults (F007-R1-7, NIT, likely)

`kiro-provider-bundle.sh` copies the vendor tree as is. `v3_core.py` is the core of the proposed upstream change, and it hard-codes `DEFAULT_KIRO_CLI=/home/hermes/.local/bin/kiro-cli`, `DEFAULT_PROXY=http://10.212.0.2:3141` and `EXPECTED_MODE="ossbot-chat"`, and cites "ossbot DESIGN §20". Upstream users would get ossbot's network and mode by default. Both gates still apply, so this does not block, but scout/self-review on the contribution will likely flag it. Env-driven defaults with ossbot's values set in `ossbot-hermes.service` would keep the probe deterministic.

### What was verified, and how

- Scope: read context.json, phase2-build-plan.md (hard rules, items 33 to 38, working rules 1 to 7), FEAT-007.json, the DESIGN §15.5 and §15.6 text, the ADR-0002/0018 addenda, and the TRACEABILITY GHMCP rows. Reviewed `git diff d2827d8..a6d6775` (89 files) and `git diff` of the tree.
- Vendor import: `b595c28` touches only `vendor/hermes-kiro-provider/` (12 files). `git diff --no-index` of the codeload archive against the vendor dir shows only the later `kiro_acp/` files, the README note, the 19-line dispatch in `__init__.py` and `UPSTREAM_*`. LICENSE is kept.
- V3 core (read): `assert_surface` treats a missing mode as a mismatch. `permission_response` picks `reject_once` > `reject_always` > `cancelled`. `_log_frame` and `_on_frame` redact the `getAccessToken` result and params. `METHOD_NOT_FOUND` is sent for unknown requests. The plan's acp tests all exist.
- HEAD gate on a `git archive HEAD` export in `/tmp`, run with the DEV venv binaries and no sync (`.ossbot-local/review-f007-head.sh`): ruff check clean, ruff format 303 files formatted, mypy no issues in 155 files, lint-imports 5 contracts kept, pytest `-m "unit or telegram or acp or e2e"` 2614 passed, 13 deselected.
- Tree gate (DEV prefixes): pytest 2626 passed, 1 failed. Ruff 8 errors, format 2 files, mypy 11 errors, lint-imports 5 kept. All failures are in WIP files.
- Live, read-only (`.ossbot-local/review-f007-live.sh`, root): `ossbot-hermes`, the probe socket, both tier units, both skill units and the gatekeeper are active. As hermes: `kiro-cli 2.32.0` and `Hermes Agent v0.22.2 … d923a5eb487a`. The tier flag is `self_improve`. `/etc/hermes/config.yaml` is root:root 0644, with `api_server: [memory, todo, session_search, ossbot, web, skills]` and the six always-disabled toolsets in `agent.disabled_toolsets`. Skills, hooks and `.env` are root:hermes 0750/0640/0750. The only hermes listener is 127.0.0.1:8642. The probe verdict is pass/clean. Doctor reports PASS 43 and WARN 1 (the versions lock, from WIP); "chat relay: enabled"; "gatekeeper health: dry_run True". `ossctl status` shows `dry_run: true`. Deployed `/opt/ossbot/app` lacks the WIP files, so the services run committed code.
- System suites (`.ossbot-local/review-f007-system.sh`, root): `test_hermes_install_hygiene.sh` exit 0 (19 PASS, mid-upgrade file and socket cases included), `test_hermes_tier.sh` exit 0 (23 PASS). `test_egress_surface.sh` was not re-run.
- Not done: no redeploy, because a redeploy rsyncs the dirty tree and would put the failing WIP live. The "second run only unchanged" claim was not re-checked this round.
- Secrets: `gitleaks git --redact --log-opts=--all` scanned 126 commits and found no leaks. A `gitleaks dir` pass over the tree timed out at 600 s and gave no result. No secret file was read. The permission check used `test -r` only.
- Safety: no GitHub write path changed in the committed range. The bundle script clones upstream read-only and writes only under `.ossbot-local/`. Dry-run is ON. No Telegram message was sent by this review. I1-I13 hold in the reviewed committed code: root never executes hermes-writable bytes, root parses Hermes JSON and HTTP answers as data, argv lists are used, and the units carry the full hardening (checked by `test_systemd_units.py` on HEAD and by reading `ossbot-hermes.service`).
- Git: all 8 commits have Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `origin/main` moved to `a6d6775` by an owner push (working rule 5) and has no commit that local main lacks. The tree is dirty with the coder's WIP (issue 1).

</details>

<details>
<summary>File map</summary>

- `vendor/hermes-kiro-provider/**`: unmodified upstream import (b595c28); `kiro_acp/{v3_core,v3,hermes_plugin}.py`, V3/V2 dispatch in `__init__.py`; WIP edit to `v3.py` (request roles)
- `tests/fakes/fake_acp_v3.py`, `tests/acp/*`: V3 contract, mode mismatch, probe eval, model switch, image blocks, permissions, token redaction, V2 selection
- `scripts/lib/hermes.sh`, `scripts/lib/{kiro,services,users}.sh`, `scripts/provision.sh`: kiro-cli for hermes and Hermes install steps
- `hermes/config/*`, `hermes/plugins/ossbot/*`: managed-scope template, permissions, plugin tools (WIP: feature store)
- `systemd/ossbot-hermes*.{service,socket}`, `ossbot-hermes-tier.{path,service,timer}`, `ossbot-skills*.{path,service,timer}`: new units
- `src/ossbot/ctl/{hermes_probe,hermes_render,hermes_tier,skills_root}.py`, `ctl/{cli,doctor}.py`: root probe, render, tier apply, skill hasher/activation, ossctl and doctor
- `src/ossbot/gatekeeper/{skill_reviews.py,chat/hermes_surface.py,migrations/0004_hermes.sql,telegram/handlers.py}`: skill reviews and the minimal skill card
- `docs/adr/0002…`, `docs/adr/0018…`, `docs/DESIGN.md`, `config/versions.lock.yaml`: research and install addenda, managed-scope decision, peeled commit
- `scripts/dev/kiro-provider-bundle.sh`, `docs/upstream/hermes-kiro-provider-v3.md`: upstream bundle prep
- WIP (uncommitted): `src/ossbot/gatekeeper/chat/{backend,hermes_api,turns,media,imgmeta,pdf_jobs}.py`, `api/chat.py`, `runner/chat_jobs.py`, `sandboxd/profiles.py`, `dockerfilter/rules.py`, `sandbox/entrypoints/extract_text.sh`, `sandbox/images/full/Dockerfile`, `scripts/setup-secrets.*`, `config/policy.yaml`, `tests/unit/gatekeeper/chat/*`

Full diff: `git -C d:\Work\autonomous-oss-contributor diff d2827d8..HEAD` plus `git diff` for the WIP.

</details>
