# FEAT-005 build review, round 2: Phase 1 rework to DESIGN revision 5

Round 2 reviews `6d1297a..32e0c9b`: the item 26 commit (`c68ed4a`), the round 1 fixes (`44f57a6`), the TGRW-15 comment fix (`3840c07`) and the item 27 close-out (`32e0c9b`). Round 1 already reviewed items 22 to 25 against the full `35438c1..2b29171` diff and found no scope cut there; this round re-checked them through the suites only. Both HIGH findings of round 1 are closed. Item 26 is committed and verified live, the deployment matches the repo, a redeploy prints only `unchanged`, and the matrix has no `rework` row and no open FEAT-005-only row. The round 1 NITs F005-R1-3, -5 and -7 are fixed with tests. Every suite I re-ran matches the recorded evidence.

Watch for: `origin/main` was pushed again at 22:39:15, while only this reviewer step was running, and the reviewer did not push. That confirms an actor outside the build steps (confirmed push, owner to identify). The P1-17 linger check covers four uids, not the five the plan names (confirmed, NIT). Two design-level NITs from round 1 are carried to the Phase 2 gate and FEAT-007.

**Verdict**: APPROVED (0 HIGH, 0 MEDIUM, 4 NIT)

## High-level view

Item 26 fills the remaining safety gaps. `hyperv-firewall.ps1` and `scripts\ossctl.ps1 doctor` now share one rule spec in `scripts/lib/common.ps1`. The script re-creates a missing, duplicated or drifted ossbot rule. Doctor FAILs on drift or on a foreign outbound Allow for the WSL VM, and WARNs on a foreign inbound Allow. Every non-root unit must carry `ProtectHome=yes|tmpfs`. Doctor and the surface test refuse lingering or a `/run/user/<uid>` for the four unit uids. A 90 s root dead-man job now guards the io_uring window in the surface test. The no-exposure guard has a live half (`test_no_exposure.sh`: listeners, container port bindings, the proxy netns, tunnel binaries, polling) and a static half (`test_no_tunnels.py`).

The round 1 fixes tighten two fail-open spots. Card recovery now runs last in housekeeping, in its own guard, so a card that fails to render cannot stop the expiry sweep. The drift check still tolerates a conf whose snapshot the installed schema rejects, but only while that snapshot is current or for 60 s after a newer one appears. Past that it reports drift, and doctor WARNs (`squid.conf verifiable`) for as long as the conf cannot be verified.

The close-out redeployed items 23a to 25 and migration 0002, re-ran the whole Phase 1 block with the new tests, and updated the matrix. The 24 open rows that name FEAT-005 all name a later FEAT and record their FEAT-005 part.

<details>
<summary>Issues (4)</summary>

1. **`origin/main` pushed by an actor outside the build steps** (NIT, owner) — the reflog has `update by push` to `32e0c9b` at 22:39:15, during this reviewer step, which did not push. Repo hooks and scripts contain no push. The owner should find and stop the auto-push (IDE sync, orchestrator, scheduled task). History must stay as it is.
2. **P1-17 skips the fifth uid entirely** (NIT) — the plan names five service uids. ossnobody is dropped from both checks, though only its `/run/user/1001` is expected by design (live `Linger=no` today). Keep the `/run/user` exemption and also assert `Linger=no` for ossnobody in doctor and the surface test.
3. **Draft hold-back with an unbounded maximum width** (NIT, carried F005-R1-4) — an `extra_patterns` entry like `x[a-z]{1,1000}y` can show a prefix in a draft. Raise it at the Phase 2 gate as a DESIGN change (refuse max width ≥ `HOLD_BACK`), and ask the owner first (hard rule 12).
4. **Drift check reads the login-window state from the conf under test** (NIT, carried F005-R1-6) — `hermes_login` comes from the live conf. Compare it with the real window state when FEAT-007 item 35/36 adds the IdC login window.

</details>

<details>
<summary>Details</summary>

### Pushes to `origin/main`

At the start of this review, `origin/main` pointed at `2b29171`, as in round 1. The reflog now has a sixth `update by push` entry: `32e0c9b` at 22:39:15 +07. That is six minutes after the close-out commit (22:33:01) and while this reviewer was running `bootstrap -Only provision` and a log grep. Neither one pushes. `git config` has no alias or hook path, `.git/hooks` holds only samples, and the only `push` strings outside `docs/` are `github_writer.push_argv` and a test against a local fake fork. That supports the coder's statement that it did not push and places the pushes outside the workflow steps (confirmed push; actor not identified). gitleaks finds nothing in the 61 commits. The finding is environmental and does not block FEAT-005, but hard rule 7 says nothing is pushed, so the owner should stop it before the next FEAT.

### P1-17 uid set

`check_no_user_managers` and the surface loop use `UNIT_UIDS = (ossrunner, hermes, gatekeeper, ossbox)`. WSL logs in as ossnobody by default, so its `/run/user/1001` is expected. The linger check does not need that exemption, though. A lingering default-login user would keep a user manager running after the last WSL session ends, outside every unit filter. This is a small, documented narrowing of the plan's "five service uids" (confirmed, NIT).

### Rejected-snapshot window

The tolerance has no time bound while the rejected snapshot is still `current`, which means the gatekeeper has not restarted since the deploy. Doctor's WARN is the only signal in that state. The 60 s branch (a newer snapshot is current and the proxy has not re-rendered) has unit coverage only. No schema-breaking redeploy exercised it live in this round, because 23a to 25 added only defaulted fields. If the proxy misses its restart after such a deploy, the check now reports drift, and `--check` then blocks unit starts until `ossbot-proxy` restarts (fail closed, by design).

</details>

## What was verified and how

- Scope. Round 2 diff against items 26 and 27 of the build plan and the FEAT-005 steps. P1-16: shared rule spec, rule properties checked (direction, action, enabled, protocol, port, VMCreatorId), duplicate and drift re-create, foreign Allow reporting. P1-17: `ProtectHome` asserted for every non-root unit (`test_non_root_units_protect_home`), doctor `service uids: no user manager` with `test_doctor_users.py`, and surface linger and `/run/user` checks. P1-19: the dead-man job is armed before the sysctl drop, the script stops (`finish`) if arming fails, and the job is cancelled after the restore and in the EXIT trap. No-exposure: `test_no_exposure.sh` and `test_no_tunnels.py` as listed. VERIFICATION threat-table row 41 is updated. TGRW-15 comments now point at current items. The round 1 fixes come with `test_a_failing_card_recover_does_not_stop_the_expiry_sweep`, `test_squid_drift_bounds_the_rejected_snapshot_window` and `test_kill_during_a_slow_preflight_waits_at_most_the_preflight_bound`. FEAT-005 criteria (ceiling, github_user_content, default `open` with invariants, tier default `self_improve` failing closed to `locked`, deltas 1-9, `/kill` ≤ 2 s, redaction, P1-4, P1-15..19, no exposure) all have code and tests. Items 22 to 25 were reviewed in round 1.
- Hermetic and lint (Windows PowerShell, `$env:WSL_UTF8=1`, DEV prefix): `pytest -m "unit or telegram or acp or e2e"` → 1963 passed, 11 deselected (matches VERIFICATION). `ruff check src tests` all passed. `ruff format --check`: 211 files already formatted. `mypy`: no issues in 110 files. `lint-imports`: 5 contracts kept, including the new chat contract. Its one ignored import is the Phase 1 submit→github_writer ignore.
- Deployment: `diff -rq -x __pycache__ src /opt/ossbot/app/src` is empty. `bootstrap.ps1 -Only provision` printed every step `unchanged` (kiro/hermes `skipped (phase 2)`). Its doctor showed PASS 31, WARN 2, SKIP 3, with `service uids: no user manager`, `squid.conf verifiable`, `egress mode: open (default open)`, `hermes tier: self_improve` and `gatekeeper health: dry_run True`.
- Root and Windows suites (re-run): `ROOTDEV pytest -m sandbox` → 9 passed, 2 skipped (`needs_key`). `ROOT bash tests/system/test_no_exposure.sh` → all checks passed; the live `ss -ltnupe` shows only 10.255.255.254:53 (WSL resolver) and chrony on loopback. `ROOT bash tests/system/test_egress_surface.sh` → exit 0, 225 PASS. That run covers allowlist/research/open applied with `ossbot-firewall --check` passing in each, hermes and research reaching github.io and gists in every mode, runner and gatekeeper denied, the llm pool getting web_public only in open, telegra.ph denied everywhere, the dead-man job armed and cancelled, and the linger and `/run/user` checks. Afterwards the mode was back at `open`, `io_uring_disabled` at 2, the watchdog timer active and no dead-man timer left. `ROOT /opt/ossbot/bin/ossbot-firewall --check` → 0. `scripts\ossctl.ps1 doctor` → exit 0, `hyper-v firewall` PASS with exactly the ossbot rules, `.wslconfig` and localhost-forwarding PASS. I did not re-run `test_kill.ps1`, `test-isolation.ps1` or the other ten system scripts: their recorded counts are consistent with every suite I did re-run.
- Matrix (build-plan check over 508 rows): 80 rows name FEAT-005, 56 done and 24 missing, and every missing row names a later FEAT with "FEAT-005 part done (item N)" evidence. No row naming FEAT-005 is `rework`, no FEAT-005-only row is open, and every done row has evidence. The 16 `rework` rows left name only later FEATs. Totals line: deferred 26, done 164, missing 302, rework 16.
- Safety: dry-run ON (doctor). No GitHub write path changed in round 2. Telegram is untouched in round 2, and FYIs still go through the outbox to the owner. No tunnel, webhook or listener (both guards). `gitleaks git --redact --log-opts=--all` → 61 commits, no leaks. No secret file was read. I1-I13 hold in the round 2 changes, and both changed tolerances now fail closed after their bound.
- Git: all ten commits since `35438c1` are by Amsal Khan <md.amsalkhan@gmail.com> (author and committer) and follow Conventional Commits. `git status --porcelain` was empty before this review wrote its files. The push to `origin/main` is issue 1.
