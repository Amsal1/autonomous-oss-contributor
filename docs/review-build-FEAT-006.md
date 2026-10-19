# FEAT-006 build review, round 4: pipeline backend (runner, gate 1, size budget, model switch)

Round 4 covers the five commits after round 3 (`dfbc994..d4e0a14`, 11 files, +408/−54). They answer all five round-3 findings. Runner GitHub reads now retry network errors and 5xx at 2/8/30 s and fail as `github_unavailable`. A rate limit delays the stage or the discovery cycle until the reset instead of failing it. The re-scout backoff is scoped to the state it was recorded in. The three FEAT-006-only rows are closed with real evidence, DES-V-8 is back to `missing`, and the stray ADR sentence is gone. A cut-short `ossctl discover` request now runs again. I re-ran the hermetic bar, the lint gate, gitleaks, the root suites and `test_crash_safety.sh`. All are green and match the FEAT findings.

One new defect comes from the retry fix itself. Discovery runs on the scheduler tick before dispatch, and it catches a read failure per target and moves on. Each failing target now costs 40 s of retries, so a GitHub API outage (or a proxy that stays down) holds the tick for about 48 min.

Watch for:
- **confirmed** (code; precondition seen live): during a GitHub API or proxy outage, one discovery cycle spends about 72 × 40 s before `_dispatch` runs. For that time no stage is started, including the owner's Start work or Continue. `ossctl targets verify` takes as long.
- **confirmed**: `origin/main` was advanced by a push at 2026-10-19 08:47 +07, inside the round-3 coder's window. The repo does not show who pushed.

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 0 HIGH, 1 MEDIUM, 1 NIT)

## High-level view

The §20 read policy now sits in `GitHubRead._get`. A rate-limited stage run closes as `cancelled`, not `interrupted`, so a rate limit does not use up the max-3 re-queue. The retry pause runs in whatever thread made the read. In a stage thread that costs nothing, but discovery runs on the tick thread.

Discovery is where the retries do harm. `discover.run` treats a failed read as "skip this target" and keeps going through about 72 targets. Before this round a refused connection failed at once: the round-2 journal at 06:03:04 shows the whole list failing with `ConnectError` within a few tens of milliseconds. Now each target waits 2 + 8 + 30 s first. The first retry absorbs a short proxy restart. A long outage stalls new dispatch for the length of the cycle.

The matrix check gives deferred 27, done 221, missing 260, rework 13 (521 rows), the same as the Totals line. Every open FEAT-006 row also names a later FEAT.

<details>
<summary>Issues (2)</summary>

1. **F006-R4-1 (MEDIUM) GitHub outage stalls the scheduler tick**: discovery runs synchronously before `_dispatch` and skips a failing target only after 40 s of retries, so an outage over about 72 targets blocks every new dispatch (Start work, Continue, watch) for about 48 min per cycle, and `ossctl targets verify` takes as long. End the cycle on `github_unavailable` (a `Deferred` with `discover_after`, keeping `pending_discover`), or add a short fail-fast breaker in `GitHubRead`. Add a multi-target test that bounds total sleep and checks dispatch in the same tick.
2. **F006-R4-2 (NIT) `origin/main` pushed during round 3**: the reflog shows `update by push` to 1981308 at 08:47 +07, between the round-3 fix commits and d4e0a14. The round-3 review's "nothing was pushed" missed it. Confirm that the pushes come from the owner or the parent session and not from a build step (hard rule 7).

</details>

<details>
<summary>Details</summary>

### F006-R4-1 (MEDIUM): a GitHub outage holds the scheduler tick for a whole discovery cycle

`Scheduler.tick` calls `self.discover()` synchronously and calls `_dispatch` only after it returns. Inside `discover.run`, every target starts with `sc.gh.repo(entry.repo)`, and a `GitHubReadError` is caught per target:

```python
        except GitHubReadError as exc:
            log.warning("discovery read failed for %s: %s", entry.repo, exc, extra={"event": "discover_read_failed"})
```

With 58f5742, `_get` sleeps 2, 8 and 30 s before it raises `github_unavailable`. `config/targets.yaml` has about 72 discovery-enabled targets, so a cycle during an outage takes about 48 min (more when the first read succeeds and a later one fails). For that time the tick does not return. No new stage is dispatched: no vet or scout for new items, no fetch after Start work, no package after Continue, no watch poll. Stages already running are not affected because they run in pool threads. The cycle then completes normally and sets `last_discovery`, so it happens once per `interval_hours` while the outage lasts. A stop, kill, pause or drain is still noticed between targets, at most 40 s late, which is inside `TimeoutStopSec`.

The precondition has already happened live. The round-2 runner journal at 2026-10-19 06:03:04 has `discover_read_failed … ConnectError` for every target within a few tens of milliseconds, during a proxy outage. A short restart is now covered by the first retry, as VERIFICATION records. An API-only GitHub incident (5xx while git over HTTPS still works) or a proxy that does not come back would hit the long path. `ossctl targets verify` (`verify_targets.verify`) loops over the same targets with the same reader, so it would also run for about 48 min instead of reporting FAIL rows quickly.

`test_github_read.py` covers retries on one read and a rate-limited discovery cycle, but not a cycle over several targets with an unavailable API.

Suggested fix: treat `github_unavailable` as the end of the cycle, not of the target. In `discover.run`, re-raise a `GitHubReadError` whose `error_class` is `github_unavailable` as a `Deferred`. `Scheduler.discover` then keeps `pending_discover` or clears `last_discovery` and sets `discover_after` (for example 15 min). `github_http_<status>` can stay per target. An alternative is a short breaker in `GitHubRead`: after one exhausted retry, reads fail at once for a few minutes. That also bounds `targets verify`. Test: three targets on a refusing transport, total sleeps ≤ 40 s, and an item in `PROPOSAL_APPROVED` dispatched in the same tick.

### F006-R4-2 (NIT): `origin/main` moved by push during round 3

`git reflog show origin/main` has `update by push` at 2026-10-19 08:47:10 +07 to 1981308. That is after the round-3 fix commits (07:09) and before the round-3 notes commit d4e0a14 (08:49). The same pattern runs through the whole build: at least 25 push updates since 2026-10-18 15:46, one of them 9 s after the round-2 handoff commit 52bca2a. The round-3 review said "nothing was pushed". It compared `main` with `origin/main` and did not check the reflog. The remote is the owner's own repository (`Amsal1/autonomous-oss-contributor`), so this is not a product GitHub write and no history was rewritten (`origin/main` is an ancestor of `main`). Hard rule 7 does forbid steps from pushing, though, and the FEAT findings do not mention a push. The coder or the parent session should confirm that the pushes come from the owner or the parent and not from a build step.

</details>

## What I verified and how

- Scope: `git diff dfbc994..HEAD` (round-3 fixes: 58f5742, 5217c3d, 1981308, d4e0a14). Earlier rounds covered `03e5d54..dfbc994`. I read the round-3 review and verdict, FEAT-006 (status `completed`, findings incl. the round-3 note), the build plan (hard rules, items 28–32, matrix check), DESIGN §20 (GitHub read rows) and the changed code: `runner/{github_read,scheduler,verify_targets,pipeline}.py`, `runner/stages/{discover,__init__}.py`, plus the new and changed tests.
- Round-3 findings: R3-1 (retries, `github_unavailable`, rate-limit delay, `cancelled` stage run) is fixed with tests. R3-2 (state-scoped `held`/`delayed`, wording for non-gate-1 states, Continue dispatched on the next tick) is fixed with tests. R3-3: the matrix check gives Totals deferred 27, done 221, missing 260, rework 13, 521 rows. DES-FR-21, CR4-2d and CHAT-32 are `done` with test names that exist. DES-V-8 is `missing` with "FEAT-006 part done". DES-FR-10 carries the agent and prompt evidence. Every open row naming FEAT-006 also names FEAT-007/008/009/010/011. R3-4: the sentence is gone from both ADRs. R3-5: `pending_discover` is kept and re-run, and `discover_dropped` is logged at shutdown.
- Scope rules from earlier rounds still hold for the changed code. The scout proposes before any code work and the claim stays behind gate 1. 👤 items skip ranking, caps, size and filters but not the ceiling. The tests-required rule stays in the prompts and in `review.enforce_tests`. Kiro headless stays `chat --v3`. The runner does not import the gatekeeper (import-linter).
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e"` gave **2493 passed, 13 deselected** (244 s), matching the FEAT findings.
- Lint gate (`.ossbot-local/lint.ps1`): ruff all passed, format 283 files, mypy no issues in 146 files, lint-imports 5 contracts kept.
- Deployed build: `/opt/ossbot/app/src/ossbot/runner/{github_read,scheduler}.py` are byte-identical to HEAD (`cmp`). Runner, gatekeeper, sandboxd and proxy are `active`. I did not redeploy, because the last commit is docs-only.
- `ROOTDEV pytest -m "sandbox or e2e_docker"`: **13 passed** (47 s). `ROOT bash tests/system/test_crash_safety.sh`: all checks passed, including the seeded mid-stage runner SIGKILL, re-queue and "no outbound action re-sent". I did not re-run `test_lifecycle.sh`, `test_config_write.sh`, `test_egress_surface.sh` or `test_firewall_failclosed.sh`. This round changed none of their inputs, and round 3 recorded them green.
- Doctor (no probes): PASS 37, SKIP 3 (Hermes). It reports the catalog at 20 models, the safety probes passed for `654f751d194a`, and `dry_run True`. VERIFICATION's round-3 row says "doctor PASS 33, SKIP 3" for the post-redeploy run. That count is likely from the provision step's doctor subset, and nothing depends on it.
- No GitHub write: the live DB (`/srv/ossbot/gatekeeper/ossbot.db`, opened `mode=ro`) has 0 `outbound_actions`, 31 SKIPPED and the 2 old `FAILED internal:githubreaderror` described in VERIFICATION. The runner journal for the last 3 h has no `github_read_retry`, `github_unavailable`, `github_rate_limited` or `stage_crashed` event. I made no GitHub or Telegram call myself.
- Secrets: gitleaks (`/usr/local/bin/gitleaks git --redact --log-opts=--all`) scanned 114 commits and found no leaks. I read no secret file.
- Hardening: no unit file changed this round. `tests/unit/test_systemd_units.py` passes in the bar.
- Git: all commits since `03e5d54` have Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `git status --porcelain` was empty before this review. `main` is 1 commit ahead of `origin/main` and 0 behind. See F006-R4-2 for the push.
- Commands this round followed the user's new rule (no inline PowerShell full of variables, subexpressions or long pipelines; multi-line work goes in scripts). The matrix check ran as `.ossbot-local/matrix-check.ps1` and the DB look as `.ossbot-local/r4-db.py` (both gitignored scratch). **Rule for the next coder rounds:** keep commands short and plain. Put anything multi-line in `.ossbot-local/` (scratch) or `scripts/dev/` (committed, reusable), and run it by reference (`wsl -d ossbot -u root -- bash /mnt/d/Work/autonomous-oss-contributor/<path>.sh`) or over stdin (`Get-Content <file> -Raw | wsl -d ossbot -u root -- bash -s`). Prefer the existing helpers (`scripts/*.ps1`, `ossctl`, `.ossbot-local/*.sh`).

<details>
<summary>File map (round-3 fix commits)</summary>

- `src/ossbot/runner/github_read.py`: retries 2/8/30 s, `GitHubReadError` as `StageFailed`, `GitHubRateLimited` as `Deferred`, `limited_until`.
- `src/ossbot/runner/scheduler.py`: state-scoped `held`, `delayed` for rate-limited stages, `discover_after`, `pending_discover`, `GATE_1` wording.
- `src/ossbot/runner/verify_targets.py`: a rate limit is a FAIL row.
- `tests/unit/runner/test_github_read.py` (new), `test_rescope_dispatch.py`, `conftest.py`: tests for the above.
- `docs/TRACEABILITY.md`, `docs/VERIFICATION.md`, `docs/adr/{0005,0006}`, FEAT-006.json: rows, live checks, addendum cleanup, findings.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff dfbc994..d4e0a14`.

</details>
