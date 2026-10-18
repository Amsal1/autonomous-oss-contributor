# FEAT-006 build review, round 1: pipeline backend (model switch, gate 1, runner core)

Round 1 covers the four commits on top of base `03e5d54` (cf0802d, abfb0ce, d20fbef, 7a1062e). Items 28 (model switch) and 29 (gatekeeper side of CR-004) are done. Item 30 (runner) is partly done, and items 31 and 32 have not started. The runner core has three defects in how it handles stops and failures. A sandboxd restart fails items instead of re-queueing them. The runner never runs the M11 safety probes, and while the probe gate refuses jobs it retries every 10 s with no backoff. A drain cuts running stages short instead of letting them finish.

Watch for:
- **confirmed**: FEAT-006 is unfinished. The tree is dirty (`FEAT-006.json` has uncommitted progress notes). There is no runner unit, no e2e suite, no redeploy and no live check.
- **confirmed**: if sandboxd is SIGKILLed or restarts mid-stage, the item goes to `FAILED` instead of being re-queued (AC 32, CR-001).
- **confirmed**: the runner never calls `POST /v1/probes/run` (M11). While the probe gate answers 423, every Kiro-stage item re-runs each 10 s tick, costing about 5 GitHub GETs, a stage-run row and 2 audit events each time.
- **confirmed**: a `/drain` or `down` interrupts running stages at the next heartbeat. DESIGN §13.5 says running work finishes during a drain.

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 3 HIGH, 5 MEDIUM, 1 NIT)

## High-level view

The model switch and gate 1 follow DESIGN rev 5. `scout` inherits `planner` and `vision` is a role. The claim guard moved to `PROPOSAL_APPROVED → FETCHING`, behind the consumed approval of the current version. The size preference, filters, "never" and the daily cap apply only to autonomous items, and the ceiling applies to all. `RESCOPE_REQUIRED` happens only for the ceiling or `reapprove`. Their live proof (catalog filled with the sandbox key, a switch reaching the next `--model`) is item 32 and has not run.

The runner core's failure handling has three problems. It turns sandbox interruptions into `FAILED`. It treats the probe gate's 423 as "try again next tick" with no backoff and never runs the probes itself. It applies `require_up` at every heartbeat, so a drain cuts work short. Separately, the tests-required backstop decides "the repo has a suite" from the scout's own `tests` list, which is untrusted output.

Two parts of the scout depart from DESIGN without a recorded decision. The scout snapshot is a `fetch`-entrypoint job in the task volume, run before gate 1, while §9.5a describes a fetch-free snapshot job. An expired or snoozed proposal is re-scouted with no "still relevant" check. Item 30's CLI commands (`ossctl discover`, `contribution import`, `targets verify`, the real `resumed` count for `ossctl up`), all of item 31 (e2e, `ossbot-runner.service` with hardening) and all of item 32 (images, live checks, VERIFICATION) are still to do.

<details>
<summary>Issues (9)</summary>

1. **F006-R1-1 (HIGH) Finish the WIP**: commit or finish the dirty `FEAT-006.json`. Complete the rest of item 30 and all of items 31 and 32, then set the FEAT status, findings, TRACEABILITY rows and VERIFICATION "Phase 2 live checks".
2. **F006-R1-2 (HIGH) Sandbox interruption fails the item**: map the sandboxd job result `failed`/`interrupted` and `SbxError` 503 (`sandboxd_unreachable`) to `Interrupted` (stage run `interrupted`, re-queue), not `StageFailed`/`fail()`. Add tests.
3. **F006-R1-3 (HIGH) M11 first start incomplete, probe-gate hot loop**: have the runner call `probes_run` with resolved models after the catalog. Hold all Kiro stages, with no transition and no stage run, while the catalog is empty or the probes have not passed. Send the `catalog_empty` alert with an hourly retry. Back off on 423 instead of re-running every 10 s.
4. **F006-R1-4 (MEDIUM) Drain interrupts running stages**: at heartbeat check only kill and pause (`require_running`). Use `require_up` only before a stage or job starts, so a drain lets work finish within sandboxd's timeout. Fix `test_heartbeat_interrupts_a_running_stage[draining]`.
5. **F006-R1-5 (MEDIUM) Scout clone is a fetch job before gate 1**: implement the DESIGN §9.5a read-path snapshot (runner GET tarball, then a `snapshot` job), or record the decision in DESIGN §9.1/§9.5a, an ADR addendum and TRACEABILITY, and word AC 37 and `test_e2e_proposal` to match.
6. **F006-R1-6 (MEDIUM) Daily cap re-scout loop**: check `proposal.max_per_day` before scouting (a cheap gatekeeper pre-check), or hold the item until the window rolls. Today a full clone and Kiro session run every tick and are thrown away on 429.
7. **F006-R1-7 (MEDIUM) "Has a suite" decided by model output**: derive `has_suite` deterministically (for example test-glob matches in the fetched tree, recorded in `fetch.json`). Don't rely on the scout's `tests` list.
8. **F006-R1-8 (MEDIUM) Re-scout without a relevance check**: before `PROPOSAL_EXPIRED/SNOOZED → SCOUTING`, re-read the issue (open, unassigned, no linked open PR, rediscover rules) and take `→ SKIPPED` when it no longer applies.
9. **F006-R1-9 (NIT) Claim wait without an issue**: with `proposal.claim_comment: on`, a repo-level or PR `/work` item waits in `PROPOSAL_APPROVED` forever (`Deferred("claim_without_issue")`). Skip the claim requirement when there is no issue, or tell the owner.

</details>

<details>
<summary>Details</summary>

### F006-R1-1 (HIGH): FEAT-006 is unfinished and the tree is dirty

`git status --porcelain` shows ` M docs/plan/task-ossbot-v1/features/FEAT-006.json`. These are the coder's progress notes, and they are uncommitted. The findings say this directly: "Item 30 still open: ossctl discover / contribution import / targets verify, ossctl up real resumed count, TRACEABILITY rows. Then item 31 (e2e, runner unit), item 32 (redeploy, images, live)."

Confirmed missing:

- `ossctl discover`, `ossctl contribution import`, `ossctl targets verify`. None are in `ctl/cli.py`.
- `tests/e2e/**`, `tests/fakes/upstream_fixture.py` and `tests/bin/kiro-cli`. There is no e2e test: `-m e2e` collects nothing new.
- `systemd/ossbot-runner.service` and its entries in `OSSBOT_ENABLE_UNITS`, the kill stop list, `tests/system/lib.sh` and `test_crash_safety.sh`.
- The image rebuild. `sandbox-llm` doesn't carry the new `.kiro/agents/*.json`, `prompts/*`, the `scout` role in `kiro.sh` or the `prefetch` switch in `fetch.sh`, and `config/versions.lock.yaml` has no new digests.
- Item 30's TRACEABILITY rows (DES-FR-3/4/10/12/14, DES-V-5/5a/5b/6/8/21/24/33, CR1-3a2/3c/4b, CR4-1a..1d, CR4-2a/2e/2j/2m, CR4-3a/3b, CR4-4, CR4-5e, CHAT-13/28/32) are not updated.
- The DESIGN §23 items 5b, 6 and 8 left "pending live probe" in the ADR-0005 addendum.
- Any FEAT-006 entry in `docs/VERIFICATION.md`.

Acceptance criteria 1 (the e2e part, AC 4/5/8/28/30, `test_e2e_resume`), 2, 3, 5 (the `test_e2e_proposal` part) and 6 cannot be checked yet. Working rule 4 of the build plan requires CHANGES_REQUESTED while the tree is dirty.

### F006-R1-2 (HIGH): a sandbox interruption or restart fails the item instead of re-queueing it

sandboxd records a job cut by a drain timeout or a crash as `state: failed, error_class: interrupted` (`sandboxd/jobs.py` `INTERRUPTED`, `_record`, `_mark_interrupted`, `recover`). The runner treats every non-`succeeded` job as a stage failure:

```python
# runner/stages/common.py job() and runner/llm.py Kiro._once
if done.get("state") != "succeeded":
    raise StageFailed(str(done.get("error_class") or ...))
```

`Scheduler.run_one` then calls `fail()`, which moves the contribution to `FAILED` (`StageRun.__exit__` records the run as `failed`, not `interrupted`). When sandboxd is SIGKILLed or restarted, `Sandbox.run` gets an `httpx` error on its next poll and raises `SbxError(503, "sandboxd_unreachable")`. `run_one` handles only 423 as "retry later", so every other status goes to `self.fail(sc, cid, "sbx_...")`, and the item is `FAILED` before sandboxd even comes back to recover. This contradicts AC 32 ("A SIGKILL of any service or sandbox mid-stage, then restart, resumes or re-queues the task") and the CR-001 runner rule that the plan item spells out ("a sandbox job ending failed/interrupted marks its stage_run interrupted and re-queues"). The planned `test_crash_safety.sh` runner case and `test_e2e_resume` will hit this. No unit test covers it.

Fix: in `common.job` and `Kiro._once`, raise `Interrupted` for `error_class == "interrupted"`. In `run_one`, also treat `SbxError` 503 as transient (no `fail()`). Add unit tests for both paths. The requeue limit of 3 stays in the gatekeeper.

### F006-R1-3 (HIGH): the M11 first start is incomplete, and the probe gate turns into a hot loop

DESIGN §10.9 (M11) and `test_e2e_first_start.py` (§22.4) give the order `list_models` → catalog upsert → `POST /v1/probes/run` with resolved models → first stage. If `list_models` fails, a `catalog_empty` alert goes out, nothing is probed and no Kiro job starts, and the catalog is retried hourly (§20 failure table). `Scheduler.first_start` only reads `probes_status()` and logs a warning. `Sandbox.probes_run` exists but nothing calls it. The probe key includes `agent_configs_sha256`, so after the image rebuild of item 32 every tuple is new and no Kiro job can pass the gate until someone runs `ossctl doctor --probes` by hand. A failed catalog job only logs. Dispatch goes on, so every item resolves `model_unavailable` and is moved to `FAILED`, where DESIGN says it should wait.

The probe gate's 423 has no backoff. Each 10 s tick, a `VETTING` item runs `vet.run`. That opens a stage run (a row plus a `stage_started` audit event), makes three or more GitHub reads for facts and two for the issue and comments, and submits a Kiro job. The job gets 423 `safety_probe_missing`. `StageRun.__exit__` records `failed internal:SbxError` (another audit event), and `run_one` returns. Failed runs don't count toward `REQUEUE_MAX`, so this never stops. At `max_concurrent_pipelines` items, that is roughly 1,800 GitHub GETs per hour per item: three items exceed the 5,000/h token limit. It also adds about 1,700 hash-chained audit rows per hour per item. Scout items do the same, plus a clone job each time.

Fix: finish M11 in `first_start` (catalog, then `probes_run` with `resolve()`d planner and coder models, then stages). Gate Kiro stages on "catalog non-empty and probes passed" before any transition or stage run. Send `catalog_empty` with an hourly retry. Back off per item on 423, or hold the whole queue. Treat a refusal as "not started" rather than a failed run.

### F006-R1-4 (MEDIUM): a drain interrupts running stages at the next heartbeat

`Gate.check` runs `require_running` and `require_up`. `StageRun.tick` calls it at every poll (every 2 s inside `Sandbox.run`), and the runner cancels the sandbox job on `NotUp`. DESIGN §13.5 says the lifecycle "only stops *new* work", "the runner schedules no stage", "the Docker filter is unchanged, so running jobs can finish", and `/drain` means "stop scheduling new work, running work finishes". `down` gives running jobs `S` seconds through `POST sandboxd /v1/drain`, and AC 31 reports "N finished, M re-queued". As written, N is effectively always 0: a drain cancels a 50-minute implement session within seconds. `tests/unit/runner/test_pipeline_gates.py::test_heartbeat_interrupts_a_running_stage[draining]` asserts this behaviour. The build plan's wording ("`require_up` … at each heartbeat") conflicts with DESIGN, and DESIGN wins.

Fix: check kill and pause at the heartbeat, and lifecycle only before a stage and before each new sandbox job (sandboxd's 423 `draining` then stops the next job of a multi-job stage). Treat that 423 and sandboxd's `interrupted` result as `Interrupted` (see R1-2). Change the draining case of the test to "the stage completes".

### F006-R1-5 (MEDIUM): the scout snapshot is a `fetch` job in the task volume, before gate 1

`runner/stages/scout.py` runs `job(sc, cid, "scout", "fetch", "fetch", {"repo", "clone_depth": 1, "prefetch": False}, "rw")`. That is the `fetch` profile and entrypoint, with `repo_mode rw` in `ossbot-task-<cid>`, before Start work. `fetch.sh` gains `OSSBOT_ARG_PREFETCH=0` and a `.scout-clone` marker. DESIGN rev 5 says something else. §9.1 row 2a gives "`snapshot` of a shallow read-only clone (no net)". §9.5a describes "a shallow read-only snapshot of the default branch (`fetch`-free: a `--depth 1` clone by the runner's read path into a `snapshot` job …)". §9.1 also says "fetch (deps, task volume) runs only after Start work", and AC 37 says "no fetch, plan or implement job runs for it until Start work".

No repo code runs, because the dependency prefetch is skipped, so the safety risk is small. Still, the fetch profile's egress (registries) is used before gate 1, a task volume exists for candidates the owner may skip, and a literal `test_e2e_proposal` would fail. The decision is recorded only in the FEAT findings. It is missing from DESIGN, the ADR-0005 addendum and TRACEABILITY, which the contribution rules require for a decision change.

Fix: implement the read-path snapshot. For example, the runner GETs `/repos/{r}/tarball/{default}` through 3140 and stages it as a `snapshot` input, which needs a sandboxd input type. Or ask, per hard rule 12, and record the deviation in DESIGN §9.1/§9.5a, an ADR addendum and TRACEABILITY, with AC 37 and its test worded as "no dependency fetch, plan or implement".

### F006-R1-6 (MEDIUM): hitting `proposal.max_per_day` re-runs the whole scout every tick

`ProposalService.submit` checks the daily cap only when the finished proposal arrives (429). `scout.run` turns the 429 into `Deferred`, so the item stays `SCOUTING`. `Scheduler._eligible` returns `True` for `SCOUTING`, so the next tick (10 s later, capped at `max_concurrent_scouts`) clones again, runs another full Kiro scout session, reads the PRs again and gets 429 again, until the 24 h window rolls. With the defaults (`proposals_per_cycle: 3` every 6 h, so up to 12 a day, against `max_per_day: 5`), this is reachable on an ordinary day.

Fix: check the cap before scouting, either from the gatekeeper's prefs or count endpoint or with a pre-flight `POST`. Or store the deferral time and skip the item in `_eligible` until the window opens.

### F006-R1-7 (MEDIUM): the tests-required backstop trusts the scout's claim that the repo has no suite

`review.run` computes `has_suite = bool(sc.gk.proposal(cid)["proposal"].get("tests"))`. If the scout writes `tests: []` with any `no_suite_reason`, the coder can ship a change with no tests, and the deterministic rejection is skipped (`tests_exception = "no_suite"`). This is exactly the case AC 41 and the user's request ("if the repo has tests … we should try to add tests") ask the backstop to catch. The proposal card shows the reason to the owner, which helps, but the review rule is meant to be deterministic and should not depend on model output.

Fix: detect a suite from the fetched tree, for example any path matching `size.test_globs` or known test configs (`pytest.ini`, `jest.config.*`, `*_test.go`), recorded in `fetch.json` by `fetch.sh`. Use that in `enforce_tests`, and refuse (or flag on the card) a proposal whose `no_suite_reason` contradicts it.

### F006-R1-8 (MEDIUM): expired and snoozed proposals are re-scouted without checking that they still apply

AC 37 and §9.5b say "an expired proposal is re-scouted if still relevant", and the state machine has `PROPOSAL_EXPIRED → SKIPPED` for the opposite case. `scout.run` moves `PROPOSAL_EXPIRED`/`PROPOSAL_SNOOZED` straight to `SCOUTING`, and nothing re-reads the issue (closed, assigned, an open PR linked, rediscover rules). Nothing takes the `SKIPPED` edge. A closed issue gets a fresh proposal card.

Fix: add a cheap relevance check (`gh.issue`, `keep_issue`, `open_prs_mentioning`, `rediscover.eligible` with the current `issue_hash`) before the re-scout transition. Take `→ SKIPPED` with a reason when it fails, and add unit tests.

### F006-R1-9 (NIT): the claim wait never ends for an item without an issue

With `proposal.claim_comment: on`, the `PROPOSAL_APPROVED → FETCHING` guard requires an `issue_claim` action. `fetch._claim` raises `Deferred("claim_without_issue")` for a repo or PR `/work` item, every tick, with no message. Exempt items with no issue from the claim requirement in `_guard`, or tell the owner once.

</details>

## What I verified and how

- Scope: `git diff 03e5d54..HEAD` (106 files, +7104/−118) and the four commits. I read FEAT-006, the build plan (hard rules, B1–B10, items 28–32, working rules), `context.json`, DESIGN §9.1–§9.5b, §9.14, §10.9 (M11), §13.5, ACs 31/32/37–41, the TRACEABILITY rows the items name, and the ADR-0003/0005/0009 addenda.
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e"` gave **2382 passed, 11 deselected** (213 s). That matches the FEAT finding (2382). No `e2e`/`acp` tests exist yet.
- Lint gate: `DEV ruff check src tests` all checks passed. `DEV ruff format --check src tests`: 260 files already formatted. `DEV mypy`: no issues in 144 files. `DEV lint-imports`: 5 contracts kept, including "Untrusted-side components never import the gatekeeper" with `ossbot.runner` as a source.
- `ROOT shellcheck -x sandbox/entrypoints/fetch.sh sandbox/entrypoints/kiro.sh`: clean.
- gitleaks: `gitleaks git --redact --log-opts=--all .` (distro `/usr/local/bin/gitleaks`) scanned 67 commits, no leaks. I read no secret files. No secret values appear in the diff. The runner adds `GITHUB_READ_TOKEN` to its log redactor.
- Safety checks by reading code. Dry-run is untouched. Claims are outbound actions behind gate 2 with dry-run statuses, and there is no new GitHub write path. `runner/github_read.py` is GET-only through proxy 3140 (unit test `test_github_client_is_get_only`). Telegram text goes only through `ctx.notify` and the outbox. Kiro headless still runs `chat --v3` (`sandbox/entrypoints/kiro.sh:49`). The `scout` role was added to `ResearchRole`, `KiroRole`, `ROLE_REPO_MODE` and `TRUST_TOOLS` (B8). `toolSearch.enabled: false` is in `cli.json` and `SETTINGS_REQUIRED`. The coder agent has no web tools until §23 item 6 is confirmed. `.kiro/agents/*` are new files (only `.gitkeep` existed). The gatekeeper refuses stage runs while paused, killed, draining or down. Proposal binding is recomputed in `consume`.
- User-directed work: `create_user` accepts only GitHub URLs of enabled targets. `ProposalService.submit` skips the size preference, filters, never and cap for `source='user'` and keeps the ceiling for every item. Vet still refuses an AI ban for user items. `ratelimit.py` still enforces `open_prs_per_repo` at submit for every item. The override confirm is FEAT-009.
- Size: `size_report` splits code and test lines, and `RESCOPE_REQUIRED` happens only for `ceiling` or `reapprove`, matching §9.14 ("reapprove: back to gate 1 when the class grows").
- TRACEABILITY: the Totals line (deferred 26, done 178, missing 291, rework 13, 508 rows) matches a recount of the status column. The item 28/29 rows have evidence. The item 30 rows are untouched (R1-1).
- Git: every commit is by Amsal Khan <md.amsalkhan@gmail.com> (author and committer). `origin/main` = d20fbef is an ancestor of `HEAD`, which is the owner's push (working rule 5) and not a finding. The tree is not clean (R1-1).
- Not run: `ROOTDEV pytest -m "sandbox or e2e_docker"`, the system suites and live checks. The deployed app is the FEAT-005 build, the images are not rebuilt, there are no `e2e_docker` tests, and no runner unit exists (items 31 and 32). `docs/VERIFICATION.md` has no FEAT-006 live-check records to compare against.

<details>
<summary>File map</summary>

- `config/models.yaml`: `scout` (inherits planner) and `vision` roles.
- `src/ossbot/common/models.py`: `resolve()`, `ModelsFile` (mtime cache, last good), `parse_list_models`, catalog-or-forced.
- `src/ossbot/common/{config.py,schemas.py,rediscover.py}`: roles, `Proposal`/`WorkRequest`/`PrefsUpdate`, CR-004 states, Never/Skip rediscover rules.
- `src/ossbot/gatekeeper/{proposals.py,proposal_approvals.py}`: gate-1 content source, submit, Start/Adjust/Smaller/Split/decline/Undo, TTL sweep.
- `src/ossbot/gatekeeper/state_machine.py`: CR-004 edges, gate-1 guard, claim guard on `PROPOSAL_APPROVED → FETCHING`, evidence columns.
- `src/ossbot/gatekeeper/{packaging.py,outcomes.py}`: size report, `_rescope`, outcomes table writer.
- `src/ossbot/gatekeeper/{discovery_prefs.py,api/prefs.py}`: the only writer of size and filter preferences, 60 s undo.
- `src/ossbot/gatekeeper/{contributions.py,api/work.py}`: `/v1/work` intake (`source='user'`).
- `src/ossbot/gatekeeper/{stage_runs.py,api/stage_runs.py}`: stage-run bookkeeping, refusals while stopped, requeue limit.
- `src/ossbot/gatekeeper/{models_admin.py,api/models.py}`: model set with force, refresh request and claim, new-model FYIs.
- `src/ossbot/gatekeeper/migrations/0003_cr004.sql`: proposals, prefs, outcomes, `runner_requests`, contributions rebuild.
- `src/ossbot/runner/**`: scheduler, pipeline (gate, StageRun), clients, scoring, output parsing, prompts, all stages incl. scout.
- `src/ossbot/sandboxd/{profiles.py,jobs.py,probes.py}`, `sandbox/entrypoints/{fetch.sh,kiro.sh}`, `sandbox/kiro/settings/cli.json`: scout role, scout clone, Tool Search off.
- `.kiro/agents/ossbot-*.json`, `prompts/*.md`: role agents and prompts (tests-required rule, web-research rule, disclosure).
- `src/ossbot/ctl/cli.py`: `model list|set`, `models refresh`, `work`, `size show|set|undo`, `filters`.
- `docs/{DESIGN.md,TRACEABILITY.md,adr/0003,adr/0005,adr/0009}`: API table rows, matrix rows, addenda.
- Tests: `tests/unit/runner/**`, `tests/unit/common/test_models_resolve.py`, `tests/unit/gatekeeper/{test_proposals,test_size_report,test_discovery_prefs,test_work_intake,test_outcomes,test_stage_runs,test_models_admin,...}.py`.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 03e5d54..7a1062e`.

</details>
