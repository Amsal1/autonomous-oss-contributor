# FEAT-009 build review, round 2: Telegram workspace M1 (gate-1 presentation and the round-1 fixes)

Since round 1, three commits landed on top of `c5009c5`. `94f3f43` delivers item 46: proposal cards with versions and the deterministic diff, decline and Undo, the RESCOPE and ceiling-hit cards, `/work` and link cards, `/size` and `/filters`. `4e1da3a` fixes every round-1 code finding (interdiff navigation, real-builder budget fuzzing, per-kind goldens and purposes, the loudness cap, copy title, `/queue`, `stopped_message_generation`). `f963981` adds the one-open-PR override and moves 93 FEAT-009 rows to `done`. The tree is green on every gate: 3,250 hermetic tests, ruff, ruff format, mypy, lint-imports (8 contracts) and gitleaks. Items 47 and 48 are still not built. Item 46 has one write-path defect and three behaviour gaps against §14.11.15–§14.11.18.

Watch for: items 47 (controls, notifications, offline states) and 48 (close-out, live checks) are missing (confirmed). `🙋 Ask maintainers first` writes an `issue_claim` row directly, with no `<!-- ossbot:action=… -->` marker and no AI-assistance line, so restart reconciliation can't find the posted claim (confirmed). The rescope `✅ Continue at m` isn't refused while draining or `💤 DOWN (workers)` and binds nothing (confirmed). The rescope `📏 Make it smaller` asks for one class below the approved class (confirmed). Scout-authored paths in the forbidden-paths warning aren't defused (confirmed).

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 2 HIGH, 3 MEDIUM, 3 NIT)

## High-level view

All ten round-1 findings are closed except the unbuilt items. TRACEABILITY carries evidence for items 44–46. The rows still open are item 47's, plus the §14.23 verification rows that item 48's live checks close. One item 46 promise also waits on item 47: the "Never this repo" confirm says "Undo in /repos".

The gate-1 card keeps Start work as its only decision token, and the declines and Undo follow §14.11.15. The weak spots are at the edges. `🙋 Ask maintainers first` writes its own `issue_claim` row instead of going through the actions path, so the claim lacks the bytes DESIGN requires on every claim. Some scout text on the card is escaped but not defused.

The rescope card (§14.11.17) is built from kit tokens, not from the proposal Start-work machinery. That is why its Continue skips the draining and workers-down refusals and binds nothing. Its Make it smaller targets the wrong class. The ceiling-hit card meets U13: no button raises the ceiling.

<details>
<summary>Issues (8)</summary>

1. **Items 47 and 48 not delivered** (HIGH): build the §14.14.1 command set with pickers and confirms, priorities, quiet hours, coalescing, the offline and killed states, and short descriptions (item 47). Then redeploy, run the live owner-only checks, and update VERIFICATION, the §14.23 verification rows and the FEAT status (item 48).
2. **Ask-first claim bypasses the actions path** (HIGH): create the `issue_claim` through the same service code as `POST /v1/actions`, so it gets the action marker, the AI-assistance line, normalization, `policy.check_text` and `require_running`. Test the stored bytes.
3. **Rescope Continue isn't a proposal Start work** (MEDIUM): apply `gate1_refusal` (paused, killed, draining, down) at the first tap, at Confirm and before the transition. Bind the measured class and the report to the Confirm token, and record the decision as human.
4. **Rescope Make it smaller asks for the wrong class** (MEDIUM): in `RESCOPE_REQUIRED`, send "fit class <approved>", as §14.11.17 says, and allow it when the approved class is `xs`.
5. **Forbidden-path warning not defused** (MEDIUM): render the scout paths on the `⛔ Touches forbidden paths` line as untrusted `Text`/`Code` spans with the 🤖 label, and add a hazard test.
6. **Expired and superseded proposal copy** (NIT): use `⌛ Expired · re-scouting if still relevant`, `[⛔ Not decidable]` and the "This proposal expired; a fresh one will arrive…" toast.
7. **👤 v1 card doesn't reply to `Working on it`** (NIT): chain a `👤` v1 card to the `Working on it` message when it is in ✅ Reviews.
8. **Validate line labelling** (NIT): show repo facts (test runner found, tiers) with ✅ and keep 🤖 only for the scout's text.

</details>

<details>
<summary>Details</summary>

## Findings

### F009-R2-1 · HIGH · Items 47 and 48 are not delivered
Detail: confirmed. This carries over the open part of F009-R1-1 (item 46 is now in `94f3f43`). No file of item 47 exists: no `telegram/commands/{admin,lifecycle}.py` and no `telegram/notify.py`. None of its tests exist either (`test_commands_full.py`, `test_egress_cmd.py`, `test_hermes_tier_cmd.py`, `test_skill_card.py`, `test_model_cmd.py`, `test_lifecycle_cmds.py`, `test_priorities.py`, `test_quiet_hours.py`, `test_coalesce.py`, `test_offline_states.py`). So `/drain /undrain /down /up`, `/egress`, `/hermes tier`, `/skills` and the skill card, `/model` and the new-model card, `/reload`, `/repos`, `/limits`, `/logs`, `/settings`, `/quiet`, `/notify` and `/new` are absent. So are P0–P3 priorities, quiet hours, coalescing that never holds a message with buttons (AC 49), the §14.16 offline and killed states, and the short-description updates. Item 46 also depends on `/repos`: the "Never this repo" confirm text promises "Undo in /repos". Item 48 hasn't started. Nothing was redeployed, there are no live owner-only checks (topics, pinned dashboard, a dry-run proposal and preview card in the real chat, `/kill` then `/resume`), `docs/VERIFICATION.md` has no FEAT-009 entry, and FEAT-009.json is still `pending`. The matrix check shows 37 `missing` and 2 `rework` FEAT-009 rows. They are item 47's rows plus the verification rows (DES-V-20, DES-V-32, CR3-V-1/4/5/6/12/14/15) and CR3-D12/D13, which item 48 or 47 closes.
Files: `src/ossbot/gatekeeper/telegram/commands/`, `src/ossbot/gatekeeper/telegram/notify.py` (new), `tests/telegram/`, `tests/system/test_lifecycle.sh`, `docs/VERIFICATION.md`, `docs/TRACEABILITY.md`, `docs/plan/task-ossbot-v1/features/FEAT-009.json`.
Suggested fix: build item 47 as phase2-build-plan.md and the FEAT's carried detail specify, commit, then run item 48.

### F009-R2-2 · HIGH · `🙋 Ask maintainers first` writes an `issue_claim` outside the actions path
Detail: confirmed. `proposal_actions.ask_first` inserts the `outbound_actions` row itself, with `{"target_number", "body": CLAIM_BODY}`. The worker path, `api/actions.create_action`, does four things this skips:
- it appends `\n\n<!-- ossbot:action=<id> -->`;
- it runs `normalize_text`;
- it runs `policy.check_text("comment", …, secret_values=…)`;
- it calls `control.require_running`.

DESIGN §12 says: "Every `comment` and `issue_claim` body also ends with the hidden marker `<!-- ossbot:action=<action_id> -->` … Restart reconciliation (12.5, M5) finds a posted comment by this marker". `reconcile.py` searches for exactly that marker. A claim from this button that was posted just before a crash is therefore not recognised after restart. It is retried or marked not found, and the retry can post a second public comment on the upstream issue. The Telegram copy of `CLAIM_BODY` also drops the line the runner's copy carries, `— drafted with AI assistance, reviewed by @{login}` (§12: watcher-drafted comments end with it). The button also works while paused. Dry-run stays on and gate 2 still applies, so nothing is public today. The bytes on the card are still the bytes that would be posted. `test_proposal_card.py::test_ask_maintainers_first_creates_a_claim_card` checks only that a row and an approval exist. It is also likely that the runner's fetch stage requests a second claim after Start work, because `claim_requested` is runner-side state that this path never sets (not traced through the gatekeeper's claim guard).
Files: `src/ossbot/gatekeeper/telegram/proposal_actions.py` (`ask_first`, `CLAIM_BODY`), `src/ossbot/gatekeeper/api/actions.py`, `src/ossbot/runner/stages/fetch.py`, `tests/telegram/test_proposal_card.py`.
Suggested fix: move the body of `create_action` into one gatekeeper function (for example `ContributionService.request_action(cid, kind, target, body, actor)`) used by the API and by the button. Use one claim template with the login. Assert in the test that the stored body ends with the marker and contains the AI-assistance line, and that the button is refused while paused. Then check that the fetch stage reuses an existing claim.

### F009-R2-3 · MEDIUM · Rescope `✅ Continue at m` doesn't follow the proposal Start-work rules
Detail: confirmed. §14.11.17 defines Continue as "a proposal Start work at the measured class: two taps". §14.11.15 says Start work "is refused at Start, at Confirm, and again immediately before its effect … while the system is paused, killed, draining or `💤 DOWN (workers)`", and that "the binding is recomputed at all three points". `rescope_cards.continue_first` checks only that the state is `RESCOPE_REQUIRED`. `continue_confirm` checks `approvals.system_running()` (paused or killed), not `gate1_refusal` (which adds the lifecycle state). After that, `ProposalService.continue_rescope` checks nothing. So Continue goes through while draining or with the workers down. The Confirm token's arg is `{cid, rev}`. The measured class in the sentence is read again at the second tap, and the token's `rev` is never compared with the report's `rev`. If the item leaves and re-enters `RESCOPE_REQUIRED` while a Confirm panel is open, the tap continues with a report the panel didn't show. The transition is recorded with `Actor.GATEKEEPER`, not human, even though a person decided it (the `RESCOPE_REQUIRED → PACKAGING` edge allows only G). `test_rescope_card.py` tests only the paused refusal.
Files: `src/ossbot/gatekeeper/telegram/rescope_cards.py`, `src/ossbot/gatekeeper/proposals.py` (`continue_rescope`), `src/ossbot/gatekeeper/state_machine.py`, `tests/telegram/test_rescope_card.py`.
Suggested fix: call `ctx.decisions.proposals`' gate-1 refusal at both taps and in `continue_rescope`. Put `{rev, measured_class, sha256(size_report_json)}` in the Confirm token's arg and refuse a mismatch with "Superseded…". Record the human actor in the evidence, or add an H edge. Add draining and workers-down cases.

### F009-R2-4 · MEDIUM · Rescope `📏 Make it smaller` targets one class below the approved one
Detail: confirmed. §14.11.17 shows "Approved s … measured m", and its Make it smaller "sends the "fit class s" feedback to scout". That is the approved class. `ProposalService.request_revision(kind="smaller")` uses `approved_class` in `RESCOPE_REQUIRED` and then steps one class down (`SIZE_CLASSES[class_index(current) - 1]`). So the feedback asks for `xs`, and `test_rescope_card.py::test_make_it_smaller_sends_feedback` asserts "fit class xs". When the approved class is `xs`, the button answers "Already the smallest class." and does nothing, so an `xs` item that overran is left with only Continue or Drop. That rescopes harder than the user approved, which matters for the size control the user asked for.
Files: `src/ossbot/gatekeeper/proposals.py` (`request_revision`), `tests/telegram/test_rescope_card.py`.
Suggested fix: in `RESCOPE_REQUIRED`, use `approved_class` itself as the target (and allow `xs`). Keep "next smaller than the estimate" for the gate-1 card. Update the test.

### F009-R2-5 · MEDIUM · The forbidden-paths warning renders scout-authored paths without defusing
Detail: confirmed. `proposal_cards._warnings` builds `"⛔ Touches forbidden paths: " + ", ".join(middle_cut(h, 40) …)` and renders it with `k.line(w)`, a `Const` span. `Const` is escaped, but it isn't passed through `make_visible`/`defuse_commands` and isn't labelled 🤖. The paths come from the scout's `areas`, which are free text up to 200 characters (`Line200`). Any string matching a forbidden glob qualifies, and `*` matches spaces and slashes. So a hostile repo can steer scout into an area such as `.github/workflows/x /kill` or one with bidi controls, and that text appears on the decision card as a clickable command or with disguised characters. That breaks U6/U8 (model text labelled, made visible and defused). `test_kit_builders.py` never reaches this line, because its sample areas don't match a forbidden glob.
Files: `src/ossbot/gatekeeper/telegram/proposal_cards.py` (`_warnings`), `tests/telegram/test_proposal_card.py`.
Suggested fix: build the line from `k.Const("⛔ Touches forbidden paths: ")` plus `k.Code(k.Text(middle_cut(h, 40), 40))` spans. Add a test with a forbidden area containing `/kill` and U+202E, and assert the word joiner and the `⟦U+202E⟧` marker.

### F009-R2-6 · NIT · Expired and superseded proposal cards use the PR-card copy
Detail: confirmed. §14.11.15 says an expired proposal is edited to `⌛ Expired · re-scouting if still relevant` with `[⛔ Not decidable](disabled)`, and a tap answers "This proposal expired; a fresh one will arrive if the issue is still open." §14.11.16 gives the superseded card `[⛔ Not decidable]`. `stale.render` renders `⌛ Expired · approval was valid 72 h` and `CardPublisher.render_stale` adds `⛔ Not approvable` for every family. A kit tap answers "This proposal is no longer open." Neither string from §14.11.15/16 exists in `src/`.
Files: `src/ossbot/gatekeeper/telegram/review/stale.py`, `src/ossbot/gatekeeper/telegram/publisher.py`, `src/ossbot/gatekeeper/telegram/proposal_actions.py`.
Suggested fix: branch on `gate == 1` for the header, the disabled label and the toast of an expired approval.

### F009-R2-7 · NIT · A `👤` v1 proposal card doesn't reply to `Working on it`
Detail: confirmed. §14.11.15 Delivery: "a `👤` v1 card replies to the `Working on it` message when that is in ✅ Reviews". `CardPublisher._chain_to` chains only to an earlier proposal card, so a v1 card always starts a new chain.
Files: `src/ossbot/gatekeeper/telegram/publisher.py`.
Suggested fix: when the cid has a `work_request` view in the Reviews thread and no earlier proposal card, reply to that view.

### F009-R2-8 · NIT · The Validate line labels everything 🤖
Detail: confirmed. §14.11.15 shows `Validate ✅ repo has tests (vitest) · T0 unit · T1 n/a · 🤖 easy` (repo facts from vet or scout tool output with ✅, difficulty with 🤖). `_validate_line` renders `Validate 🤖 <n> test(s) to add/update · <validation_plan>`. Labelling a fact as 🤖 is the conservative direction, so it is only a presentation gap.
Files: `src/ossbot/gatekeeper/telegram/proposal_cards.py`.
Suggested fix: show the vet's test-runner fact and the tier with ✅, and keep the scout's plan text under 🤖.

## Round-1 findings re-checked

F009-R1-1 is now partial and carried as F009-R2-1. F009-R1-2 is fixed for TRACEABILITY: 93 FEAT-009 rows are `done` with test evidence, and shared rows (for example CHAT-27, CR3-AC-66, CR3-BA-16) name their FEAT-009 part. Its VERIFICATION half moves to item 48 (F009-R2-1). F009-R1-3 is fixed: the subject carries `source=interdiff`, and `nav`, `file_pick` and `comment_hunk` stay in the interdiff, with a 2-file, 2-page test. F009-R1-4 is fixed by `test_kit_builders.py`. F009-R1-5 is fixed by `test_card_goldens.py` and 10 goldens. F009-R1-6 is fixed by `test_loudness.py`. F009-R1-7 is fixed: an over-long copy button is dropped, never cut, and the 256-character test checks the button. F009-R1-8 is fixed (labels, expired filtered, maintainer-triggered `push_update` in class 0). F009-R1-9 is fixed (mockup lines at y=652/670). F009-R1-10 is fixed (`ALLOWED_UPDATES` plus a polling test).

## What was verified and how

The working tree was clean at `f963981` (`git status --short` empty), so the gates ran on the committed tree in the shared DEV venv.

- `DEV pytest -m "unit or telegram or acp or e2e" -n 6`: 3,250 passed, 0 failed (8 min 10 s). The 17 warnings are Starlette's `httpx` deprecation in e2e. This includes `test_handlers_nonblocking.py` (`/kill` within 2 s while a fake Hermes stream is held open; Approve answered within 2 s) and every new item 46 test module.
- `DEV ruff check src tests`: all checks passed. `DEV ruff format --check src tests`: 427 files already formatted. `DEV mypy`: no issues in 211 source files. `DEV lint-imports`: 8 contracts kept, 0 broken.
- gitleaks (`/usr/local/bin/gitleaks git --redact` over the whole history, 178 commits): no leaks found.
- Git: every commit from `69f5e4e` to `f963981` has author and committer `Amsal Khan <md.amsalkhan@gmail.com>`. `origin/main` is still `69f5e4e`, the same as at round 1, so nothing was pushed since and no history was rewritten.
- Matrix check (the build-plan command for FEAT-009): 93 done, 37 missing, 2 rework. The open rows are listed under F009-R2-1.
- Code read, by safety rule:
  - **Decision tokens.** Start work is the only decision token on the proposal card (`proposal_start`, single-use, message-bound). Every other button is a kit token with `approval_id` NULL, resolved by `(cid, version)`, and an old version answers "Superseded…".
  - **Declines.** Decline goes through `ProposalService.decline` → `ProposalApprovals.reject(…, *, reason, actor, snooze_until)` in one transaction (token kill, the human transition, `proposal_declined`, the never record).
  - **Undo.** Undo is a single-use 10-minute token, checked against the `proposal_declined` time and the state-machine `UNDO_WINDOW`. It re-sends through `reissue_proposal(keep_expiry=True)`.
  - **Size pickers.** `/size` and `/filters` write only through `DiscoveryPrefs.update`/`undo`, and `xxl` is refused.
  - **Read-only explorer.** The rescope explorer opens with `action_id=None` and has no comment, Show card or approve purposes (`test_explorer_is_read_only`).
  - **Update budget.** The `/work` and link-card GitHub reads run in `spawn(…, "github_read")` with a 10 s timeout. No new handler awaits I/O inline.
  - **Gate-2 override.** The one-open-PR override lifts only `open_prs_per_repo`, only for a `👤` contribution with an audited `open_pr_override`, and only after the Confirm panel stated it (`test_open_pr_override.py`).
- Untrusted text: issue titles, notes, approach, files, the diff lines, rationale and the coder's size reason are `Text`/`claim` spans (escaped, made visible, defused). The exception is F009-R2-5.
- Not run in this round, because the FEAT isn't at close-out: redeploy, `ossctl doctor`, the root suites and the live owner-only checks.

</details>

<details>
<summary>File map</summary>

- `telegram/proposal_cards.py`: the gate-1 card, the version diff, Details (6 sections), the Start-work Confirm panel and the keyboard.
- `telegram/proposal_actions.py`: Skip, Later (+ custom days), Never (+ confirm), Undo, Adjust, Make it smaller, Details navigation, Ask maintainers first.
- `telegram/rescope_cards.py`: the RESCOPE_REQUIRED and ceiling-hit cards, Continue, Re-check, Split, Drop, read-only Explore.
- `telegram/work_links.py`: `/work`, `Working on it`, link cards with notes (BA-8), the AI-ban refusal card.
- `telegram/size_pickers.py`: `/size` (global, preferred, per repo with `inherit`) and `/filters` (4 pages), with 60 s Undo.
- `gatekeeper/{proposals,proposal_approvals,packaging,live_checks,context}.py`, `api/contributions.py`: the BA-12 `reject`, the open-PR override, rescope card dispatch, the S6 change-request consumption, the refusal-card hook.
- `telegram/{publisher,review/*,ui/send,bot}.py`: round-1 fixes (proposal reply chains, interdiff source, `/queue` classes, copy title, `ALLOWED_UPDATES`).
- `runner/stages/{discover,vet}.py`, `common/config.py`: `max_open_prs_by_me_per_repo`, the AI-ban evidence.
- `tests/telegram/*` (8 new item 46 modules, `test_kit_builders.py`, `test_card_goldens.py`, `test_loudness.py`, 10 goldens), `tests/unit/gatekeeper/test_open_pr_override.py`.
- `docs/TRACEABILITY.md`, `docs/ux/01-pr-preview-card.svg`.
- Full diff: `git -C d:\Work\autonomous-oss-contributor diff c5009c5..f963981` (round 2), `git -C d:\Work\autonomous-oss-contributor diff ef4657a..f963981` (whole FEAT).

</details>
