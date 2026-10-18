# FEAT-006 build review, round 2: pipeline backend (runner, gate 1, size budget, model switch)

Round 2 covers the 25 commits after round 1 (`ece62ea..52bca2a`, 98 files, +4142/−407). They close every round-1 finding, finish items 30–32 and record two owner decisions (decision A, the hardened scout clone; decision B, probe verdicts), plus the `roles.test` / per-role fallback model rules. The runner now re-queues sandbox interruptions, runs M11 in order with backoff, lets drains finish, paces scouting against the daily cap, checks relevance before a re-scout, and takes "the repo has a suite" from `fetch.json`. The hermetic bar, the lint gate, the root suites and all five system suites pass on the deployed build (I re-ran them all). One new defect blocks approval: going back to gate 1 from `RESCOPE_REQUIRED` cannot work and loops.

Watch for:
- **likely**: a re-scout from `RESCOPE_REQUIRED` (Make it smaller, Split, Adjust after a ceiling or `reapprove` pause) runs the scout clone into the task volume that still holds the build clone. `git clone` refuses a non-empty `/work/repo`, the stage fails, `fail()` is a no-op for that state, and the next tick tries again, forever. A re-approved proposal's real fetch would hit the same non-empty volume.
- **confirmed**: the scheduler only sees the 200 most recently updated contributions of any state, so once terminal items pile up, quiet `WATCHING`, snoozed and expired items stop being dispatched.
- **confirmed**: AC 2 is unmet. The `needs_key` sandbox tests are empty stubs with an unconditional skip, and several TRACEABILITY `[VERIFY]` rows are closed with evidence that does not support them.
- **confirmed** (user decision): docs say the test roles are switchable from Telegram `/model`, and no model switch sends a Telegram FYI.

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 1 HIGH, 6 MEDIUM, 3 NIT)

## High-level view

Sandboxd answers now become outcomes in one place, `errors.sandbox_run`. The probe gate's 423 holds every stage and re-opens the M11 probe step, which runs on `roles.test` per the recorded owner decision. Gate 1 holds before work starts: the only pre-Start job is the hardened depth-1 scout clone, and its volume is gone when the stage ends.

Going back to gate 1 after work has started is broken. The re-scout and the second fetch both assume an empty task volume, and after implementation the volume holds the build clone. The scheduler's view of the queue is a 200-row window ordered by `updated_at`, so long-waiting items fall out of it.

The evidence is weaker than the FEAT claims in three places. AC 2's `needs_key` cases still never run. Some `[VERIFY]` rows are marked done with a pasted evidence string that has nothing to do with them, and some FEAT-006-only rows are still `missing`. VERIFICATION still says the root and system suites were not run. The user's new decision on test roles also needs doc fixes and a model-switch FYI. The probe run is about 80 s against the owner's 60 s target.

<details>
<summary>Issues (10)</summary>

1. **F006-R2-1 (HIGH) Back to gate 1 from RESCOPE_REQUIRED loops**: the re-scout from `RESCOPE_REQUIRED` and the fetch after re-approval both clone into the task volume that already holds the build clone. Read the existing build tree for the re-scout (or clone into a separate path), make the post-re-approval fetch reset or reuse the volume, stop the silent `NO_FAIL` retry loop, and add runner/e2e tests for Smaller/Split from `RESCOPE_REQUIRED`.
2. **F006-R2-2 (MEDIUM) Scheduler blind past 200 contributions**: `_dispatch` reads `GET /v1/contributions?limit=200` ordered by `updated_at DESC` over all states. Query non-terminal states only (or paginate), and add a test with more than 200 terminal rows.
3. **F006-R2-3 (MEDIUM) AC 2 needs_key cases still skipped**: `test_kiro_host_connect` and `test_workspace_canary_s_ws_1` are empty and unconditionally skipped. Implement them against the real key (or tie S-ws-1 to the live `workspace` probe with a recorded result), and update the ADR-0005 addendum, which still lists items 5b, 6 and 8 as pending.
4. **F006-R2-4 (MEDIUM) TRACEABILITY rows closed without matching evidence, others left open**: DES-V-8, DES-V-17 and DES-V-18 are `done` with the generic "catalog 20 ids … targets verify 73/73" string, and DES-V-5/5a/21/24/33 cite runner code instead of their ADR addendum. DES-V-9, CR4-1h and CR4-2c (FEAT-006 only) are still `missing`. DES-V-5b and DES-V-6 say "-> FEAT-007" but their phase column names only FEAT-006. DES-FR-14 still says the import CLI is missing. Fix each row with its real evidence or owner.
5. **F006-R2-5 (MEDIUM) Docs offer the test roles in Telegram /model**: DESIGN §16 ("Test roles" paragraph), README "Models" and TRACEABILITY MDL-3 ("Telegram /model picker shows test roles (FEAT-009)") contradict the user's decision. Say test roles change only through `config/models.yaml` / `ossctl model set test …`, and make MDL-3 tell FEAT-009 to exclude them from the picker.
6. **F006-R2-6 (MEDIUM) Model switches are not announced**: `ModelsAdmin.set_model` and `PUT /v1/models/{role}` audit and return a diff but queue no Telegram FYI, while DESIGN §16 and the user require "audited, catalog-validated, FYI". Queue one owner FYI (role, old → new, actor) through the outbox in the same transaction as the audit, and test it.
7. **F006-R2-7 (MEDIUM) Probe run over the owner's 60 s target**: a full probe run takes about 80 s (six sequential Kiro sessions; doctor 86 s), and the target is recorded nowhere. Bound or parallelise the sessions, or ask the owner (hard rule 12) and record the accepted figure in DESIGN §10.9.
8. **F006-R2-8 (NIT) Runner crash case passes vacuously**: `test_crash_safety.sh` SIGKILLs the runner without first putting a stage in `running`. Seed a running stage (fake upstream) before the kill and assert it is re-queued.
9. **F006-R2-9 (NIT) targets verify transient unit is unhardened**: the `systemd-run --uid=ossrunner` worker lacks the runner unit's `NoNewPrivileges`, `ProtectSystem`, `RestrictAddressFamilies`, io_uring `SystemCallFilter` and `ExecStartPre` firewall check. Pass the same `-p` properties.
10. **F006-R2-10 (NIT) VERIFICATION "Phase 2 live checks" is stale**: the "Root suites" and "System tests" rows say not run, and the "Global switch" row contradicts the "Global switch, live" row. Record the round-2 results (below) and drop the contradicting row.

</details>

<details>
<summary>Details</summary>

### F006-R2-1 (HIGH): going back to gate 1 from `RESCOPE_REQUIRED` cannot complete and retries forever

`stages.stage_for` sends `RESCOPE_REQUIRED` with pending feedback to `scout.run`. The gatekeeper allows Smaller/Split there (`ProposalService.request_revision`, `allowed = {PROPOSED, RESCOPE_REQUIRED}`), and `RESCOPE_REQUIRED` is the state the item pauses in after `/package` measured a ceiling hit or a `reapprove` overrun. By then the task volume holds the build clone with the implemented commits. sandboxd refuses to destroy it (`RESCOPE_REQUIRED` is neither terminal nor in `PRE_START_STATES`), so the volume survives.

`scout._scout` then submits the scout-mode `fetch` job into that volume. `fetch.sh` removes `/work/repo` only when the `.scout-clone` marker exists. The build clone has no marker, so `git clone … "$REPO"` runs against a non-empty directory, git refuses, and the job ends `clone_failed`:

```bash
if [[ -f "$SCOUT_MARK" ]]; then rm -rf "$REPO" "$SCOUT_MARK"; fi  # build clone: not removed
mkdir -p "$REPO" ...
"${G[@]}" clone ... "https://github.com/$SLUG.git" "$REPO" || fail clone_failed "git clone failed"
```

`StageFailed("clone_failed")` reaches `Scheduler.fail`, which returns without a transition because `RESCOPE_REQUIRED` is in `NO_FAIL`. Feedback is still pending, so the next tick (10 s later) dispatches the scout again. The result is an unbounded stream of clone jobs, failed stage runs and audit rows, with no message to the owner. If the re-scout did get through, the new version's Start work leads to `PROPOSAL_APPROVED → FETCHING`, and the real fetch hits the same non-empty volume.

No test in `tests/unit/runner` or `tests/e2e` drives the runner through `RESCOPE_REQUIRED`. Confidence is "likely": the git refusal and the state handling are confirmed from the code, but this path was not run.

Fix options: for a re-scout after Start work, snapshot the existing build tree (research profile, ro) instead of cloning. Or make scout mode clone into its own path, and have the post-re-approval fetch start from a reset volume (sandboxd `destroy` for `PROPOSAL_APPROVED` items that come from a rescope, or `fetch.sh` resetting `/work/repo` when no build is in progress). Then make a repeated `StageFailed` in a `NO_FAIL` state alert the owner and back off rather than retry silently.

### F006-R2-2 (MEDIUM): the scheduler only sees the newest 200 contributions

`Scheduler._dispatch` iterates `self.sc.gk.contributions(limit=200)`, and `ContributionService.list` answers `ORDER BY updated_at DESC LIMIT ?` over every state (`le=200` on the route). Discovery creates candidates every cycle, and most end `SKIPPED` within minutes: the live check skipped 3 of 3. Over weeks, terminal rows outnumber the window. Items that wait without updates then drop out of it and are never dispatched. That covers `WATCHING` (an open PR whose review comments the watcher must answer), `PROPOSAL_SNOOZED` past its snooze and `PROPOSAL_EXPIRED` past the 7-day cooldown. `sweep_volumes` has a per-cid fallback, but dispatch does not.

Fix: list only states that have a stage (a state-set filter or an `active=1` query on the gatekeeper side), or page through the results. Add a test with more than 200 terminal rows and one old `WATCHING` item.

### F006-R2-3 (MEDIUM): AC 2's `needs_key` cases still do not run

AC 2 requires `ROOTDEV pytest -m "sandbox or e2e_docker"` to pass "with the needs_key cases now running against the real key". My run gave 11 passed, 2 skipped. `tests/sandbox/test_profiles.py::test_kiro_host_connect` and `::test_workspace_canary_s_ws_1` are bodiless functions under an unconditional `pytest.mark.skip`. The live `workspace` probe (`sandboxd.probes.eval_workspace`, 5/5 PASS) covers S-ws-1 in substance, but nothing ties the two together. The ADR-0005 item-30 addendum still says items 5b, 6 and 8 "stay pending live probe", while TRACEABILITY marks DES-V-8 done.

Fix: implement both tests to run when the sandbox key is present (they run as root through sandboxd, so product code reads the key). Or replace the S-ws-1 stub with an assertion over the recorded `workspace` probe result for the current tuple. Then update the ADR-0005 addendum for item 8, and record 5b and 6 as moved.

### F006-R2-4 (MEDIUM): TRACEABILITY bookkeeping does not match the evidence

The close-out pasted two generic evidence strings across rows they don't prove:

- DES-V-17 is about whether `public_repo` is sufficient for fork, push, PR and comments, plus fork latency and secondary limits. DES-V-18 is about whether the commit email is verified on `Amsal1` and not linked to `<work-login>`. Both are `done` with "catalog 20 ids, probes 5/5 on roles.test, kiro smoke pong, targets verify 73/73". None of those checks touches the write token or the account emails. ADR-0006's addendum still lists the live fork/push/PR behaviour as pending.
- DES-V-8 has the same string (see R2-3).
- DES-V-5, 5a, 21, 24 and 33 cite "runner/{scheduler,…}.py … 2457 hermetic passed", although their evidence is the ADR-0005 addendum.

Rows whose phase names only FEAT-006 and are still `missing`: DES-V-9 (live catalog spelling is now recorded in VERIFICATION), CR4-1h (the text says "rework done") and CR4-2c. DES-V-5b and DES-V-6 say "-> FEAT-007" in the evidence, but their phase column still reads `P2 FEAT-006`, so the FEAT-007 matrix check won't list them. DES-FR-14's evidence still says "`ossctl contribution import` CLI missing".

Fix: give each row its real evidence (ADR addendum or VERIFICATION line). Set DES-V-17/18 back to `missing` with an owner FEAT, or verify them read-only (DES-V-18 can be checked on GitHub's public commit attribution docs and the profile; write scopes stay pending until the first approved submit). Move 5b/6 to `P2 FEAT-007`, close DES-V-9/CR4-1h/CR4-2c or give the open part an owner, fix DES-FR-14, and recount the Totals line.

### F006-R2-5 (MEDIUM) and F006-R2-6 (MEDIUM): test roles and the model-switch FYI (user decision)

The user's decision supersedes the coder's earlier instructions: `test` and `test_vision` must not appear in the Telegram `/model` picker, and changing them must be audited, catalog-validated and announced by a Telegram FYI. No picker exists yet (item 47 is FEAT-009), so there is no code to remove. Three documents say otherwise, though:

- DESIGN §16, "Test roles" paragraph: "it is switched like any role (`ossctl model set test <id>`, Telegram `/model`: audited, FYI, catalog-validated)".
- README "Models": "Switch any role, including `test`, with `ossctl model set …` or the Telegram `/model` picker".
- TRACEABILITY MDL-3: "remaining: Telegram /model picker shows test roles (FEAT-009)". This would make FEAT-009 build the thing the user ruled out.

On the FYI: `ModelsAdmin.set_model` (`gatekeeper/models_admin.py`) appends `model_set` to the audit log and returns the diff. `PUT /v1/models/{role}` passes the diff back to `ossctl`. Nothing is queued on the outbox, so `ossctl model set test …` (or any role) changes the model silently on Telegram. Catalog validation and audit are in place. Fix: queue one owner FYI (`🧠 Model: <role> <old> → <new> (by <actor>)`) in the audit transaction, also for `--force`. Add a unit test in `test_models_admin.py`, and add a row (or extend MDL-3) for "test roles never in `/model`" so FEAT-009 tests the exclusion.

### F006-R2-7 (MEDIUM): the probe run is over the owner's 60 s target

The FEAT handoff records that a full probe run takes about 80 s ("6 sequential Kiro sessions") "above the owner's 60 s target". VERIFICATION shows doctor `--probes` at 86 s. `ProbeRunner` runs the sessions one after another, including the two-part `research_deny` and up to 3 inconclusive retries. That figure is the floor, not the worst case. The target appears in no DESIGN section or TRACEABILITY row, so nothing tracks it. Fix: run independent probes concurrently, or bound each session (for example a per-probe timeout that yields INCONCLUSIVE). Or, per hard rule 12, ask the owner to accept the measured figure and record it in DESIGN §10.9.

### NITs

F006-R2-8: the runner case added to `test_crash_safety.sh` SIGKILLs `ossbot-runner` and then checks "no stage run left running with a stale heartbeat". If no stage was running at the kill, which is the usual idle case, the check passes without testing anything. The mid-stage behaviour is covered in-process by `test_e2e_resume.py` (a simulated kill, not a real SIGKILL). Seed a running stage against the fake upstream first.

F006-R2-9: `ossctl targets verify` starts `ossbot.runner.verify_targets` with `systemd-run --uid=ossrunner -p EnvironmentFile=… -p PrivateTmp=yes` only. It reads GET-only through 3140 and the owner firewall rules apply by uid, but the transient unit lacks the hardening every ossrunner process otherwise has. Add `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `RestrictAddressFamilies`, the io_uring `SystemCallFilter`/`SystemCallErrorNumber`, `CapabilityBoundingSet=`, and a firewall check first.

F006-R2-10: VERIFICATION "Phase 2 live checks" still says the root suites and system tests were "not run … left for the review round". It also keeps a "Global switch: not exercised as a live switch" row next to "Global switch, live: the next invocation ran with the new `--model`". Record the round-2 results below and remove the contradiction.

</details>

## What I verified and how

- Scope: `git diff 03e5d54..HEAD` with the focus on `ece62ea..52bca2a` (the round-2 commits). I read FEAT-006, `context.json`, the build plan (hard rules, items 28–32, working rules, the GitHub MCP addition, which is moved to FEAT-007), the round-1 review, DESIGN §9.1/§9.5a/§16 and the AC 37 diff, the ADR-0005/0006 addenda, and the code of `runner/{errors,scheduler,pipeline,verify_targets}.py`, `runner/stages/{__init__,common,scout,review}.py`, `sandbox/entrypoints/fetch.sh`, `sandboxd/jobs.py` (destroy, `PRE_START_STATES`), `sandboxd/probes.py`, `gatekeeper/{models_admin,proposals,state_machine,contributions}.py`, `systemd/ossbot-runner.service`, `scripts/{lib/services.sh,bin/ossbot-kill,bin/ossbot-resume}` and `tests/e2e/{test_e2e_proposal,test_e2e_resume}.py`.
- Round-1 findings: R1-1 to R1-9 are all addressed in code, tests and docs. R1-5 follows owner decision A, recorded in DESIGN §9.1/§9.5a, AC 37, ADR-0005 and DES-DEF-13. R1-4 drops `require_up` from the heartbeat, which DESIGN §13.5 wins over the build-plan wording.
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e"` gave **2458 passed, 13 deselected** (263 s), which matches the FEAT finding.
- Lint gate: `ruff check` all passed, `ruff format --check` 281 files formatted, `mypy` no issues in 146 files, `lint-imports` 5 contracts kept.
- Redeploy: `bootstrap.ps1 -Only provision` printed `unchanged` for every step. The deployed build equals HEAD, the images are unchanged, and doctor reports PASS 33, SKIP 3 (Hermes): catalog 20 models, safety probes all passed for `b9dbc1c08d4b`, dry_run True, egress open, tier self_improve. `systemctl is-active` reports runner, gatekeeper, sandboxd and proxy all `active`.
- `ROOTDEV pytest -m "sandbox or e2e_docker"`: **11 passed, 2 skipped** (the two `needs_key` stubs, R2-3), 45 s. That includes both `e2e_docker` tests (scout clone runs nothing and is destroyed; build fetch → validate → package). No "Pool overlaps" this time.
- System suites, run as root on the deployed build: `test_config_write.sh`, `test_crash_safety.sh` (incl. the runner case), `test_lifecycle.sh`, `test_egress_surface.sh` (runner families, seccomp, io_uring, no linger, no interop, 3140 denials in every egress mode) and `test_firewall_failclosed.sh` (the runner start is refused by `ExecStartPre --check`). All five exited 0 with "all checks passed". Afterwards the units were active again, and `/var/lib/ossbot/control` held no `killed`, `paused` or lifecycle flag.
- gitleaks (distro `/usr/local/bin/gitleaks git --redact --log-opts=--all`): 93 commits scanned, no leaks. I read no secret files, and no secret value appears in the diff or the docs.
- Safety by reading code: GET-only GitHub reads by the runner, Telegram only through `gk.notify` / outbox, dry-run untouched (doctor `dry_run True`), no new GitHub write path, the runner unit matches the non-root pattern (both firewall units, `--check`, kill condition, families, io_uring filter, `ProtectHome=yes`, `TimeoutStopSec=150`) and appears in `OSSBOT_ENABLE_UNITS`, the kill stop list, the resume order and `PHASE1_UNITS`. The coder agent has only `@ossbot-exec/*` tools, and `chat --v3` is unchanged.
- Live records: the doctor output above matches VERIFICATION (20 ids, the probe tuple). I did not re-run `ossctl kiro smoke` or `targets verify`, and I made no live GitHub or Telegram call myself.
- Git: all 30 commits since the base are by Amsal Khan <md.amsalkhan@gmail.com> (author and committer). `git status --porcelain` was empty before this review. `origin/main` equals `main`, with no commit missing locally: the owner's push (working rule 5), not a finding.

<details>
<summary>File map (round-2 commits)</summary>

- `src/ossbot/runner/errors.py` (new): `StageFailed`/`Interrupted`/`Deferred`/`ProbesNotReady`/`CapReached`, `sandbox_run`.
- `src/ossbot/runner/scheduler.py`: M11 order with retries, probe-gate hold, scout pacing hold, volume sweep, `recover_interrupted`.
- `src/ossbot/runner/pipeline.py`: heartbeat checks kill/pause only; sandbox notes.
- `src/ossbot/runner/stages/{scout,review,common,fetch,catalog,implement,importer}.py`: relevance check, cap pre-check, scout clone and destroy, `has_test_suite`.
- `src/ossbot/runner/{llm,output_parse,prompts,gk_client,sbx_client,verify_targets}.py`: V3 stream parsing, pacing/discover clients, `targets verify` worker.
- `src/ossbot/sandboxd/{jobs,probes,profiles,main}.py`: `PRE_START_STATES` destroy, probe verdicts and retries, two-part research_deny.
- `src/ossbot/gatekeeper/{contributions,control,proposals,state_machine,api/*}.py`, `migrations/0003_cr004.sql`: discover request, pacing, resumed count, claim guard without an issue.
- `src/ossbot/common/{models,config,schemas}.py`, `config/models.yaml`: test roles, per-role fallback, distinct-after-fallback.
- `src/ossbot/ctl/{cli,doctor}.py`: `discover`, `contribution import`, `targets verify`, `kiro smoke`, probes in M11 order.
- `sandbox/{entrypoints/fetch.sh,entrypoints/kiro.sh,images/llm/Dockerfile,kiro/settings/permissions.yaml,probes/*}`, `.kiro/agents/*.json`, `config/versions.lock.yaml`: scout mode, V3 engine and agents, digests.
- `systemd/ossbot-runner.service`, `scripts/lib/{services,users}.sh`, `tests/system/{lib.sh,test_crash_safety.sh,test_firewall_failclosed.sh}`: runner unit and wiring.
- `tests/e2e/**`, `tests/bin/kiro-cli`, `tests/fakes/upstream_fixture.py`, `tests/unit/{runner,ctl,gatekeeper,sandboxd}/**`, `tests/unit/test_no_hardcoded_models.py`: new and updated tests.
- `docs/{DESIGN,TRACEABILITY,VERIFICATION}.md`, `docs/adr/{0005,0011,0017}`, `README.md`, `docs/plan/*`: decisions, rows, live checks.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff ece62ea..52bca2a`.

</details>
