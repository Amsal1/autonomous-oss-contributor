# FEAT-009 build review, round 4

Scope: cd46f63..d37fdec (c5ce99c, 2d30db7, b85e4c3, d05d4a7, 8a6c152, d37fdec). The working tree was clean before this review; only this file and the JSON verdict are written. No suites were rerun. The evidence comes from the commit messages and docs/VERIFICATION.md (FEAT-009 round-4 section).

## Prior findings (round 3)

- F009-R3-1 (anomaly pause P0): resolved. `notify.AUTO_PAUSE_RE` matches the text `control.py:161` emits (`⏸ Paused by {actor}: ...`) for actor `anomaly` and the service accounts `uid:N/worker|sandbox|requester`, and classifies it as `(0, False)`. Human pauses (`tg:N`, `uid:0/operator`) stay P2. `test_priorities.py::test_an_anomaly_pause_rings_in_control_during_quiet_hours` drives the real anomaly path (second secret_in_text hit with quiet hours on) and asserts a single pending P0 row in `control` with `disable_notification False`.
- F009-R3-2 (/reject): resolved. `commands/review.py` adds `@command("reject", safety="config")` with a card picker, the four reasons and then `confirm_panel`. Only `reject_confirmed` (a message-bound confirm token) calls `ApprovalService.reject_open`, plus the extracted `Handlers.rejected` effects (unchanged code moved out of `_reject`), `rejected_stub`, `repo_cooldown` and `ask_reject_reason`. `reject` is in the M1 set. Two tests cover it. In the first, the approval stays `pending` up to the confirm tap, then becomes `rejected`, the contribution goes to REJECTED and the 7-day repo cooldown is set. In the second, Cancel rejects nothing and a bad cid gets the usage text. TRACEABILITY DES-FR-6 updated.
- F009-R3-3 (item 48 live check): resolved through the second option the finding offered. Redeploy 1 at 8a6c152 (carries b85e4c3) was followed by redeploy 2 with every step `unchanged`. Doctor PASS 45. A read-only outbox peek shows one dashboard after the restart (only edits of message 507, no new send or pin). The coder explains why proposal and preview cards cannot reach the real chat through product code alone (the fake upstream exists only in the e2e harness). FEAT-009.json is back to `in_progress`, and `blocked_reason` names the owner-only checks. All of this is recorded in VERIFICATION.md.
- F009-R3-4 (NIT, confirms after kill): fixed. In `handlers.py`, a CONFIRMS tap other than `resume_confirm` spends its token while killed, shows the killed view and applies no effect. The test was updated.

## WIP disposition

The WIP was committed, not discarded. The leftover context.py, outbox.py, ui/send.py and telegram/notify.py were finished and committed in c5ce99c. VERIFICATION.md records this ("nothing reverted"). At d05d4a7 the only leftovers were the reviewer's files, which were committed with 8a6c152.

## New code checked

- `reject_open` reuses the status guard (`OPEN_STATUSES`), so a confirm on a card that has already closed raises or returns "no longer open". The nested `db.transaction` follows the same pattern the card path already used.
- `/reject` and its confirm are both gated while killed: the command is outside `KILLED_ALLOWED`, and the confirm is outside `KILLED_CONFIRMS`. The picker action only edits the view.
- Verification for 8a6c152: targeted tests 34 passed, lint gate clean, one hermetic run with 3257 passed and 1 failed. The failure was an outdated assertion text in a test, which was updated; the file was rerun and passed. This is enough evidence.

## Findings

No HIGH or MEDIUM findings.

- F009-R4-1 (NIT, carries to the Phase 2 gate): FEAT-009 stays `in_progress` with these owner-only checks open: typed /kill and /resume, a proposal card and a preview card in the real chat, unpinning stale dashboards 505/506, and the client-rendering probes DES-V-32 and CR3-V-1/4/5/6/12/14/15. The Phase 2 gate must see these done before FEAT-009 is marked completed.

Verdict: APPROVED.
