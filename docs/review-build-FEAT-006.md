# FEAT-006 build review, round 3: pipeline backend (runner, gate 1, size budget, model switch)

Round 3 covers the 15 commits after round 2 (`aa6c06e..0318424`, 32 files, +677/−108). They close all ten round-2 findings. A re-scout from `RESCOPE_REQUIRED` now reads the build tree instead of cloning into it, and the next Start work builds from a fresh clone. The dispatch view pages through every non-terminal item. The `needs_key` cases run live through product code. Model switches send an owner FYI. The probe-duration target and the 30 s per-session cut are recorded as owner decision A + C. The doc rows were corrected. I re-ran the hermetic bar, the lint gate, a redeploy, the root suites and two system suites, and all are green. The live DB has no outbound action at all.

Two behavioural defects remain, plus one bookkeeping gap. The backoff defect is new this round. The GitHub-read defect dates from item 30 and shows in the runner's own live data.

Watch for:
- **confirmed** (live): a transient GitHub read failure fails the item for good. DESIGN §20 asks for 3 retries and a delayed stage on a rate limit. Two discovery candidates are `FAILED internal:githubreaderror` in the live DB because proxy 3140 refused for one moment.
- **confirmed**: the new backoff for a failed re-scout is keyed by contribution only. If the owner taps Continue after the failure, nothing is dispatched for up to 6 h, with no message.
- **confirmed**: three rows that only FEAT-006 can still close are `missing` although both of their parts are done. DES-V-8 is `done` while its FEAT-007 half is open.

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 0 HIGH, 3 MEDIUM, 2 NIT)

## High-level view

Going back to gate 1 after work has started now works end to end. `test_e2e_proposal.py::test_back_to_gate_1_from_rescope_required` drives a ceiling pause, Make it smaller, a scout session with no clone, PROPOSED without a volume, then a fresh fetch, plan and package measured on the new change only. A re-scout failure keeps the build tree for Continue and backs off with one owner message. But that backoff outlives the state it was meant for and holds whatever the owner decides next.

The runner reads GitHub with a single attempt per request. A refused connection, a 5xx or a rate-limit answer raises `GitHubReadError`. Discovery catches it per target. Vet and scout do not, and the scheduler's catch-all turns it into a terminal `FAILED internal:githubreaderror`. Autonomous candidates come back only after the 7-day cooldown. A 👤 item the owner sent with `/v1/work` has to be sent again.

The round-2 doc corrections are in, and VERIFICATION matches what I observed. Four rows still carry the generic evidence string, and three rows co-owned with the closed FEAT-005 were left `missing`.

<details>
<summary>Issues (5)</summary>

1. **F006-R3-1 (MEDIUM) Runner GitHub reads fail items on transient errors**: `GitHubRead._get` makes one attempt and has no rate-limit handling. Vet and scout let `GitHubReadError` reach the scheduler's catch-all, which sets `FAILED internal:githubreaderror` (observed live on 2 candidates). Implement DESIGN §20: retries at 2/8/30 s for network errors and 5xx, a delayed stage (sleep until reset, at most 1 h) for a 403/429 rate limit, and `error_class=github_unavailable` when the retries run out. Map the error to `Interrupted`/`StageFailed` in one place and add fake-transport tests.
2. **F006-R3-2 (MEDIUM) Re-scout backoff blocks the owner's next decision**: `Scheduler.held` is keyed by cid and checked before `stage_for` in any state. After a failed re-scout, Continue (`RESCOPE_REQUIRED → PACKAGING`) is not dispatched until the backoff ends (10 min, doubling to 6 h). Record the state with the hold and drop it when the state changes. Add a test: failed rescope re-scout, `continue_rescope`, package dispatched on the next tick.
3. **F006-R3-3 (MEDIUM) TRACEABILITY rows left open or closed too early**: DES-FR-21, CR4-2d and CHAT-32 (FEAT-005 part done, FEAT-006 part done) are still `missing`. DES-FR-10, DES-FR-21, CR4-2d and CHAT-32 cite the generic "runner/{scheduler,…} (914d20a, fcefda3, a51fa5f, 2457 hermetic passed)" string. DES-V-8 is `done` with its ACP half open for FEAT-007. Close the three rows with their real evidence and set DES-V-8 to `missing` with "FEAT-006 part done", like DES-V-9.
4. **F006-R3-4 (NIT) Stray tool text in ADR-0006**: the round-2 addendum ends item 17 with "Content was rephrased for compliance with licensing restrictions." Remove the sentence.
5. **F006-R3-5 (NIT) An interrupted `ossctl discover` request is dropped**: `claim_discover` consumes the request. If a stop, pause or drain ends that targeted cycle, `discover()` returns `[]` without re-queueing it or telling the owner. Re-queue the request, or note that it was dropped.

</details>

<details>
<summary>Details</summary>

### F006-R3-1 (MEDIUM): one GitHub read failure is a terminal FAILED

`runner/github_read.py` `_get` turns any `httpx.HTTPError` or status ≥ 400 into `GitHubReadError`. It never retries. The class defines a `transient` property, but no caller uses it. `discover.run` catches the error per target. `vet._facts` (`sc.gh.repo`, `file_text`) and the scout's `issue_text`, `_prs` and `not_relevant` do not. The error reaches `Scheduler.run_one`'s `except Exception`, which calls `fail(..., "internal:githubreaderror")`. For VETTING or SCOUTING that is a terminal FAILED.

DESIGN §20 specifies otherwise for the runner's GitHub reads:

```
| GitHub read (runner) | timeout / 5xx   | yes: 3 retries 2/8/30 s        | stage retry or failed, error_class=github_unavailable |
| GitHub read          | 403 rate limit  | yes: sleep until reset (≤ 1 h) | stage delayed |
```

The live DB shows the effect. `c_D1A3KD4FNB` (comet-ml/opik) and `c_D1A3KCMPGG` (openlit/openlit) are `FAILED internal:githubreaderror` at 2026-10-18T22:04:02Z. The runner journal has `GitHubReadError: GET /repos/comet-ml/opik: ConnectError` (`[Errno 111] Connection refused` from proxy 3140) in `vet._facts`, logged as `stage_crashed`. The proxy was briefly unavailable, during a redeploy or system test. A real rate limit during a discovery cycle that vets many candidates would fail them all the same way. Autonomous items return only after the 7-day cooldown (`rediscover.COOLDOWN_STATES`). A 👤 item from `/v1/work` stays failed until the owner sends it again.

Fix: retry network errors and 5xx at 2/8/30 s inside `_get`. For a 403 with `x-ratelimit-remaining: 0`, or a 429, raise a delay (for example `Deferred` with the reset time, which the scheduler already treats as "stage stopped"). After the retries, raise `StageFailed("github_unavailable")`. The stages then need no per-call handling. Test with a `fake_github` transport: connect refused, 502 then 200, 403 rate limit, final failure class.

### F006-R3-2 (MEDIUM): the backoff holds the item in whatever state comes next

The R2-1 fix stores `self.held[cid] = (count, next_try)` when a stage fails in a `NO_FAIL` state. It only clears the entry when a stage for that cid succeeds. `_dispatch` checks it before `stage_for` and never compares states:

```python
held = self.held.get(cid)
if held is not None and self.now() < held[1]:
    continue  # repeated stage errors in a gate-owned state: backing off
stage = stages.stage_for(c)
```

After a failed re-scout from `RESCOPE_REQUIRED`, the owner gets "It stays at RESCOPE_REQUIRED; retrying in 10 min, then less often". The natural reply is ✅ Continue at the measured class (`ProposalService.continue_rescope`, `RESCOPE_REQUIRED → PACKAGING`). The item is then in a runner state, but the package stage is skipped until the hold expires: 10 min after the first failure, up to 6 h after repeated ones. The owner is told nothing more. `NO_FAIL` also covers AWAITING_APPROVAL…SUBMIT_FAILED, where the message text ("revising the proposal failed") would be wrong.

`test_rescope_dispatch.py` covers the backoff and the single message, but not a state change during the backoff. Fix: store the state with the hold (`held[cid] = (state, count, until)`), skip only while the current state equals it, and drop the entry otherwise. Add the Continue case as a test.

### F006-R3-3 (MEDIUM): matrix rows

The matrix check for FEAT-006 lists 29 open rows. Every one also names another open FEAT, except three that are co-owned only with FEAT-005, which is closed:

- DES-FR-21: "FEAT-005 part done (item 22) … FEAT-006 part done", status `missing`.
- CR4-2d: "FEAT-005 part done … the estimate/actual comparison is FEAT-006; FEAT-006 part done", status `missing`.
- CHAT-32: "FEAT-005 part done … requiring tests is FEAT-006 (CR4-2j); FEAT-006 part done", status `missing`.

Nobody else will close them. The FEAT-006 evidence on these three, and on DES-FR-10 ("agents, prompts"), is the pasted "runner/{scheduler,pipeline,errors,stages/*}.py, ctl verify/discover/import; tests/unit/runner/** … 2457 hermetic passed" string. Round 2 removed that string from the VERIFY rows. The real evidence is `test_size_report.py` and `scoring` for DES-FR-21, the size report vs estimate for CR4-2d, `review.enforce_tests` plus `prompts/{coder,reviewer}.md` for CHAT-32, and `.kiro/agents/ossbot-*.json` plus `prompts/*.md` for DES-FR-10.

DES-V-8 goes the other way. Its phase is "P2 FEAT-006 + P2 FEAT-007" and its evidence says the V3 ACP half is checked in FEAT-007, but its status is `done`. The FEAT-007 matrix check lists only `missing`/`rework` rows, so it will not show the ACP half. DES-V-5b, DES-V-9 and DES-V-17 use "missing, FEAT-006 part done" for the same situation. The Totals line needs recounting after the change.

</details>

## What I verified and how

- Scope: `git diff aa6c06e..HEAD` (round-2 fixes, 15 commits). Earlier rounds covered `03e5d54..aa6c06e`. I read the round-2 review, FEAT-006 (status `completed`, findings incl. the round-2 handoff), the build plan (hard rules, items 28–32, matrix check, working rules), DESIGN §9.5a, §10.9 "Duration", §16 and §20, and the ADR-0005/0006 addenda. Code read: `runner/{scheduler,gk_client,main,pipeline,github_read}.py`, `runner/stages/{__init__,scout,fetch,package,watch,discover}.py`, `gatekeeper/{contributions,models_admin,proposals}.py`, `sandboxd/{jobs,probes}.py`, `ctl/cli.py` (targets verify), `sandbox/entrypoints/{fetch,kiro}.sh`, `systemd/ossbot-runner.service`, and the new and changed tests.
- Round-2 findings: R2-1 through R2-10 are addressed in code, tests and docs. R2-7 rests on the owner's decision A + C recorded in DESIGN §10.9, which the FEAT findings report as asked under hard rule 12. I did not see the owner exchange myself.
- Scope rules: scout proposes before any code work. The pre-Start job is still only the hardened depth-1 clone, and a rescope re-scout runs no repo job. The claim stays behind gate 1 and survives a rescope (`claim_requested` kept). 👤 items skip ranking, caps, size and filters but not the ceiling. The tests-required rule is in the coder and reviewer prompts and in `review.enforce_tests`. Kiro headless is `chat --v3` in `kiro.sh`. The runner does not import the gatekeeper (import-linter contract).
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e"` gave **2477 passed, 13 deselected** (230 s), matching the FEAT findings.
- Lint gate: `ruff check` all passed, `ruff format --check` 282 files formatted, `mypy` no issues in 146 files, `lint-imports` 5 contracts kept, 0 broken.
- Redeploy: `bootstrap.ps1 -Only provision` printed `unchanged` for all 15 steps (the deployed build equals HEAD). `/etc/ossbot/versions.lock.yaml` digests equal the committed ones.
- `ROOTDEV pytest -m "sandbox or e2e_docker"`: **13 passed, 0 skipped** (46 s). This includes the two former `needs_key` cases: `ossctl kiro smoke` on `roles.test` and the live probe status for tuple `654f751d194a`.
- System suites as root on the deployed build: `test_crash_safety.sh` passed all checks, including "stage run seeded in 'running' before the kill" and "the seeded mid-stage run is interrupted by the restarted runner". `test_lifecycle.sh` passed all checks. I did not re-run `test_config_write.sh`, `test_egress_surface.sh` or `test_firewall_failclosed.sh` this round. Round 2 ran them green, and VERIFICATION records the coder's green re-run after f29cb47. Afterwards runner, gatekeeper, sandboxd and proxy were `active`, and `/var/lib/ossbot/control` held only `boot-seen`, `egress-mode` and `last-shutdown`.
- Doctor (no probes): PASS 37, SKIP 3 (Hermes). It reports catalog 20 models, safety probes all passed for `654f751d194a`, `dry_run True`, and the write token as `public_repo` only. This matches VERIFICATION "Phase 2 live checks".
- No GitHub write: the live `outbound_actions` table is empty. Contributions are 25 SKIPPED and 2 FAILED (F006-R3-1), read with a read-only SQLite connection. I made no GitHub or Telegram call myself. `ossctl kiro smoke` ran once, inside the root suite, through product code.
- Hardening: the runner unit is unchanged and matches the non-root pattern. The `targets verify` transient unit now carries the same properties (`test_targets_verify_unit_has_the_runner_hardening`) and runs `ossbot-firewall --check` first.
- Secrets: gitleaks (`/usr/local/bin/gitleaks git --redact --log-opts=--all`) scanned 108 commits and found no leaks. I read no secret file. The FYI text and probe evidence carry no secret value.
- Git: all 45 commits since `03e5d54` have Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `git status --porcelain` was empty before this review. `main` is 15 commits ahead of `origin/main`, and `origin/main` has none that `main` lacks. Nothing was pushed or rewritten.

<details>
<summary>File map (round-3 commits)</summary>

- `src/ossbot/runner/scheduler.py`: `active_contributions` dispatch, `held` backoff for `NO_FAIL` failures, stop between targets.
- `src/ossbot/runner/stages/scout.py`: rescope re-scout from the build tree, volume kept on failure, build notes cleared.
- `src/ossbot/runner/stages/discover.py`, `runner/{main,pipeline}.py`: `stopping` hook, `_check` between targets.
- `src/ossbot/runner/gk_client.py`, `gatekeeper/{contributions,api/contributions}.py`: `active=true&offset=` paging.
- `src/ossbot/gatekeeper/models_admin.py`: one owner FYI per switch in the audit transaction.
- `src/ossbot/sandboxd/{jobs,probes}.py`: `PROBE_SESSION_TIMEOUT_S` = 30, `bounded`, `ProbeEvidence.timed_out`.
- `src/ossbot/ctl/cli.py`: `targets verify` firewall check and hardened transient unit.
- `sandbox/entrypoints/fetch.sh`, `config/versions.lock.yaml`: every fetch clears `/work/repo`, image digests.
- `tests/unit/runner/test_rescope_dispatch.py` (new), `tests/e2e/test_e2e_proposal.py`, `tests/unit/{gatekeeper/test_models_admin,sandboxd/test_probe_gate,ctl/test_cli_pipeline,runner/test_scout_clone}.py`, `tests/sandbox/{conftest,test_profiles}.py`, `tests/system/test_crash_safety.sh`: tests for the above.
- `docs/{DESIGN,TRACEABILITY,VERIFICATION}.md`, `docs/adr/{0005,0006}`, `README.md`, `config/models.yaml`, FEAT-006.json: decisions, rows, live checks.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff aa6c06e..0318424`.

</details>
