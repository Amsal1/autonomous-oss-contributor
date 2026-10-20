# FEAT-010 build review, round 2

Reviewed at 069be20 (main). Range: 433b55d..069be20 (after the FEAT-009 approval 939b689). Working tree clean before the review files were written.

Verdict: APPROVED. No HIGH or MEDIUM findings open. One NIT carries to the Phase 2 gate.

## Prior findings (round 1)

- F1 (HIGH, item 50 WIP left in the tree): resolved. The WIP was finished and committed in 958bc6c, with follow-ups in 1d64cf0 (status-card dedupe occurrence, migration 0006 count, dedupe-key list). VERIFICATION.md records how it was handled. `git status` is clean.
- F2 (HIGH, items 50-53 missing): resolved. Item 50 is in 958bc6c and 1d64cf0. Items 51-52 are in 4d9e575 and e84e401. Item 53's close-out is in 069be20: the VERIFICATION entry, 65 TRACEABILITY rows, the ADR-0019 implementation addendum, plan items 49-52 ticked, and FEAT-010.json `in_progress` with blocked_reason set to the owner-only live checks.

## Evidence read (not re-run)

VERIFICATION.md FEAT-010 round 2 and the commit messages of e84e401 and 069be20 record the following:
- targeted item 50 tests: 44 passed, plus tests/telegram 503 passed
- items 51-52 targeted: 243 and 85 passed
- one hermetic run of `unit or telegram or acp or e2e`: 3492 passed, 6 failed. The 6 failures were the migration count, the dedupe-key list and one fake_github regression. They were fixed in 1d64cf0 and 4d9e575 and re-run targeted: 62 passed.
- BA-19/BA-20/F6-4 at e84e401: 46 passed
- lint.ps1 clean (11 import contracts kept, including the review_mirror_writer and telegraph_writer contracts)
- shellcheck and the setup-secrets selftest pass
- redeploy at 4d9e575 and at e84e401, then an idempotency run where every step printed `unchanged` (doctor PASS 41)
- test_egress_surface.sh passed: telegra.ph returns 403 for every caller, and cat_telegraph is absent while Telegraph is off
- ossctl doctor PASS 45

This matches the FEAT-010 verification list and the code-then-targeted-tests-then-one-full-run loop the owner asked for. Live checks that need the owner's hands are left open, as the plan allows.

## Spot reads (safety-relevant paths)

- `ctl/render.py` and `common/config.py`: the `telegraph` category can't be listed by any profile, caller or mode (config validator). The renderer emits it only for `gatekeeper`, only while `telegram.telegraph.enabled`, and as the one allowed pre-deny rule. Both `assert_context_invariants` checks reject anything else. The always_denied domains are unchanged.
- `gitops.push_with_token`: a faithful extraction of the old `github_writer` askpass/memfd code. The token stays out of argv, the URL and env. `github_writer` still never forces a push.
- `review_mirror_writer.py`: bound to one repo. API paths are confined to `/repos/<mirror>/` with `..` and `//` refused. Refspecs are validated by SHA and cid regexes. A force push happens only to `review/<cid>` and `review-base/<cid>`. Writes are reachable only through `active_repo()`, which needs the config setting, a confirmed reload and a passing `enable` probe. That matches ADR-0019 C and DESIGN §14.11.14, and it is off by default.
- `telegraph.py`: one `check()` → `eligible()` path at render, tap, Confirm and right before createPage. A paused or killed system refuses the write before the API call.

## NIT (non-blocking, carries to the Phase 2 gate)

- N1: The closed-PR status card (DESIGN §14.11.11) lacks `[🔁 Re-attempt later] [🗑 Forget]`. The coder recorded this as an open question (it conflicts with AC 30). DESIGN already says "§5 AC 30 rules still apply", so the owner should decide one of two things: what Re-attempt later means for a CLOSED contribution, or whether to drop the button. `Forget` does not depend on AC 30. TRACEABILITY CR3-IH-2 is marked `done` but should note it is partial until this is decided.
