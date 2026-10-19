# FEAT-007 build review, round 2: Kiro V3 provider fork, Hermes v0.22.2, capability tiers, assistant backend

Round 2 reviews `a6d6775..15299f1`, the 17 commits since the round 1 review, against the full FEAT-007 scope from base `d2827d8`. All seven round 1 findings are fixed. Item 37 (assistant backend) is committed with its tests, and Hermes got a `--network none` code-job tool by owner decision. Item 38 ran most of its live checks, but the coder's own findings leave FEAT-007 `in_progress` with three gaps open, and those gaps decide this round.

Watch for: the food-photo check failed live, because the image reached Kiro as an ACP image block and the model answered that it could not see it (confirmed, HIGH). No Hermes plugin tool call has ever reached the gatekeeper in a live turn, so `ossbot_request_sandbox_job`, the feature stores and the status, notify, pause and kill tools are unproven end to end (confirmed, HIGH). GHMCP-1..4 have still not started (confirmed, MEDIUM). Smaller items: the gatekeeper imports the root probe module, streamed deltas are not capped, and pytest-xdist is missing (rule 9).

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 2 HIGH, 1 MEDIUM, 3 NIT)

## High-level view

In the fork, round 2 changed only two things. The adapter defaults are now generic, with ossbot's values set in the unit. A `ruamel.yaml` fix lets the provider read `models.yaml` inside Hermes, which has no PyYAML.

The assistant backend sends attachments inline as `/v1/responses` parts, because Hermes v0.22.2 accepts no file uploads (a recorded decision change). Images are stripped of metadata without Pillow. PDFs are read only in a `--network none` `extract_text` job. The gatekeeper cannot reach sandboxd, so it queues PDF and code jobs and the runner executes them.

The live picture has two holes, and both sit on the provider's edge, where Hermes and Kiro meet. Text turns, the model switch, sourced answers and github.io/gist fetches work. An image arrives at Kiro but is not seen, and Hermes' own tool calls never come back out of the Kiro text bridge. The two features the owner asked for, the food-photo estimate and code on attachments, therefore do not work yet. Some matrix rows also read done although they depend on these paths.

The read-only GitHub MCP server is the remaining planned scope. The owner's "keep it simple" decision is recorded in the findings, but nothing is built.

<details>
<summary>Issues (6)</summary>

1. **F007-R2-1 Food-photo vision turn fails live** (HIGH; `vendor/hermes-kiro-provider/kiro_acp/v3.py`, `config/models.yaml`, `docs/VERIFICATION.md`, `docs/TRACEABILITY.md`) — the image block reaches Kiro (`images=1`), but the model says it cannot see it, so CR3-IH-4 and the item 38 acceptance criterion are unmet. Find out whether the block shape, the `test_vision` model (inherits `claude-haiku-4.5`) or Kiro is at fault, fix it or record a key or account cause under "User action needed", and pass the check. Until then set DES-V-30 back to a partial status ("which catalog models accept images" is unanswered).
2. **F007-R2-2 Hermes tool calls never reach the gatekeeper** (HIGH; `vendor/hermes-kiro-provider/kiro_acp/v3.py` `format_prompt`/`_create_chat_completion`, `hermes/plugins/ossbot/tools.py`, `docs/TRACEABILITY.md`) — in no live turn did a `function_call` arrive, and the model said `ossbot_request_sandbox_job` was unavailable. Every plugin tool, including stop requests and code jobs, is dead in production. Log metadata only (the number of tool definitions received, whether bridge sections were rendered, the number of `<tool_call>` blocks parsed), fix the bridge, and pass one live plugin call plus the live image code job. CR2-3f and CR2-7i should not read done until then.
3. **F007-R2-3 GHMCP-1..4 not started** (MEDIUM; `config/versions.lock.yaml`, `sandbox/images/llm/Dockerfile`, `.kiro/agents/ossbot-{scout,reviewer}.json`, `hermes/plugins/ossbot/`, `src/ossbot/ctl/doctor.py`, `docs/TRACEABILITY.md`) — carried from F007-R1-6. The owner's 2026-10-19 decision is recorded, but nothing is implemented. Build it as decided (pinned v2.0.2, `--read-only` read toolsets, `GITHUB_READ_TOKEN`, a fail-closed startup check, doctor `get_me`), with the fake-listing tests.
4. **F007-R2-4 Gatekeeper imports the root probe module** (NIT; `src/ossbot/gatekeeper/context.py`, `chat/backend.py`, `api/chat.py`) — `ossbot.ctl.hermes_probe` is the root ACP client. Move `read_result`/`relay_open` to `ossbot.common`, or add an import-linter contract limiting the gatekeeper to those two names.
5. **F007-R2-5 Streamed deltas are not capped** (NIT; `src/ossbot/gatekeeper/chat/backend.py` `ChatService.ask`, `chat/hermes_api.py`) — `deltas` grows without bound when no `response.completed` arrives, and `aiter_lines` buffers a whole line before the `MAX_EVENT` check. Stop at `MAX_ANSWER` and bound the line read.
6. **F007-R2-6 pytest-xdist missing** (NIT; `pyproject.toml`, `uv.lock`) — build-plan rule 9 asks for `-n auto`, and the hermetic bar took 8 min 11 s serially. Add the pinned dev dependency.

</details>

<details>
<summary>Details</summary>

### The vision turn reaches Kiro and is not seen (F007-R2-1, HIGH, confirmed)

VERIFICATION's FEAT-007 table records `ossctl chat selftest --fixture fried-chicken-leg.jpg` on `test_vision`. The provider log shows `images=1`, so `latest_images` decoded the `input_image` data URL and `_turn` attached an ACP `{"type":"image","mimeType","data"}` block, because `promptCapabilities.image` is advertised. The answer still said the image could not be analysed, and no estimate came back. The coder's findings list the check as open. Three causes are plausible, and the record does not tell them apart:

- the block shape or MIME handling Kiro V3 expects;
- the `test_vision` model, which resolves through `test` to `claude-haiku-4.5`, not accepting images through Kiro;
- Kiro dropping images for the `ossbot-chat` agent.

A headless `kiro-cli chat --v3` image prompt on `roles.vision` (`claude-sonnet-5.5`) would split them. The ACP block could also be compared with the ACP schema's `ImageContent`. The managed config disables Hermes' own `vision` toolset, so the provider path is the only image path. DES-V-30 is marked done, but this check is what would answer its question "which catalog models accept images (the `vision` role)".

### Hermes' tools do not survive the Kiro text bridge (F007-R2-2, HIGH, confirmed)

The V3 client renders Hermes' tool definitions into the prompt with `acp_openai_bridge.render_tool_bridge_sections(tools, tool_choice)` and parses `<tool_call>` blocks back out with `extract_tool_calls_from_text`. Live, VERIFICATION records that "no `function_call` reached the gatekeeper in this or any other live turn; the model said the tool was unavailable". The live system agrees: `~hermes/.hermes/ossbot-features/` does not exist, so `ossbot_feature_save` has never run. Doctor shows the surface `ok` with `ossbot` in the tier's toolsets, so the tools are registered. They are lost between Hermes and the model, or between the model's answer and Hermes. Several explanations fit, and the provider logs only model and image counts, so they can't be told apart today:

- `tools` arriving empty at `_create_chat_completion`;
- the preamble telling the model to prefer its own `web_search`/`web_fetch`;
- Kiro's agent answering in a shape the bridge does not parse.

The impact is wider than the code job. Every plugin tool (status, queue, notify, pause, kill, request_discovery, the feature stores) is unreachable in production, and the round 1 fix for F007-R1-2 has passed only the hermetic and root suites. CR2-3f and CR2-7i read done on hermetic evidence alone.

### GitHub MCP server, still not started (F007-R2-3, MEDIUM, confirmed)

GHMCP-1..4 are `missing` with "not started" evidence. The FEAT findings record the owner's 2026-10-19 decision: the pinned v2.0.2 binary, `stdio --read-only --toolsets repos,issues,pull_requests,actions`, the token from `GITHUB_READ_TOKEN`, wiring into the scout/reviewer agents and Hermes, one startup check that fails closed on any non-read tool, and doctor `get_me == Amsal1`, with no new probe framework. The plan addition also asks for the read token's sha256 in the output key scan. One thing needs checking at 2.32.0: Kiro expands `${VAR}` in agent MCP env only for names in `cli.json` `kiroAgent.mcpApprovedEnvVars`.

### Smaller items (F007-R2-4..6, NIT, confirmed)

`gatekeeper/context.py`, `chat/backend.py` and `api/chat.py` import `ossbot.ctl.hermes_probe` to read the probe file. The gatekeeper now depends on the root ACP client's module, and no import-linter contract stops it from using more of it. `ChatService.ask` appends every delta to a list and joins them when no `response.completed` arrives. `TurnStore.done` caps the stored text at 200 KiB, but memory is unbounded, and `HermesClient._events` sees a line only after `aiter_lines` has buffered it whole. The ADR addendum promises "per answer (512 KiB)". Rule 9 (f3ca1f8) asks for pytest-xdist, which is not in `pyproject.toml`.

### What was verified, and how

- Scope: read context.json, phase2-build-plan.md (hard rules 1-12, items 33-38, working rules 1-9, the GHMCP addition), FEAT-007.json (base `d2827d8`, round 2 progress, open items), DESIGN §8.3 I1-I13 and the §15.5/§15.6 changes in this range, the ADR-0002 round 1 addendum, and the TRACEABILITY rows for items 33-38 and GHMCP. Reviewed `git diff a6d6775..HEAD` (80 files). Read `api/chat.py`, `chat/{backend,hermes_api,turns,media,code_jobs}.py`, `context.py` (chat runtime), `runner/chat_jobs.py`, `run_code.sh`, the sandboxd profile, dockerfilter, schema and secret-scan diffs, `kiro_acp/v3.py`, the plugin `tools.py`/`features.py` and the `ossbot-hermes.service` diff.
- Round 1 fixes: F007-R1-1 is committed (d29ae13, a64a8a1). The corpus was re-recorded, and the images were rebuilt with new digests in `versions.lock.yaml`. F007-R1-2 is fixed by 024c7c6 (owner decision recorded in DESIGN §15.5 and the ADR, CR2-3f-research deferred). F007-R1-3 is recorded in the ADR addendum. F007-R1-4 is covered by e3233db/efac22e. F007-R1-5 is fixed by b6bdd97 (`git var GIT_AUTHOR_IDENT`). F007-R1-7 is fixed by e8a6b7c.
- Safety by reading: code jobs run in profile `code` (sandbox-full, `--network none`, not a key pair, `env -i`, read-only inputs, `timeout -k`, the cid `sys-chat` bound to `extract`/`code` only). The gatekeeper re-caps the artifacts with `O_NOFOLLOW` and drops result files with a secret hit. Submission requires requester role and tier `self_improve`, and is refused or cancelled while killed, paused or down. This is consistent with I4, I5, I8 and I9. Selftest answers go only through `c.notify` (the outbox to the owner). Hermes SSE is parsed field by field and bounded per event. Attachments are capped before and after `getFile`, and kept 0700/0600 under the gatekeeper's state (live: `gatekeeper:gatekeeper 700/600`).
- Lint and tests (`.ossbot-local/review-f007-r2.sh`, DEV venv on the clean worktree at 15299f1): ruff check clean, ruff format 328 files formatted, mypy no issues in 164 files, lint-imports 5 contracts kept. Targeted pytest over `tests/acp`, `tests/unit/gatekeeper/chat`, `test_hermes_plugin.py`, sandboxd `test_run_code`/`test_extract_text`, `tests/unit/ctl`, `test_systemd_units.py`, `test_hermes_surface.py` and `tests/unit/dockerfilter`: 621 passed in 52 s. The full hermetic bar was not re-run. Per rule 9, the coder's recorded run at a64a8a1 (2811 passed, 19 deselected) covers HEAD, because `a64a8a1..HEAD` changes only docs. The root suites (19 passed) and the system suites are left to the gate.
- Live, read-only (`.ossbot-local/review-f007-r2-root.sh`, root): the deployed `/opt/ossbot/app/{src,vendor,hermes/plugins/ossbot}` equals the worktree. Seven units are active: ossbot-hermes, the probe socket, the tier and skills paths, the gatekeeper, the runner and sandboxd. As hermes: `kiro-cli 2.32.0` and `Hermes Agent v0.22.2 … d923a5eb487a`. The tier flag is `self_improve`. The managed scope lists `api_server: [memory, todo, session_search, ossbot, web, skills]`, with terminal/file/code_execution/delegation/cronjob/browser in `disabled_toolsets`. The probe passed clean at 10:50Z. Doctor reports PASS 44, with "chat relay: enabled" and "dry_run True". `ossctl status` shows `dry_run: true` and lifecycle up. No feature store directory exists. Hermes reads `models.yaml` from `/mnt/d/...`, unchanged from round 1.
- Live records: the VERIFICATION FEAT-007 table matches what is observable (versions, tier, probe, doctor, relay). The model-switch claim (`config_sets=0` because `_meta.kiro.modelId` is honoured) is covered hermetically by `test_model_switch.py`. FINAL_REPORT "User action needed" lists none, which fits: neither open live check failed for a key reason. This review sent no Telegram message.
- Secrets: `gitleaks git --redact --log-opts=--all` scanned 142 commits with no leaks. No secret file was read. `secret_scan` now also covers `REVERSE_IMAGE_SEARCH_KEY`.
- No GitHub write: nothing in the range touches `submit.py`/`github_writer.py`. The bundle script writes only under `.ossbot-local/`. Dry-run is ON.
- Git: all 23 commits since the base have Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `git status --porcelain` is empty. `origin/main` (a64a8a1) was moved by owner pushes (reflog "update by push", working rule 5), and `main..origin/main` is empty, so no remote commit is missing locally and no history was rewritten.

</details>

<details>
<summary>File map</summary>

- `src/ossbot/gatekeeper/chat/{backend,hermes_api,turns,media,imgmeta,pdf_jobs,code_jobs}.py`, `api/chat.py`, `context.py`, `app.py`: assistant backend, relay gate, attachments, PDF/code job queues, selftest
- `src/ossbot/runner/{chat_jobs,gk_client,main}.py`: runner side of the PDF and code jobs
- `src/ossbot/sandboxd/{profiles,jobs}.py`, `dockerfilter/rules.py`, `common/schemas.py`, `tests/unit/dockerfilter/corpus/recorded.json`: profiles `extract` and `code`
- `sandbox/entrypoints/{extract_text,run_code}.sh`, `sandbox/images/full-base/Dockerfile`, `sandbox/images/full/{Dockerfile,code-requirements.*}`, `scripts/lib/images.sh`, `.dockerignore`, `config/versions.lock.yaml`: poppler, hash-pinned code venv, split toolchain base, new digests
- `hermes/plugins/ossbot/{tools,features}.py`, `hermes/skills/reverse-image-search/SKILL.md`: code-job tool, feature stores, skill stub
- `vendor/hermes-kiro-provider/kiro_acp/{v3,v3_core}.py`, `systemd/ossbot-hermes.service`: generic defaults, request roles, ruamel models file
- `scripts/setup-secrets.{sh,ps1}`, `gatekeeper/secret_scan.py`, `config/policy.yaml`, `common/config.py`: reverse-image key hook, `chat.attachments`
- `scripts/dev/kiro-provider-bundle.sh`, `docs/upstream/hermes-kiro-provider-v3.md`: author from git, configuration note
- `ctl/{cli,hermes_probe}.py`: `chat selftest`, `kiro smoke --fetch`, probe env
- `tests/**`: chat unit tests, acp model-switch additions, plugin, sandboxd and root sandbox tests, fixtures
- `docs/{DESIGN,TRACEABILITY,VERIFICATION,FINAL_REPORT}.md`, `docs/adr/0002…`, `docs/plan/*`: decisions, rows, live checks, rules 8 and 9

Full diff: `git -C d:\Work\autonomous-oss-contributor diff a6d6775..HEAD` (this round), `d2827d8..HEAD` (the whole FEAT).

</details>
