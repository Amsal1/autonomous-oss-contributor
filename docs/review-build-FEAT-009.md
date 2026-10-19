# FEAT-009 build review, round 1: Telegram workspace M1 (kit, plumbing and the PR review surface)

FEAT-009 is half built. Two commits since base `ef4657a` deliver item 44 (component kit, typed Bot API 10.1–10.3 adapter, routing table, topics with flat fallback, menu scopes, pinned dashboard, `pending_input`, migration 0005, two import-linter contracts) and item 45 (the S1–S12 PR review surface: decorated cards with the size line and `👤` marker, explorer, file views, `.patch`/HTML/`pr_body.md` documents, comments and the draft review, interdiff, S7 edit text, S8 confirm, S9, S12, `/queue`). The committed tree is green on every gate: 3,138 hermetic tests, ruff, ruff format, mypy, lint-imports (8 contracts) and gitleaks. Items 46 (CR-004 presentation), 47 (controls, notifications, offline states) and 48 (close-out, live checks) are not committed. Item 46 sits half-done as uncommitted work in the tree, and no TRACEABILITY or VERIFICATION row has moved.

Watch for: the unfinished items and the dirty tree (confirmed); the interdiff view drops into the full-diff file view as soon as you navigate or comment, so you can't review a multi-file or multi-page interdiff (confirmed); AC 1's budget fuzzing covers two synthetic builders, not the real views (confirmed); the AC 5 per-kind S1 goldens and the AC 11 loudness cap have no tests (confirmed).

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 2 HIGH, 4 MEDIUM, 4 NIT)

## High-level view

The kit and plumbing (item 44) follow §14.18 and §14.10, and the safety rules found nothing to fix: owner-only routing, escaped kit rendering, `pending_input` by reply id only, and the handler budget with `/kill` within 2 s. The gap is test depth. The AC 1 budget property test fuzzes a synthetic `card_like` view and the quoted chat answer, not the production view builders.

The review surface (item 45) keeps Approve and Confirm on the card only, and the PR title is never cut. The interdiff is the weak part. It is computed correctly at `/package`, but its view reuses the file-view builder, and every button on it leads back to the full diff. Line-level review of a revision is therefore limited to the first interdiff file and page. The per-kind S1 goldens and the loudness cap are untested.

Items 46–48 are missing: proposal cards, versions, decline and Undo, the rescope and ceiling cards, `/work` and link cards, `/size` and `/filters`, the full §14.14.1 command set, priorities, quiet hours, coalescing, the offline and killed states, and the live owner-only checks. The 128 open FEAT-009 rows in TRACEABILITY (118 missing, 10 rework) include rows that items 44 and 45 already implement.

<details>
<summary>Issues (10)</summary>

1. **Items 46–48 not delivered; WIP in the tree** (HIGH): finish, test and commit the uncommitted item 46 files, then build items 47 and 48 as the build plan specifies.
2. **TRACEABILITY and VERIFICATION untouched** (HIGH): set the rows closed by items 44 and 45 to done with evidence, record the FEAT-009 part on shared rows, and add the FEAT-009 VERIFICATION entries.
3. **Interdiff view leaves the interdiff** (MEDIUM): keep file navigation, paging, the hunk picker and quote replies inside the interdiff patch (view `interdiff`), and add a multi-file, multi-page interdiff test.
4. **AC 1 budget fuzzing covers no real view** (MEDIUM): run the Hypothesis budget test over every real View builder in classic and rich modes, and assert AC 53's "maximal untrusted input never reaches ViewTooLarge".
5. **AC 5 per-kind S1 goldens missing** (MEDIUM): add LIVE and DRY RUN goldens for `open_pr` r1/rN, `push_update`, `comment`, `issue_claim` and `skill_activate`, and assert each card's minted purposes and `single_use` flags against §14.18.4.1.
6. **AC 11 loudness cap untested** (MEDIUM): test that at most `loud_cards_per_hour` new cards per hour go out with sound, that classes 0–1 ring, and that re-sends and autonomous proposals are silent.
7. **Copy title is cut, not checked** (NIT): replace the silent `[:256]` slice with a length check (omit the button when the title is over the limit), and assert the 256-character title's copy button carries it in full.
8. **/queue details differ from §14.11.13** (NIT): sort labels, class 0 for maintainer-triggered `push_update`, and approvals past `expires_at` still listed until the sweep.
9. **Mockup 01 text overlap** (NIT): the new note line at y=648 overlaps `📝 Edit text → picker` at y=654.
10. **`ALLOWED_UPDATES` lacks `stopped_message_generation`** (NIT): add it with the FEAT-010 streaming work (or now), as §14.2 requires; the rule-2 handler already exists.

</details>

<details>
<summary>Details</summary>

## Findings

### F009-R1-1 · HIGH · Items 46, 47 and 48 are not delivered, and the tree holds uncommitted item 46 work
Detail: confirmed. The two FEAT-009 commits are `69f5e4e` (item 44) and `1bc77a7` (item 45). `git status --porcelain` lists 9 modified files (`context.py`, `main.py`, `packaging.py`, `proposal_approvals.py`, `proposals.py`, `telegram/commands/__init__.py`, `telegram/commands/registry.py`, `telegram/handlers.py`, `telegram/keyboards.py`) and 5 untracked ones (`telegram/proposal_actions.py`, `proposal_cards.py`, `rescope_cards.py`, `size_pickers.py`, `work_links.py`). That is item 46 in progress, with none of its tests (`test_proposal_card.py`, `test_proposal_versions.py`, `test_proposal_undo.py`, `test_rescope_card.py`, `test_ceiling_card.py`, `test_work_link_cards.py`, `test_size_picker.py`, `test_filters_picker.py`). Item 47 has no code: the §14.14.1 command set with pickers (`/drain /undrain /down /up`, `/egress`, `/hermes tier`, `/skills` and the skill card, `/model` and the new-model card, `/reload`, `/repos`, `/limits`, `/logs`, `/settings`, `/quiet`, `/notify`, `/new`), `telegram/notify.py` priorities, quiet hours, coalescing and batching, the §14.16 offline and killed states, short-description updates, the stale-message rules, and the item 47 tests. Item 48 is also missing: redeploy, live owner-only checks (topics, pinned dashboard, a dry-run proposal and preview card in the real chat, `/kill` then `/resume`), matrix, VERIFICATION, FEAT `completed`. FEAT-009.json is still `pending`. Under working rule 4 a reviewer can't approve a dirty tree.
Files: the 14 working-tree files above; `docs/plan/task-ossbot-v1/features/FEAT-009.json`.
Suggested fix: read the WIP, finish item 46 with its tests and commit it. Then build items 47 and 48 as phase2-build-plan.md specifies, committing per item.

### F009-R1-2 · HIGH · No TRACEABILITY or VERIFICATION update for items 44 and 45
Detail: confirmed. `git diff ef4657a..HEAD -- docs/TRACEABILITY.md docs/VERIFICATION.md` is empty. The matrix check for FEAT-009 shows 118 rows `missing`, 10 `rework` and 4 `done` (all done before this FEAT). Rows that the committed code implements and tests are still open. Examples: DES-V-20, CR3-M1 (FEAT-009 part), TGRW-10, TGRW-12, TGRW-15 (FEAT-009 part), CR3-D4, CR3-AC-1 (partial, see F009-R1-4), CR3-AC-4, CR3-AC-9, CR3-AC-12, CR3-AC-14. VERIFICATION.md has no FEAT-009 entry. Context step 1 says "When an item closes rows, set them to done with evidence in the same commit", and working rule 2 says to update as you go. The Totals line wasn't updated either.
Files: `docs/TRACEABILITY.md`, `docs/VERIFICATION.md`.
Suggested fix: for each row named by items 44 and 45, set it to `done` with the test or file evidence, or keep it open with the missing part named. Shared rows record the FEAT-009 part. Update the Totals line and add the hermetic run to VERIFICATION.md.

### F009-R1-3 · MEDIUM · The interdiff view can't show past the first file or page, and its comments land on the full diff
Detail: confirmed. In `INTERDIFF` mode, `review/interdiff_view.view` builds the view with `explorer.file_view(subject_with_interdiff_files, 0, page)` and changes only `kind`. The buttons on that view come from `file_view`. `◀ Prev file` / `Next file ▶` are `file_pick`, the `📂 1/N` picker and the page pager are `nav` with `view="file_picker"`/`"file"`, and `💬 Comment` is `nav` `view="hunk_picker"`. All of them call `review.actions.open_file`, which reloads the subject from `change.patch`, the full change. So with a 3-file interdiff, `Next file ▶` opens file 2 of the full diff, not of the interdiff. A long interdiff file can't be paged. The hunk picker offers the full diff's hunks. Quote replies on an interdiff view also skip the line locator, because `on_view_reply` runs `patchview.locate` only for `kind == "file"`. They are stored file-level against `view["path"]`, which is always the first interdiff file, even when the quote comes from elsewhere. §14.11.7 says that in `INTERDIFF` mode the view "has the same comment features as a file view". `tests/telegram/test_interdiff.py` uses a single-file interdiff only.
Files: `src/ossbot/gatekeeper/telegram/review/interdiff_view.py`, `src/ossbot/gatekeeper/telegram/review/explorer.py` (`file_view`), `src/ossbot/gatekeeper/telegram/review/actions.py` (`nav`, `file_pick`, `on_view_reply`), `tests/telegram/test_interdiff.py`.
Suggested fix: give the file-view builder a source flag (`change` or `interdiff`) carried in the nav args (`view="interdiff"`, `index`, `page`, `hunk_picker`), so navigation, paging and the hunk picker resolve against `interdiff.patch`. Run the quote locator for `kind == "interdiff"` in `INTERDIFF` mode, and keep file-level comments for `RANGE_DIFF`. Add a test with a 2-file, 2-page interdiff that taps Next file, the pager, the hunk picker and a quote reply.

### F009-R1-4 · MEDIUM · AC 1 budget property test doesn't cover the real view builders or rich mode
Detail: confirmed. `tests/telegram/test_kit_limits.py::test_every_builder_fits_its_budget` uses `BUILDERS = {"chat_answer_quoted", "card_like"}`. `card_like` is a hand-made view, not `cards.render`. AC 1 asks for "Hypothesis tests over all View builders, in classic and rich modes, with random untrusted input". The builders that take untrusted text get no fuzzing: S1 per kind via `cards.render`/`view_for_action`, `explorer.explorer_view` for all 9 sections (issue title, vetter and planner claims, review findings, test tails), `file_view`, `interdiff_view.view`, `comments.draft_view`, `queue.view`, `dashboard`, the edit picker and prompts, and the S12 stale renders. AC 53's second sentence, "Maximal untrusted input never reaches ViewTooLarge", is tested only with a long title on a synthetic view. Rich rendering (`rich.py`) isn't in the property test.
Files: `tests/telegram/test_kit_limits.py`; the builders above.
Suggested fix: register every production View builder in `BUILDERS`, with a fixture that fills each untrusted field at its maximal bound. Render each in classic and, where it has a rich form, rich mode. Assert the §14.17.4 budgets and 24-byte `callback_data`, and that no builder raises `ViewTooLarge`.

### F009-R1-5 · MEDIUM · AC 5 goldens per kind and mode, and card-level purpose checks, are missing
Detail: confirmed. `tests/telegram/golden/` holds only `dashboard.html` and `s1_title_256.html`. AC 5 requires golden S1 cards in LIVE and DRY RUN for `open_pr` (r1, and rN with interdiff), `push_update`, `comment`, `issue_claim` and `skill_activate`. It also requires "each card's minted purposes and their `single_use` flags equal the §14.18.4.1 table", and that `comment`/`issue_claim` cards lack Explore, Files and Request changes, with `📝 Edit text` opening `edit_comment`. `tests/unit/gatekeeper/test_card_purposes.py` (FEAT-005) checks the purpose function. `test_explorer.py::test_card_keyboard_per_open_pr` checks only the `open_pr` labels. No test reads the `ui_tokens` rows a published card of each kind actually minted. `test_s1_cards.py` covers the headers by substring only.
Files: `tests/telegram/test_s1_cards.py`, `tests/telegram/golden/`.
Suggested fix: add the 12 goldens through `tests/telegram/golden.py`. Add a parametrized test that publishes each kind through the rig and compares `SELECT purpose, single_use FROM ui_tokens WHERE tg_message_id = <card>` with the §14.18.4.1 table.

### F009-R1-6 · MEDIUM · The AC 11 loudness cap has no test
Detail: confirmed. `CardPublisher._loud` puts the rules of §14.11.13 into code: re-sends silent, autonomous proposals never loud, classes 0–1 (claims, `👤` work) loud, the rest loud while fewer than `telegram.loud_cards_per_hour` `card:` rows with `priority = 1` went out in the last hour. No test anywhere touches `loud_cards_per_hour` except the config range check. AC 11's last sentence ("At most `loud_cards_per_hour` cards per hour go out without `disable_notification`") is assigned to item 45 (CR3-AC-11). The count also relies on `card:` rows getting `priority = 1` exactly when they ring, which nothing tests.
Files: `src/ossbot/gatekeeper/telegram/publisher.py` (`_loud`), `tests/telegram/test_queue.py` or a new test.
Suggested fix: publish 4 `open_pr` cards within an hour and assert 2 ring and 2 carry `disable_notification: true`. Assert that an `issue_claim` and a `👤` card ring past the cap, that a Show card re-send is silent, and that a card an hour later rings again.

### F009-R1-7 · NIT · Copy title is sliced instead of length-checked
Detail: confirmed. `ui/send.py` emits `{"copy_text": {"text": spec.copy_text[:COPY_TEXT_MAX]}}`. Item 45 and the FEAT acceptance criterion ask for a copy-title length check, and §14.11.2 section 9 says it copies "the stored title unchanged". Titles are already limited to 256 code points, so today the slice is a no-op. The behaviour is still "cut", not "check", and no test asserts that the 256-character title's `📋 Copy title` button carries it in full (`test_title_never_cut.py` checks the card, Confirm and explorer text only).
Files: `src/ossbot/gatekeeper/telegram/ui/send.py`, `tests/telegram/test_title_never_cut.py`.
Suggested fix: when `copy_text` exceeds the limit, omit the button (log a warning) instead of cutting. Extend the 256-character test to the copy button.

### F009-R1-8 · NIT · /queue details differ from §14.11.13
Detail: confirmed. The sort buttons read `⬆️ Oldest` and `🎯 Priority`, while §14.11.13 says `[⬆️ Oldest first] [🎯 By priority]`. A `push_update` triggered by a maintainer review isn't ranked class 0; every `push_update` is class 2. `items()` lists `pending`/`armed` approvals whose `expires_at` has passed until the sweep runs, but the section says expired items aren't listed. The ceiling and rescope classes come with item 46.
Files: `src/ossbot/gatekeeper/telegram/review/queue.py`.
Suggested fix: align the labels, filter `expires_at > now`, and rank a maintainer-triggered `push_update` as class 0 once the trigger is recorded (or note the gap in TRACEABILITY).

### F009-R1-9 · NIT · Mockup 01 has overlapping text
Detail: confirmed. The new constraint note line `never cut; 📋 Copy title copies it unchanged.` sits at y=648, and the existing bold `📝 Edit text → picker (reply to card)` sits at y=654 in the same column, so the two lines overlap.
Files: `docs/ux/01-pr-preview-card.svg`.
Suggested fix: move the `📝 Edit text` label and its button block down by one line (about 16 px).

### F009-R1-10 · NIT · `ALLOWED_UPDATES` doesn't request `stopped_message_generation`
Detail: confirmed. `telegram/bot.py` keeps `ALLOWED_UPDATES = ["message", "callback_query"]`, and its docstring says the same. Item 44 added routing rule 2 (`Handlers.on_raw`, `rawapi.parse_stopped`, `auth.authorized_chat_only`). §14.2 sets `ALLOWED_UPDATES = ["message", "callback_query", "stopped_message_generation"]`. Drafts that can be stopped arrive with FEAT-010 item 49, so nothing is user-visible yet.
Files: `src/ossbot/gatekeeper/telegram/bot.py`.
Suggested fix: add the update type now (with a polling test), or record it in FEAT-009's findings for item 49.

## What was verified and how

The committed tree was exported with `git archive HEAD` to `/tmp/r009` inside the distro. It ran against a separate venv (`/tmp/r009venv`, `uv sync --frozen --offline`), so the uncommitted WIP didn't affect the results and the shared DEV venv wasn't touched.

- `pytest -m "unit or telegram or acp or e2e" -n 6`: 3,138 passed, 0 failed (7 min 15 s). This includes `test_handlers_nonblocking.py` (`/kill` within 2 s while a fake Hermes stream is held open; Approve answered within 2 s), `test_title_never_cut.py`, `test_pending_input.py`, `test_edit_text.py`, `test_routing.py`, `test_scopes.py`, `test_topics_fallback.py`, `test_dashboard.py` and `test_stale.py`.
- `ruff check src tests`: all checks passed. `ruff format --check src tests`: 408 files already formatted. `mypy`: no issues in 206 source files. `lint-imports`: 8 contracts kept, 0 broken (new: "The chat relay never imports the approval, state, control or outbox modules" and "The UI kit never imports the decision or submit paths", whose only ignored import is a typing-only `ui.send -> context`).
- gitleaks (`/usr/local/bin/gitleaks git --redact` over the whole history, 174 commits): no leaks. `gitleaks dir --redact src` over the working tree, WIP included: no leaks.
- Authorship: `69f5e4e`, `c227f78` and `1bc77a7` have author and committer `Amsal Khan <md.amsalkhan@gmail.com>`. `origin/main` (`69f5e4e`) is an ancestor of local `main`, and no history was rewritten. Under working rule 5, the owner's pushes aren't a finding. No hooks are installed.
- Code read for safety. Only `auth.authorized()` messages and callbacks reach routing, and `stopped_message_generation` passes `authorized_chat_only()` only. Explorer, file and interdiff tokens are kit `nav` tokens without an approval id (`test_explorer.py`). Approve and Confirm exist only on the card (`review/actions.py` has none). `edit_pr_text` goes through `ApprovalService.set_text_override`, whose policy refusals are tested at service level (`tests/unit/gatekeeper/test_approvals.py::test_text_override_refused_before_storing`: confidentiality floor, missing disclosure, secret pattern, gatekeeper secret value, nothing stored). `edit_comment` runs `policy.check_text` with `secret_values` before storing. Documents scan publish bytes and never alter them (`test_documents.py`). The HTML template autoescapes and has no `safe` filter.
- Handler budget by grep and code read: no `httpx`, `subprocess`, `git.run` or `asyncio.sleep` in a `telegram/**` handler path. Documents, `getFile`, `io` commands and `io` actions are spawned. The interdiff runs at `/package` in `packaging.py`.
- Not run in this round, because the FEAT isn't at close-out: redeploy, `ossctl doctor`, the root suites, live owner-only checks and the WIP's tests.

</details>

<details>
<summary>File map</summary>

- `src/ossbot/gatekeeper/telegram/ui/*`: the component kit (views, blocks, classic and rich renderers, sanitizing, limits, icons, mint-then-bind sending).
- `telegram/rawapi.py`: typed Bot API 10.1–10.3 request and response models.
- `telegram/routing.py`, `pending_input.py`, `inputs.py`, `actions.py`: the routing table, ForceReply correlation, typed-input and button registries.
- `telegram/topics.py`, `scopes.py`, `dashboard.py`, `dashboard_actions.py`: topics with flat fallback, command scopes, pinned dashboard.
- `telegram/chat_relay.py`: non-streaming Hermes relay (rules 7–9).
- `telegram/publisher.py`, `cards.py`, `keyboards.py`: card publisher (loudness, recovery, stubs) and the S1 renderer.
- `telegram/review/*`: explorer, file views, documents, comments and the draft review, interdiff view, edit text, confirm, stale, queue, size line, action resolution.
- `gatekeeper/interdiff.py`, `review_html.py`, `templates/review.html.j2`: interdiff computation at `/package`, and the HTML diff.
- `gatekeeper/migrations/0005_telegram_ux.sql`, `outbox.py`, `common/config.py`, `config/telegram.yaml`: the data model, outbox priority, thread and coalesce columns, and the strict Telegram config.
- `docs/adr/0020-telegram-library.md` (addendum), `docs/ux/01`, `03` (full title).
- `tests/telegram/*` (30 test modules, harness `tgharness.py`, `reviewkit.py`, goldens), `tests/fakes/fake_telegram.py`, `tests/unit/*` updates.
- Full diff: `git -C d:\Work\autonomous-oss-contributor diff ef4657a..1bc77a7`.

</details>
