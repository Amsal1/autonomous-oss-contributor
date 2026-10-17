# Design review: docs/DESIGN.md (Draft v2, revision 3)

Reviewer: design-review step (fresh context, round 4) · Date: 2026-10-17
Scope: DESIGN.md Parts I–III, with close reading of every section the revision touched (§5 AC 26–30, §6.1, §7.3, §7.5, §9.1–9.13, §10.1–10.9, §12.2–12.7, §13, §15.1–15.4, §16.3, §19, §20, §22, §23, §27.0), ADR-0001..0018 (all revision-3 amendments read), and the round-3 review and verdict. Checked against the ten verbatim user messages.

Live checks run for this round:
- A local git 2.55 experiment (temp dir under the gitignored `.ossbot-local/`, removed afterwards). It built a bundle with the revision-3 `package.sh` shape (`checkout -B ossbot-change`, `bundle create refs/heads/ossbot-change ^<base>`), then fetched it into a bare repo with and without `-c protocol.file.allow=never`, and ran `bundle verify` under the same flag.
- Nothing pushed or fetched from any remote. No builds or tests run, because there is no code yet.

Verdict: **CHANGES_REQUESTED** (0 HIGH, 2 MEDIUM, 6 NIT)

## Overall assessment

Revision 3 resolves all 19 round-3 findings (M1–M11, N1–N8). The fixes are consistent across DESIGN, §19/§20/§22/§23 and the ADR amendments, and the four stated deviations (M2 snapshot root, M6 repo-fact skips, M9 root probe, M10 no `setup-hermes.sh`) are each justified and safer than or equal to what was asked.

The trust boundary still holds in the design text:
- **Approval** is deterministic and Telegram-only, with two taps and the mode recomputed at Approve, at Confirm and before the first write.
- **Submission** now has `control_checkpoint()` before every write, and restart reconciliation uses reads only. Nothing resumes a write without a fresh approval.
- **The M4 redirect** lives in the gatekeeper's state machine. It can only move a contribution towards `WATCHING`, never towards a gate state, and worker actors gain no new edge.
- **Operator import** uploads bytes, so there is no path field and no confused deputy. The imported change goes through the same package re-authoring, derived preview and approval as every other change.

Two problems remain, both from the revision-3 fixes:
- **R4-M1 (correctness, verified).** Every bundle fetch in the design runs with `protocol.file.allow=never`, and git refuses a bundle fetch under that setting. So every `/package`, every operator-import validation and every unbundle fails closed, and no contribution can complete.
- **R4-M2 (security, ambiguity).** The new root `ossbot-hermes-probe` reuses "the vendored adapter's own `launch_spec()`" so that it cannot drift from production. The production copy is the one under `~hermes`, which uid `hermes` can write. If the probe imports that copy, there is a `hermes` → root escalation, which would undo the gatekeeper-token and kill-switch boundaries. The design does not say where the root process loads that code from.

Both are small, concrete fixes. Everything else I found is NIT.

---

## Resolution of round-3 findings

| ID | Status | Evidence in revision 3 |
|---|---|---|
| M1 | Resolved, but see R4-M1 | §9.11 step 3 uses `checkout -q -B ossbot-change` and `bundle create … refs/heads/ossbot-change "^$ROUND_BASE"`. §12.5 step 3 requires exactly one `list-heads` line for `refs/heads/ossbot-change` (422 `bundle_refs`) and fetches it to `refs/ossbot/r<rev>`. §9.8 unbundle fetches the same ref to `refs/ossbot/prev/r<rev>`. Tests `test_real_package_script_bundle` and `test_bundle_refs_rejected`. The ref shape is correct; my git 2.55 run confirms the bundle lists exactly that head. The transport flag around the fetch is wrong (R4-M1). |
| M2 | Resolved | §10.1: `install -d -o 10001 -g 10001 -m 0700` for `/work /work/repo /io/output /run/exec /snap /home/agent`; `/io/input` and `/in` stay root 0755. §10.2 template: `exec` on `/tmp` only for fetch, validate, agent-exec and package. §10.8 Tmpfs rule enforces that profile set. §23 items 5 and 14 carry the [VERIFY]s, and S-perm-1 is in 22.5. Snapshot now writes the volume root, which also fixes the `/work/repo/repo` nesting. |
| M3 | Resolved | §9.13, §12.2: `bundle_b64` (≤ 50 MB decoded, 70 MB request cap on that route only), no path field, a test asserting that. Validation happens before any row exists: single head, prerequisites == `{base_sha}`, base ancestry, head descends from base. Storage is gatekeeper-only, served by `GET …/import-bundle` (worker, state-limited, sha256-checked by the runner). Path `IMPORTED→FETCHING (W)` → fetch at `base_sha` (§9.6 handles shallow history) → unbundle `mode=import` → `FETCHED→IMPLEMENTED` (W, guarded) → `VALIDATING`. `IMPORTED` is in `WORKING`, so it gets generated `FAILED` edges and the kill check. |
| M4 | Resolved | §9.2: `resolve_target()` redirects `FAILED`/`ABANDONED` to `WATCHING` whenever `pr_number` is set, keeps the task volume, cancels the fix's actions and invalidates approvals. `WATCHING→ABANDONED` is H only (`stop_watch` plus confirm). `branch_moved` sets `watch_rounds = watch_rounds_max`. AC 28 and `test_fix_round_failure_returns_to_watching`, `test_branch_moved_disables_fixes`, `test_stop_watching_human_only`, `test_e2e_fix_round_failure.py`. The redirect runs before the actor check and never widens worker reach into gate states. |
| M5 | Resolved | §12.5: step 0 commits `consumed`/`executing` first. `control_checkpoint()` runs before fork, push, PR and every retry, and an interruption ends `failed interrupted` → `SUBMIT_FAILED` → Retry with a fresh card. Restart reconciliation table per kind, reads only, runs while killed. `reconcile_pending` on read errors keeps the slot consumed. §12.3 action marker; §13.1/13.2; ADR-0008; AC 29; tests listed. |
| M6 | Resolved | §12.7 partial index `contributions_issue_active` over non-terminal states. §9.4 deterministic rules enforced in the gatekeeper's insert path (409 `duplicate_active` or `not_rediscoverable:<rule>`). New `limits.rediscover_cooldown_days: 7`. The repo-fact-skip deviation is justified (otherwise the seeded first target could never be retried). AC 30. |
| M7 | Resolved | §9.11: `OSSBOT_FORMAT_PATCH` with `--no-signature --no-stat --full-index`, pinned diff config, `--no-ext-diff --no-textconv --no-color`, run by both sides with no system or global config. §12.5 step 6 compares `patch-id --stable` plus the numstat set; byte-only differences are logged at INFO. `patch_sha256` is over the gatekeeper's own output. Tests listed. (One determinism NIT on numstat: R4-N6.) |
| M8 | Resolved | §15.1 renders `tools.tool_search.enabled: off`, with the YAML-1.1 parsing [VERIFY] in §23 item 13. The surface check forbids `tool_search`, `tool_describe` and `tool_call` explicitly, and there is a fake test. Fail-closed either way. |
| M9 | Resolved for the check itself; see R4-M2 | §15.2 `assert_surface`: `currentModeId == ossbot-chat` after `session/new` and `session/load`; tools ⊆ web tools where advertised; on violation, refuse, close and soft-kill. The root probe acts as the ACP client and evaluates the stream deterministically. The result file is root:gatekeeper 0640, keyed by version and agent/permissions/settings hashes, and gates the relay (pass younger than 2 h). §23 item 11 records that chat stays disabled if custom modes cannot be selected. The gate is sound. Where the root process loads the adapter code from is unspecified (R4-M2). |
| M10 | Resolved | §15.4: `git clone --depth 1 --branch`, tag object and peeled commit verified against `versions.lock.yaml`, `uv sync --frozen --no-dev` as `hermes`. No website installer and no `setup-hermes.sh`; the same path for upgrades; doctor drift check. (Install-script hygiene NIT: R4-N4.) |
| M11 | Resolved | §10.2/10.3/10.8/10.9/16.3: `catalog` profile (`list_models` only, `repo_mode none`, no inputs, `kiro_core` egress, fixed network `ossbot-t-sys-catalog-catalog`), exempt from the probe gate. Probes get `resolve()`d models after a catalog refresh. Fixed order list_models → upsert → probes → jobs; `catalog_empty` alert with an hourly retry. Tests `test_list_models_exempt` and `test_e2e_first_start.py`. |
| N1 | Resolved | §7.5 has a single header. |
| N2 | Resolved | Duplicates removed (§13.2, §15.1). |
| N3 | Resolved | The summarizer is `repo_mode none` in §9.1, §10.2 and §15.3. The helper is `snapshot` everywhere. |
| N4 | Resolved | §12.2/§13.1/§13.2: offline `ossctl resume` path, `resume.log`, and `resume_log_offset` FYI on the next start. |
| N5 | Resolved | §10.8: `start` is refused while killed or paused; the job is recorded `interrupted`. |
| N6 | Resolved | `sandbox_full_digest` is in `probe_key`, the PK, AC 26 and the gate test. |
| N7 | Resolved | `/v1/toolsets` uses `extra="ignore"` with required `name`, `enabled`, `tools`, and there is a pass test. |
| N8 | Resolved | §15.3, §10.9 `coder_surface` exact-id assertion, §23 item 5. |

---

## New findings

### HIGH

None.

### MEDIUM

**R4-M1. Every bundle fetch runs under `protocol.file.allow=never`, which git refuses for bundles. Every `/package`, every import validation and every unbundle fails, so no contribution can complete.**
Where:
- §12.5 step 3: "`git bundle verify`, with `protocol.file.allow=always` scoped to this one command … Then run `git fetch <bundle> refs/heads/ossbot-change:refs/ossbot/r<rev>`". `GIT_ARGS` carries `-c protocol.file.allow=never`.
- §9.13 step 2: the import bundle "is fetched with `transfer.fsckObjects=true` into a temporary per-import mirror", using the same gatekeeper git args.
- §9.8: the unbundle job fetches "with the hardened git flags from 9.11", and §9.11's `G` includes `-c protocol.file.allow=never`. This covers fix-round and `mode=import` unbundles.

Verified locally with git 2.55.0:
- `git -c protocol.file.allow=never fetch ../c.bundle refs/heads/ossbot-change:refs/ossbot/r1` → `fatal: transport 'file' not allowed`, exit 128. Git's transport layer applies the `file` allow-check to bundle paths.
- `git -c protocol.file.allow=never bundle verify ../c.bundle` → `is okay`, exit 0. `bundle verify` does not use the transport, so the design scopes the override to the one command that does not need it.

Each failure is closed (the stage or `/package` errors), but the first `/package`, the first import and the first fix round can never succeed.

Fix: move the scoped override onto the bundle fetch, everywhere a bundle is fetched, and keep `never` as the default for everything else:
```python
# gatekeeper (12.5 step 3, 9.13 step 2)
BUNDLE_FETCH_ARGS = GIT_ARGS + ["-c", "protocol.file.allow=always"]   # last -c wins; this argv only
run(["git", *BUNDLE_FETCH_ARGS, "fetch", "--no-tags", str(bundle_path),   # bundle_path: gatekeeper-chosen absolute path
     "refs/heads/ossbot-change:refs/ossbot/r%d" % rev], env=GIT_ENV, cwd=mirror)
# `git bundle list-heads` / `git bundle verify` keep plain GIT_ARGS (they need no transport)
```
```bash
# sandbox unbundle.sh (9.8 fix_round, 9.13 import)
$G -c protocol.file.allow=always fetch -q --no-tags /io/input/<bundle> <ref>:<dst>
```
Also:
- Reword §12.5 step 3 so the override is on the fetch, not on `verify`.
- Add to §23 item 19: "bundle fetch requires `protocol.file.allow` ≠ never (verified on 2.55); `bundle verify`/`list-heads` do not".
- Make `test_real_package_script_bundle` and `test_package_verify.py` run the verifier with the production `GIT_ARGS`, not a relaxed test config, so this class of error is caught. Add `test_unbundle_script_real_bundle`, which runs the real `unbundle.sh` in both modes.

**R4-M2. The root `ossbot-hermes-probe` reuses "the vendored adapter's own `launch_spec()`/`session_new_params()`", but the design never says where root loads that code from. The production copy is `~hermes/.hermes/plugins/model-providers/kiro-acp`, which uid `hermes` can write.**
Where: §15.2 Hermes-side probe bullet 1 ("so the probe and production cannot drift"); §15.2 vendoring paragraph ("Provisioning copies it to `~hermes/.hermes/plugins/model-providers/kiro-acp`"); §7.5 unit table; §18.1 (`ctl/hermes_probe.py`).
uid `hermes` is in the untrusted-reasoning tier. It holds a Kiro key, has `web_public` egress, and code-execution bugs in Hermes, its plugins or kiro-cli are an accepted residual risk (§15.1). The design accepts that residual risk only for the Hermes key.

The no-drift wording points an implementer at the very copy production runs. If root imports that copy, or puts it on its `sys.path`, `hermes`-uid code runs as root. Root can then:
- read `gatekeeper.env` (the write token);
- clear the kill flags;
- forge the probe result.

That breaks the core invariant: no path from Hermes to a public write, approval, resume, or limit change. The same applies if root runs any hermes-owned executable as root.

Fix (add to §15.2 and to ADR-0002's amendment):
- **Root-owned adapter.** `ossbot.ctl.hermes_probe` imports `kiro_acp` only from the root-owned install `/opt/ossbot/vendor/hermes-kiro-provider/` (root:root 0755, files 0644), copied by provisioning from the repo's `vendor/`. It never imports from, or adds to `sys.path`, any path under `/home/hermes`.
- **Drift check by hash, not by import.** Root computes `adapter_sha256` over the deployed `~hermes/.hermes/plugins/model-providers/kiro-acp/**` (regular files only, `O_NOFOLLOW`, sorted relative paths) and over the root-owned copy. A mismatch fails the probe with `adapter_drift`.
- **Probe key.** `adapter_sha256` joins the key: `(kiro_cli_version, hermes_agent_sha256, permissions_sha256, settings_sha256, adapter_sha256)`.
- **Execution as hermes only.** Root launches anything hermes-owned only via `runuser -u hermes --` (already the case for `kiro-cli acp`). It reads hermes-owned files only to hash them, never to parse them as code or config that root acts on.
- **Tests.** Add `tests/unit/ctl/test_hermes_probe_paths.py`: `sys.path` and `importlib` origins of `kiro_acp` in the probe process are under `/opt/ossbot`, and a modified hermes-side copy → `adapter_drift`.

### NIT

**R4-N1.** §9.8 says the unbundle job "checks `git bundle list-heads` (exactly `refs/heads/ossbot-change`, M1)" for every mode. §9.13 step 4 fetches `<import_head_ref>`, which is any `refs/heads/<name>`. Make the check mode-specific:
- `mode=fix_round`: the single head is `refs/heads/ossbot-change`.
- `mode=import`: the single head equals `contributions.import_head_ref`, passed in `bundle_ref` by the runner from gatekeeper state.

**R4-N2.** §12.5 `control_checkpoint()` requires only `running`. A `/dryrun on` that lands between step 1 (snapshot, `dry_run=false`) and a later write does not stop that write. NFR-1 binds the exact mode, and §12.4 says `dry_run` is recomputed "immediately before the first write". Make the checkpoint also require `control_state.dry_run == snapshot.dry_run`. On a mismatch, use the same `interrupted` → `SUBMIT_FAILED` path, with the note "mode changed during submission".

**R4-N3.** §16.3 and §10.9 step 2 say "runner upserts `model_catalog` via the gatekeeper", but the closed endpoint list in §12.2 (`extra="forbid"`) has no such route. Add `POST /v1/models/catalog  worker  {models: [{model_id, name}]}`. Validate each `model_id` with the same regex as `--force` (`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`), cap at 200 entries, and never set `forced` from this route.

**R4-N4.** §15.4 install script hygiene:
- Root runs `git clone` and `chown -R` at `$D.new` inside `/home/hermes/.hermes/`, which `hermes` can write. On `ossctl hermes upgrade`, a pre-planted `$D.new` symlink would redirect root's writes.
- `mv -T "$D.new" "$D"` fails when `$D` already exists; the `$D.prev` step is implied but not shown.

Fix: clone and verify in a root-owned staging dir (`/var/lib/ossbot/staging/hermes-<tag>`, removed first if present), `chown -R --no-dereference hermes:hermes`, then `mv -T "$D" "$D.prev"` (after removing an old `.prev`) and `mv -T staging "$D"`. Refuse to proceed if any of these paths is a symlink (`test ! -L`).

**R4-N5.** §10.8 create spec allows any `Cmd` from the entrypoint set for any profile, and `KIRO_API_KEY` for `catalog`. A compromised sandboxd is out of scope, because it already holds the key. But the filter is meant as an independent backstop, and as written it would accept `profile=catalog` + `kiro.sh` + key, which bypasses the probe gate at the filter layer. Bind `Cmd` to profile:
- `catalog` ⇒ `list_models` only;
- `research` and `agent-llm` ⇒ `kiro` only;
- `agent-exec` ⇒ `exec`;
- `snapshot` ⇒ `snapshot`;
- `probe` ⇒ `probe`.

Add one deny-corpus case.

**R4-N6.** §9.11 and §12.5 step 5: `FORMAT_PATCH` pins `diff.renames=false`, but both numstat invocations (`git diff --numstat …` in `package.sh` and in the gatekeeper) do not. So the numstat-set comparison depends on each side's built-in rename default. Run both with the same `-c diff.algorithm=myers -c diff.renames=false -c core.quotePath=true` prefix, ideally as `ossbot.common.gitcmd.NUMSTAT` next to `FORMAT_PATCH`.

---

## Verified assumptions

- **git 2.55.0 (local, this round):**
  - A bundle created from `checkout -B ossbot-change` with `bundle create refs/heads/ossbot-change ^<base>` lists exactly `<sha> refs/heads/ossbot-change` and one prerequisite. This confirms the M1 fix shape.
  - `fetch <bundle> refs/heads/ossbot-change:refs/ossbot/r1` succeeds without `protocol.file.allow=never`.
  - The same fetch fails with `transport 'file' not allowed` under `protocol.file.allow=never` (R4-M1).
  - `bundle verify` succeeds under `protocol.file.allow=never`.
- **M4 redirect (design-internal):** it only ever targets `WATCHING`, and from GATE states the actors stay G|H. A worker cannot reach any gate state or approval through it. `WATCHING→ABANDONED` is H only.
- **M5 (design-internal):** reconciliation is reads only, and an `executing` row counts against rate limits until resolved. Resume never re-enters steps 4–6 (§12.5, §13.2, ADR-0008).
- **M3 (design-internal):** the import route takes bytes only. The gatekeeper never opens an operator-named path. The imported tree is re-authored by `package.sh` and re-derived by the gatekeeper before any card.
- **M9 (design-internal):** the probe result is written by root to a root:gatekeeper 0640 file that Hermes cannot write. The relay requires a pass younger than 2 h. An inconclusive probe disables chat without killing.
- **M11 (design-internal):** the `catalog` profile has no prompt, agent or inputs, and a fixed network from the `agent-llm` pool. `KIRO_API_KEY` is allowed only for research, agent-llm and catalog, in both the argv test and the filter.
- **Repo docs:** all 18 ADRs exist. The revision-3 amendments in ADR-0002, 0003, 0005, 0008, 0014, 0017 and 0018 match §9, §10, §12, §13 and §15.
- **User intent:**
  - Isolation inside `D:\Work\autonomous-oss-contributor`: the distro VHD is in `.wsl\`, the drvfs mount covers only the repo, and interop is off.
  - The dedicated `ossbot` distro, the Telegram-only two-tap approval as the sole human gate, and kill levels soft, full and hard all hold.
  - Kiro CLI V3 headless for pipeline roles, Hermes over V3 ACP behind the single bot, the global model switch (`models.yaml` plus the catalog), AI disclosure, and maintainer/contributor vetting (§9.5) are all present.
  - DCO identity is `Amsal Khan <md.amsalkhan@gmail.com>`, and the GitHub login is `Amsal1`.
  - README, AGENTS.md and SETUP.md are required in FR-16 and §6.1.

## Unverified or wrong assumptions

- **Wrong (R4-M1):** that `protocol.file.allow=always` is needed for `git bundle verify` and not for `git fetch <bundle>`. It is the other way round.
- **Unspecified, security-relevant (R4-M2):** the code origin of `kiro_acp` inside the root probe process.
- **Not re-checked this round, correctly tagged [VERIFY] in the design (§23):**
  - Docker 29 copy-up ownership and `exec` tmpfs (item 14).
  - Whether kiro-cli needs exec under `/tmp` (item 5).
  - Hermes `tool_search.enabled: off` parsing and api_server behaviour (item 13).
  - V3 ACP `currentModeId` and the tool-list field, and custom `modeId` selection (item 11).
  - Kiro V3 Tool Search and MCP ids (item 5).
  - The peeled commit of `v0.22.2` and the `setup-hermes.sh` steps (item 13).
  - `patch-id --stable` equality across the image and gatekeeper git versions (item 19).
- **Not re-checked, carried from earlier rounds and still tagged:** Telegram Bot API limits; GitHub PAT behaviour; Docker firewall backend and embedded DNS; drvfs fstab escaping; `instanceIdleTimeout=-1`; V3 ACP auth options; `--list-models` schema; squid attachment limits; `age -d -i -`.
