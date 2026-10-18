# FEAT-005 build review, round 1: Phase 1 rework to DESIGN revision 5

Reviewed `35438c1..2b29171` (five commits, items 22, 23, 23a, 24, 25) plus the uncommitted item 26 work in the tree. Items 22 to 25 are implemented as DESIGN revision 5 and the build plan specify, and they are well tested. The hermetic bar (1960 passed) and the lint gate are green, and gitleaks finds nothing in the history. The FEAT is not finished, though. The coder ran out of context partway through item 26: the P1-16/P1-17/P1-19 and no-exposure work sits uncommitted and unverified. Item 27 (close-out) has not started, and the services still run the item 23 deployment, so the token core, decision services, tier API and handler budget have never run live.

Watch for: the dirty tree and unverified item 26 work (confirmed); the stale deployment, with migration `0002_decisions.sql` not yet applied to the live DB (confirmed); 9 FEAT-005-only rows still open and 5 rows still `rework` (confirmed); `origin/main` was pushed during the loop, by an actor that could not be identified (confirmed push, unattributed).

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 2 HIGH, 0 MEDIUM, 6 NIT)

## High-level view

The diff ceiling is now a config-only runaway guard of 80 files, 3000 code lines and 3000 test lines. `common/size.py` splits numstat with `test_globs` and drops `exclude_globs` from the line totals only, so every path still counts toward the file limit (B5). The per-target cap is gone, and a test enumerates token purposes, commands, confirm handlers and API routes to show that none can write `policy.diff`.

Egress modes are data with a closed grantee set (`research`, `hermes`, `agent-llm`, `catalog`). Each render first renders all three modes and refuses if any §11.5 invariant differs between them. GitHub Pages and gists reach only `research` and Hermes, in every mode. The default is `open`, and the flag fails closed to `allowlist`. A root path unit re-renders the proxy config and restarts the proxy. `ossbot-firewall --check` and the watchdog now compare the live `squid.conf` with the render for the current mode. Two tolerance windows were added to that comparison, and both are fail-open by design (NITs 5 and 6).

The Hermes tier control plane (default `self_improve`, unusable flag reads as `locked`, per-tier toolsets, operator API, `ossctl hermes tier`) invalidates open skill approvals in the same transaction as the switch. Applying a tier to Hermes is left to item 36, as planned.

The token core gains `DecisionServices` (`a_`/`p_`/`s_` dispatch by id prefix). It also gains a shared two-tap `TableDecisions` core for proposals and skills, with fail-closed content sources until FEAT-006/007, kill and reload fan-out to all three services, `disarm_expired`, re-sends that keep the expiry, atomic `mark_sent` binding, and `CardPublisher.recover`. Housekeeping now runs card recovery ahead of the expiry sweep, with no guard around it (NIT 3).

The handler budget spawns all long work and bounds the Confirm preflight to 8 s, which D14 allows. `/kill` and `/pause` cancel the cancellable kinds after their local effect. One `SecretScanner` per snapshot feeds `check_text(secret_values=)`, the patch scan and the `redact`/`draft_visible` choke point. Edited title and body text is refused before anything is stored (P1-4).

Item 26 exists only as WIP: Hyper-V rule drift handling, the `ProtectHome` and linger/`/run/user` checks, the io_uring dead-man job, `test_no_exposure.sh` and `test_no_tunnels.py`. It looks sound on reading, but it has not been deployed, run as root, recorded or committed. Item 27 has not started.

<details>
<summary>Issues (8)</summary>

1. **Item 26 WIP uncommitted and unverified** (HIGH): finish item 26, run its unit tests, `REDEPLOY`, `ROOT bash tests/system/test_no_exposure.sh`, `ROOT bash tests/system/test_egress_surface.sh` (now with the dead-man job), `shellcheck` on both scripts and `scripts\ossctl.ps1 doctor`. Then update VERIFICATION threat-table row 41, close DES-FR-22, DES-AC-42, CHAT-33, CR3-CF-1, CR3-S4, P1-16, P1-17, P1-19 and ADR-0013 with evidence, and commit by name.
2. **Item 27 close-out not done; stale deployment** (HIGH): redeploy twice (second run only `unchanged`) so that 23a/24/25 and migration 0002 go live, then run the full Phase 1 verification block. Fix the pre-consolidation FEAT comments (TGRW-15), record CHAT-37's FEAT-005 part, update Totals, set FEAT-005 `completed` with findings, add the missing run evidence for 23a/25 and leave `git status` clean.
3. **Housekeeping coupling to card recovery** (NIT): wrap `ctx.card_recover()` in its own try/except, or run it after `sweep_expired`/`prune_retention`, so that one bad row cannot stop the expiry sweep on every tick.
4. **Draft hold-back assumes bounded patterns** (NIT): BA-16 refuses patterns by *minimum* width. An `extra_patterns` entry whose maximum width is 256 or more (e.g. `x[a-z]{1,1000}y`) passes but can show a prefix in a draft. Raise this at the Phase 2 gate as a DESIGN note, or also refuse max width ≥ `HOLD_BACK` (a design change, so ask first).
5. **Drift check skips a snapshot the schema rejects** (NIT): `squid_drift` returns "no drift" for as long as the live conf names a snapshot the installed schema rejects. Bound that window (e.g. only while that snapshot is not current), or surface it as a WARN in doctor.
6. **Drift check trusts the conf's own login state** (NIT): `hermes_login` is read from the live conf, so a conf with the IdC login window left open is never drift. Compare it against the real window state when FEAT-007 adds the window.
7. **`/kill` during a slow preflight untested** (NIT): add a test that a `/kill` arriving while a Confirm preflight is running is answered within `PREFLIGHT_TIMEOUT_S` plus the budget (D14), as the item 25 test list asks.
8. **Unattributed push of `origin/main`** (NIT; owner to confirm): the reflog shows `update by push` to `b24f4e6` (21:02:45) and `2b29171` (21:29:39), each about 12 s after the coder's commit. History cannot be rewritten. The next coder should state in its findings that it did not push, and the owner should check for an auto-sync.

</details>

<details>
<summary>Details</summary>

### Unfinished item 26 and item 27

`git status` shows edits to `scripts/hyperv-firewall.ps1`, `scripts/lib/common.ps1`, `scripts/ossctl.ps1`, `src/ossbot/ctl/doctor.py`, `tests/system/test_egress_surface.sh`, `tests/unit/test_systemd_units.py` and FEAT-005.json. Three files are new: `tests/system/test_no_exposure.sh`, `tests/unit/ctl/test_doctor_users.py` and `tests/unit/test_no_tunnels.py`. On reading, the work matches item 26. The Hyper-V script re-creates a drifted or duplicated ossbot rule and reports foreign Allow rules. Doctor FAILs on drift and on foreign outbound Allows. The dead-man job is armed before the sysctl drop, and the script stops (`finish`) if arming fails. `test_no_exposure.sh` parses `ss -ltnupe`, checks that no container has `PortBindings`, checks the proxy netns, and confirms that no tunnel tool is installed and the bot uses long polling only. The hermetic run (1960 passed) includes these unit tests and they pass. None of the root or Windows checks have been run, nothing is recorded, and the tree is dirty, so item 26 cannot be counted as done (confirmed).

`diff -rq` of `src/` against `/opt/ossbot/app/src` shows the deployment stopped at item 23. `chat/`, `decisions.py`, `proposal_approvals.py`, `skill_approvals.py`, `secret_scan.py`, `telegram/ui/` and migration `0002_decisions.sql` are missing there (confirmed). So the live gatekeeper has never run the tier API, the decision services or the new handlers. Doctor's new `egress mode`/`hermes tier` checks would FAIL against it on `/v1/control/hermes-tier`. The FEAT findings stop at item 24. VERIFICATION records a 1873-pass run for item 24 but no run for 23a or 25.

On the matrix, the FEAT-005 rows stand at 47 done, 28 missing and 5 rework. Nine rows that name only FEAT-005 are still open: DES-FR-22, DES-AC-42, CHAT-33, CR3-CF-1, CR3-S4, P1-16, P1-17, P1-19 and ADR-0013 (`rework`). TGRW-15 is still `rework`, and `telegram/keyboards.py` still says "FEAT-005 adds the decorated rows". The shared rows of items 22 to 25 do record their FEAT-005 part in the evidence column (checked: CR3-U14, CR3-AC-59, CR3-BA-15, TGRW-5).

### Card recovery inside housekeeping

```
housekeeping_once: reconcile -> expire_due/disarm_expired x3 -> card_recover -> sweep_expired -> prune_retention
```

`CardPublisher.recover` calls `_card_sent`, which renders the card and does not catch errors (`_maybe_arm` catches only `ApprovalError`). An exception there is caught by the loop's generic handler. The tick ends before `sweep_expired`, and every later tick does the same while the bad row exists, so `AWAITING_APPROVAL` contributions never expire (likely; needs a row whose render raises). The old order put the sweep right after `expire_due`.

### Draft hold-back and pattern width

`draft_visible` redacts the whole text, then cuts at `max(last whitespace, len − 256)`. That hides a partial match only if it starts after the last whitespace, or if the pattern would already have matched within 256 characters. The default patterns (`{16,}`, `{36,}`, …) close early, so they are safe. A custom pattern that is whitespace-free, has a small minimum width and a large maximum (`x[a-z]{1,1000}y`) passes both BA-16 refusals, and its first `len − 256` characters show in a draft before the closing `y` arrives (confirmed by reading). The code follows the spec, which says "minimum match width", so this is a design-level NIT.

### Drift-check tolerances

`squid_drift` returns no problems when `parse_bundle` rejects the snapshot named in the live conf. That fixes the redeploy incident recorded in VERIFICATION, but the window has no end until the proxy restarts on a new snapshot. It also passes `hermes_login="\nacl cat_kiro_idc_login " in text`, so the expected render takes its login state from the file under test. Both are fail-open in a check that guards egress. Neither can be exploited today: the login window does not exist yet, and snapshots are hash-verified and written by the gatekeeper. Both should be tightened before FEAT-007 (confirmed).

</details>

## What was verified and how

- Scope against the build plan, item by item. Item 22: ceiling defaults, `SizePolicy`/`DiscoveryFilters`/`ProposalPolicy`, the class-within-ceiling validator, `check_diff(entries, policy)` with three violation codes, `size_class` replacing the per-target cap, and `test_ceiling_not_writable.py`. Item 23: `github_user_content` on `research`/`hermes` only, `mode_default: open`, the Literal grantee keys, per-context and cross-mode renderer assertions, the always-denied additions (`::/128`, Telegraph hosts) with why-comments, the flag that fails closed to `allowlist`, the operator API and `ossctl egress`, the path and apply units, the `--check` and watchdog drift, `/status` and doctor. Item 23a: `HermesPolicy`, `read_hermes_tier` (fails closed to `locked`), `hermes_surface.py`, the operator API and `ossctl hermes tier`, the doctor line, `tier_changed` invalidation in the same transaction, and the import-linter contract. Item 24: deltas 1 to 9 as listed, migration 0002 per §14.20.2, the fan-out at the four kill sites and both reload branches. Item 25: spawn/cancel/session map, `KILL_CANCELS`, the 8 s preflight bound (D14), `effect: local|io`, `SecretScanner` load refusals, `check_text(secret_values=)` at every caller, the patch value scan, `redact`/`scan`/`draft_visible`, and P1-4. No silent scope cut was found in items 22 to 25.
- Tests (Windows PowerShell, `$env:WSL_UTF8=1`, DEV prefix, working tree including the WIP): `pytest -m "unit or telegram or acp or e2e"` → 1960 passed, 11 deselected. `ruff check src tests` clean. `ruff format --check src tests`: 211 files formatted. `mypy`: no issues in 110 files. `lint-imports`: 5 contracts kept. Every test file named in the acceptance criteria exists.
- Live, read-only: `ROOT /opt/ossbot/bin/ossbot-firewall --check` exit 0 in mode `open` (flag `{"mode":"open","actor":"uid:0/operator"}`). Deployed-vs-repo `diff -rq` shows the item 23 state. Root suites (`sandbox`, system tests, `test_kill.ps1`, doctor) were not re-run in this round: the deployed code is not the code under review, and those runs belong to the item 27 close-out.
- Safety: dry-run was not touched. No GitHub write path changed except packaging's extra refusals. Telegram stays owner-only (FYIs go through `notify`). `gitleaks git --log-opts=--all`: 56 commits, no leaks. `gitleaks dir tests/unit` hits only `__pycache__/*.pyc` (runtime-built fake tokens) and the Phase 1 `test_keyscan.py` fixture. No tunnel, webhook or listener was added; `test_no_tunnels.py` passes. I1 to I13 hold in the reviewed code: fetch/validate/runner/gatekeeper egress cannot widen, every flag fails closed, and the two-tap, single-use, message-bound tokens are unchanged.
- Git: all five commits are by `Amsal Khan <md.amsalkhan@gmail.com>` (author and committer) and follow Conventional Commits. `origin/main` points at HEAD after two pushes during the loop (issue 8). The tree is dirty with the item 26 WIP (issue 1).
