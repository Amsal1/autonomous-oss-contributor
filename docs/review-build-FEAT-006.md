# FEAT-006 build review, round 5: pipeline backend (runner, gate 1, size budget, model switch)

Round 5 covers the commits after the round-4 review (`711ceaa..79eb525`: 72bb704 code, 79eb525 notes; 742f078 is the parent's plan rule 6). They fix F006-R4-1. A read that fails as `github_unavailable` after its 2/8/30 s retries now ends the discovery cycle, not just its target. The scheduler dispatches stages in the same tick and runs the cycle again after 15 min. `ossctl targets verify` marks the remaining repos FAIL `not checked`. `github_http_<status>` still skips only its own target, and DESIGN §19 has the matching row. The hermetic bar, the lint gate, the root suites, doctor and gitleaks are green and match the FEAT findings and VERIFICATION. Nothing was pushed in the round-4 coder window.

Watch for:
- **confirmed** (NIT): a discovery cycle cut short by `github_unavailable` or a rate limit re-runs with a fresh `proposals_per_cycle` budget and an empty refusal cache. An outage that keeps coming and going can therefore create more DISCOVERED items per `interval_hours` than the budget allows. `proposal.max_per_day` still caps the cards the owner sees.

**Verdict**: APPROVED (0 HIGH, 0 MEDIUM, 1 NIT)

## High-level view

The first `github_unavailable` in either discovery loop (target reads or the open-PR search) raises `discover.GitHubUnavailable`, a `Deferred`. `Scheduler.discover` handles it the same way as the round-3 rate-limit case. It keeps a requested `ossctl discover` in `pending_discover`, or clears `last_discovery` for a full cycle, and it sets `discover_after` 15 min ahead. During an outage the tick is held for about 40 s every 15 min instead of about 48 min per cycle. The new tests run over the real target list on a refusing transport, and a `PROPOSAL_APPROVED` item is dispatched in the same tick.

Each cut-short cycle starts from scratch. Items created before the cut come back as 409 and are skipped. Candidates ranked below them can still be created on top, under a new budget.

<details>
<summary>Issues (1)</summary>

1. **F006-R5-1 (NIT) Cut-short discovery cycles reset the per-cycle budget**: after `github_unavailable` or a rate limit, the cycle re-runs 15 min later (or after the reset) with a new `proposals_per_cycle` budget and an empty refusal cache, so an outage that comes and goes can create more than the budget per `interval_hours`. Count what the cut cycle already created against the re-run (for example, carry `created` and `refused` into it), or document that the budget is per attempt. Test: a cycle that creates one item and then hits `github_unavailable` creates at most `budget - 1` on the re-run.

</details>

<details>
<summary>Details</summary>

### F006-R5-1 (NIT): the per-cycle budget restarts after a cut-short cycle

`Scheduler.discover(None)` sets `last_discovery` and clears `self.refused` at the start of every full cycle. When the cycle ends with `GitHubUnavailable` (new in 72bb704) or `GitHubRateLimited` (round 3), `last_discovery` goes back to `None`, so the next attempt is a new full cycle. It clears the refusal cache again and starts with `created = []` against `bundle.policy.discovery.proposals_per_cycle`. Suppose the cut comes in the second loop (`open_prs_mentioning`), after `sc.gk.create(...)` has already succeeded for some candidates. The re-run gets those back as 409 and skips them, but it can still create up to the full budget from the candidates ranked below. An outage that comes and goes over one `interval_hours` window can produce several budgets' worth of DISCOVERED items. DESIGN §9.5b defines pacing as "`discovery.proposals_per_cycle` (default 3) cards per discovery cycle and `proposal.max_per_day` (default 5)". The owner-facing limit still holds, because the scout holds autonomous first proposals at `max_per_day` (`scout_hold_until`). The cost is extra vet reads and more items in the queue, not extra cards, so this is a NIT.

</details>

## What I verified and how

- Scope: `git diff 711ceaa..79eb525` (72bb704 code and tests, 79eb525 FEAT findings and the VERIFICATION row, plus the review files committed in 711ceaa). 742f078 only adds build-plan hard rule 6 (shell command style). Earlier rounds reviewed `03e5d54..711ceaa`. I read the round-4 review and verdict, the FEAT-006 findings (round-4 note), context.json, the DESIGN §19 row and §9.5b pacing, and the changed files: `runner/{scheduler,github_read,verify_targets}.py`, `runner/stages/discover.py` and `tests/unit/runner/test_github_read.py`.
- F006-R4-1: both `except GitHubReadError` branches in `discover.run` re-raise only `github_unavailable`. `tick` skips discovery until `discover_after` and still runs `_dispatch`. `verify_targets.verify` stops reading after the first unavailable row. The four new tests check these points: total sleep ≤ 40 s, one repo read, dispatch in the same tick, no read at +5 min and a cycle after 15 min, the requested discover kept after a 502, a 403 staying per target, and `targets verify` in both cases. No outage was induced live, and VERIFICATION says so.
- The scope rules from earlier rounds hold for the changed code. The scout proposes before any code work and the claim comes after gate 1. 👤 items skip ranking, caps, size and filters, but not the ceiling. Tests are required when the repo has a suite. Kiro headless runs `chat --v3`. The runner does not import the gatekeeper.
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e"` gave **2497 passed, 13 deselected** (234 s), matching the FEAT findings and VERIFICATION.
- Lint gate (`.ossbot-local/lint.ps1`): ruff all passed, 283 files formatted, mypy no issues in 146 files, lint-imports 5 contracts kept (including "Untrusted-side components never import the gatekeeper").
- `ROOTDEV pytest -m "sandbox or e2e_docker"`: **13 passed** (49 s). I did not re-run the system suites. This round changed none of their inputs, and the coder's crash_safety run shows in the runner journal as the SIGKILL at 09:12:16.
- Deployed build (`.ossbot-local/r5-root.sh`, read-only): the deployed `runner/{stages/discover,verify_targets,scheduler,github_read}.py` are byte-identical to HEAD. Runner, gatekeeper, sandboxd and proxy are `active`. Since 09:09 the runner journal has no `github_read_retry`, `github_unavailable`, `github_rate_limited`, `stage_crashed` or `discover_interrupted` event, and its only warning lines are the crash_safety SIGKILL.
- Runner hardening (`systemctl cat ossbot-runner`): User/Group `ossrunner`; Requires boot-gate and both firewall units; `ExecStartPre=+…ossbot-firewall --check`; `ConditionPathExists=!…/killed`; `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`; the io_uring `SystemCallFilter` with `SystemCallErrorNumber=EPERM`; `SystemCallArchitectures=native`; `ProtectHome=yes`; `ProtectSystem=strict`; `NoNewPrivileges=yes`; empty `CapabilityBoundingSet`; `TimeoutStopSec=150`.
- Doctor (no probes): PASS 37, SKIP 3 (Hermes, FEAT-007). The catalog has 20 models, the safety probes passed for `654f751d194a`, and the gatekeeper reports `dry_run True`. `config/policy.yaml` has `dry_run_default: true`.
- No GitHub write: the live DB (opened `mode=ro`) has 0 `outbound_actions`, 34 SKIPPED and the 2 old `FAILED internal:githubreaderror` recorded in round 3. I made no GitHub or Telegram call and read no secret file.
- Secrets: gitleaks (`git --redact --log-opts=--all`) scanned 117 commits and found no leaks.
- TRACEABILITY: unchanged since round 4 (Totals deferred 27, done 221, missing 260, rework 13, 521 rows). Every open row naming FEAT-006 also names a later FEAT.
- Git: 742f078, 711ceaa, 72bb704 and 79eb525 have Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `git status --porcelain` was empty before this review. The last `origin/main` update in the reflog is d4e0a14 at 08:51:02 +07, 6 s before the parent's 742f078. Nothing moved after that, so no push happened in the round-4 coder window (09:01–09:13), and F006-R4-2 needs no further step action.
- Commands followed build-plan rule 6: the root checks ran by reference as a script (`.ossbot-local/r5-root.sh`, gitignored scratch).

<details>
<summary>File map (round-4 fix commits)</summary>

- `src/ossbot/runner/stages/discover.py`: `GitHubUnavailable(Deferred)`, `_end_if_unavailable` in both read loops.
- `src/ossbot/runner/scheduler.py`: `DISCOVER_UNAVAILABLE_RETRY` (15 min) sets `discover_after`.
- `src/ossbot/runner/github_read.py`: `UNAVAILABLE` constant.
- `src/ossbot/runner/verify_targets.py`: the remaining repos FAIL `not checked` after the first unavailable read.
- `tests/unit/runner/test_github_read.py`: four tests for the above.
- `docs/DESIGN.md` (§19 row), `docs/VERIFICATION.md` (round-4 row), FEAT-006.json findings.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 711ceaa..79eb525`.

</details>
