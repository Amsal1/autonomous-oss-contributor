# FEAT-011 build review, round 1

Verdict: APPROVED (no HIGH or MEDIUM findings; two NITs carried to the Phase 2 gate).

Scope: `c3b8ebb..43dd6fb` (e93a473, 25bc861, 847af0b, 153513d, 6a4e276, 29c305c, fd42743, 5f1c1ed, 43dd6fb), checked against FEAT-011.json, build-plan items 54-57 and Working rules 1-9, DESIGN rev 5 and TRACEABILITY. This is the first round, so there were no prior findings to re-check. The working tree was clean at the start. origin/main equals HEAD, which is expected.

## Evidence checked (suites not re-run)

- Item 54. e93a473 and VERIFICATION FEAT-011 record:
  - 17 passed for test_docs_commands, test_lint_personal and test_profile_example.
  - `ossctl docs commands --check` is current, and `lint personal` reports 0 hits.
  - The lint gate is clean (ruff, format, mypy over 240 files, 11 import contracts kept).
  - The README, AGENTS.md, SETUP.md, ARCHITECTURE.md, steering files and harness-dev.json are all present. The steering and harness files are newly added, so no pre-existing `.kiro/` file was overwritten.
- Item 55:
  - gitleaks over 196 commits with `.gitleaks.toml` found no leaks. The allowlist covers only named fixture files.
  - AC 17 ran in a throwaway clone. A fake `ghp_` token got rc 1 and was not committed. An owner-value file got rc 1. A clean commit got rc 0. The clone was removed afterwards.
- Item 56:
  - `ci.yml` has `contents: read` and `persist-credentials: false`. Every `uses:` is pinned by a 40-hex SHA, and those SHAs are recorded in versions.lock.
  - The gitleaks and PSScriptAnalyzer binaries are sha256-verified.
  - The workflow is `workflow_dispatch` only. That is a recorded owner decision.
  - test_ci_workflow passed. The two failures in the owner's GitHub run were fixed in fd42743 (47 passed). Those fixes only touch the tests, and the canaries are still asserted.
- Item 57:
  - Doctor: `--probes` PASS 46 and `ossctl.ps1 doctor` PASS 45, both after the redeploy.
  - GHMCP-2 live: run `scout-54e27f891fe8` made 9 `@github/...` read calls, all completed.
  - The matrix check lists no row: 470 done, 52 deferred, 0 missing (I grepped and found no `missing`, `in_progress` or `partial`).
  - FINAL_REPORT has been written.
  - The real restart (R1) and the owner-only checks (O1-O8) are deferred by recorded owner decision on 2026-10-20, with exact steps given. The rows for CR1-6a/6b, DES-V-31 and DES-AC-32 are honestly marked `deferred`, not `done`.
- Safety-relevant code change: `probes.py hash-agents` now hashes only `ossbot-*.json`.
  - This matches what `kiro.sh` installs (`find … -name 'ossbot-*.json'`) and what `images.sh` computes on the host. `.dockerignore` keeps `harness-dev.json` out of the image.
  - Both sides use the same glob, so the probe-tuple gate is unchanged for the role agents. Doctor confirms the probes passed for the tuple after the redeploy.
- `test_no_tunnels.py` now treats README.md, AGENTS.md and the steering files as guards (they state the rule, like the skipped `docs/`). The source and test scan is unchanged, and the test re-ran with 9 passed.
- `lint_personal.py`:
  - It reports keys and never values.
  - It refuses a tracked `config/profile.yaml`.
  - It uses argv lists with a timeout and no shell.

No reachable correctness, security or data failure was introduced by the change.

## NITs (non-blocking, carried to the Phase 2 gate)

- N1: the real-shutdown finding means 6a does not drain on a real Windows shutdown. `down.ps1` exits 0xC0000142 and 6b ends 0xC000026B, and the next start reports "last shutdown unclean" (FINAL_REPORT "Restart test"). The defect is in FEAT-008's blocker, and verifying a fix needs a real restart, which the owner deferred. It is listed under "Owner decisions pending", and the rows stay `deferred`. The Phase 2 gate should confirm that the owner accepts shipping with it open.
- N2: in `.githooks/pre-commit`, step 2 runs `ossctl lint personal`, which reads working-tree files, not the staged blobs. With a partial `git add -p`, the staged content and the scanned content can differ. Running `git show :path` on the staged paths would make the check exact. gitleaks itself does scan the staged changes.
