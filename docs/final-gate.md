# Phase 2 gate review (round 1)

Reviewed at 6391fae (main; local main contains origin/main 7d6d7e3 plus 557b9d1 and 6391fae, which the owner pushes himself). No previous `docs/final-gate.json`, so there were no prior findings to re-check. Verdict: APPROVED, no HIGH or MEDIUM blockers.

## Evidence checked

The evidence is the VERIFICATION.md "Phase 2 gate (round 1)" table. Its logs under `.ossbot-local/logs/gate-*.log` exist with timestamps between 20:46 and 21:11 on 2026-10-20, and the tails match the table.

- Hermetic suite (43dd6fb): 3529 passed, EXIT 0. Root sandbox/e2e_docker (43dd6fb): 19 passed, exit 0.
- Code changed after 43dd6fb: be47ec4, 2d9b444, 6f10c86, 7d6d7e3 (ctl backup/cli/doctor/lint_personal/render, runner scheduler/discover) and 557b9d1 (PowerShell only). The lint gate and the affected tests re-ran on the 7d6d7e3 tree: `tests/unit/ctl/test_{lint_personal,doctor_users,render,backup,restore}.py`, all of `tests/unit/runner`, and `test_ci_workflow.py`, 257 passed. I grepped `tests/**` for other callers of `discover.run`, `squid_drift` and `lint_personal`. None exist outside those files, and the new `created` and `login_window_flag` parameters have backward-compatible defaults. That makes the targeted re-run sufficient, in line with the owner's instruction not to re-run full suites after small fixes.
- System tests (7d6d7e3): egress surface PASS 241, crash safety PASS 20, lifecycle PASS 47, no-exposure PASS 20. Kill: case 6 found a real defect. A task disabled by `kill.ps1 -Hard` reports `Running` while the blocker runs, so `resume.ps1` never re-enabled autostart. The fix is 557b9d1 (`Test-OssTaskDisabled` also checks Settings.Enabled). That condition is a superset of the old `State -eq 'Disabled'` check, so cases 1-5 are unaffected. Case 6 re-ran green on the 557b9d1 tree.
- Doctor `--probes` and the Windows doctor (557b9d1): PASS 46 and PASS 45, no FAIL, WARN or SKIP. gitleaks: 209 commits, no leaks found (log tail confirms). Personal lint: 0 hits.
- Redeploy: 13 s, restarts only (`images: unchanged`). This answers the owner's question about slow redeploys: the earlier ones were slow because of image rebuilds.
- GHMCP-2 in-container: the item 57 run at 29c305c. Research-profile scout `scout-54e27f891fe8` made 9 completed `@github` read calls through the Go `github-mcp-server` v2.0.2 with `--read-only`, using GITHUB_READ_TOKEN by name. The sandbox-llm image is unchanged since that run, and doctor exercised the same binary and secret at the gate. Zero writes: the read-only flag plus the GHMCP-3 startup check refuses any write tool. This meets the owner's request to just run the Go binary with the configured read secret.
- Clean tree: `git status --porcelain` was empty before this review. Only `docs/final-gate.md` and `docs/final-gate.json` are added by this step.

## TRACEABILITY

I re-tallied all 522 rows myself: 469 `done`, 53 `deferred`, none `missing` or `rework`. Every row has non-empty evidence, and every deferred row carries a written reason (DESIGN §6.1 scope, an owner decision, or an owner-only live check with a FINAL_REPORT pointer). The pass condition holds.

## Phase 2 NITs (FEAT-005 to FEAT-011)

Every NIT has a disposition.

- Fixed with a named test: F005-R2-2, F005-R2-4, F006-R5-1, F008-R2-1, F011-N2.
- Fixed in docs: F007-R4-1.
- Closed as expected: F005-R2-1 (the owner pushes origin himself).
- Deferred with a reason: F005-R2-3 and F010-N1 (owner or DESIGN decisions), F009-R4-1 (owner-only live checks).
- F011-N1 (6a does not drain on a real Windows shutdown) is deferred by the owner's decision of 2026-10-20. The owner still has to confirm whether to ship with it open. That is a NIT and does not block.

## Findings

None blocking.
