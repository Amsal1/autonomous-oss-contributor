# CR-003 / ADR-0019 design review, round 3

Reviewed: `docs/plan/CR-003-telegram-ux.md` (revision 3, all 2,067 lines), `docs/adr/0019-pr-review-surface.md` (revision 3, including Addendum A), the visible text and constraint panels of `docs/ux/00..12-*.svg`.
Context read: ADR-0004; CR-004 (states and Amendment B); FEAT-005 items 2–4; the round-2 review.
Code read: `gatekeeper/approvals.py`, `outbox.py`, `context.py`, `app.py`, `control.py` (kill, resume, reconcile, dry-run), `state_machine.py` (`sweep_expired`, IMPLEMENTING invalidation), `submit.py` (`_step0`), `packaging.py` (invalidation and action cancel sites), `db.py`, `api/notify.py`, `common/ids.py`, `common/schemas.py` (`NotifyRequest`), `telegram/{bot,handlers,cards,keyboards,escape,auth}.py`, `commands/builtin.py`, `migrations/0001_init.sql`, `pyproject.toml`; the installed PTB 22.8 `telegram/ext/_applicationbuilder.py` in the `ossbot` WSL distro.
Not re-checked this round: the live web sources (no web access in this step). §19 and Addendum A.6 cite them with URLs and access dates, and round 2 checked them against the live pages.

**Verdict: CHANGES_REQUESTED** (2 HIGH, 5 MEDIUM, 9 NIT).

## Overall
Revision 3 closes all 22 round-2 findings in substance. The hard requirements are met at the decision level:
- **ADR-0019** evaluates A, B1/B2/B3 (with the `sendData` constraint), C (with the scoped-token caveat and probe), D and the combinations on every required criterion. It recommends A+C. The default needs zero setup.
- **The review flow** (S0–S12) is fully specified: card, explore, act/interdiff, edit, approve/confirm, submitted/live/merged. It includes stale handling and the `/queue` ranking.
- **Deliverables 2 and 3** are complete. §13 prioritizes the integration hooks and marks each v1, v1.x or phase 2.
- **Research is cited**, not asserted.
- **The mockups** stay inside the 4,096-character message limit, 24-byte `callback_data`, the per-row label caps and ≤ 6 rows.
- **No new inbound path exists by default.** B3 is optional, read-only, Ed25519-only and can never approve.

The remaining problems are in the re-send path and the runtime model, where the CR builds on code it describes as reviewed and frozen:
- `reissue()` carries one `body_parts_total=1` for every card it re-creates, so Approve can appear before the verbatim parts land (ADR-0004);
- the CR relies on PTB handling one update at a time but never moves long Hermes turns out of the handler, so `/kill` and Stop would wait behind a streaming answer.

The five MEDIUM findings:
- **Show current** can't reach the newer card after a new rev or a new proposal version.
- **Replies to comment and claim cards** have no valid meaning.
- **`reissue()` can revive cards** for contributions that can't be submitted.
- **Proposal Skip/Later Undo** has no defined lifecycle.
- **Flat mode**, the zero-setup default, doesn't get the lookalike protection that ✅ Reviews has.

## Hard-requirement check

| Requirement | Status |
|---|---|
| Deterministic, two-tap, single-use, message-bound, expiring, owner-only, no-LLM decisions | Held (S1, U3, §12.4); view purposes are multi-use but never decide |
| Binding to SHA + patch + title/body hashes, invalidated by any edit | Held (S2, U4; `compute_binding` unchanged) |
| Nothing public before approval; dry-run ON and labelled | Held (S3; Telegraph is its own two-tap public act, labelled "n/a") |
| No new inbound exposure by default; Mini App optional, read-only, Ed25519, no secrets, never approves | Held (S4, ADR Decision 3) |
| ADR-0019 options, criteria, one recommendation, zero-setup default, full flow, stale handling, queueing | Met; stale "Show current" is broken by the resolution rules (finding 3) |
| Deliverables 2 and 3 | Met |
| Research cited | Met (§1, §19, Addendum A.6) |
| Mockups respect Telegram constraints and cover key screens | Met (13 screens; constraint panels consistent with §11.2/§11.4) |
| Approve only after every verbatim part is delivered (ADR-0004) | **Broken on every re-send** (finding 1) |
| Typed `/kill` immediate (D14, AC 15) | **At risk**: not guaranteed while a chat turn streams (finding 2) |

## Findings

### 1. HIGH: `reissue()` gives every re-created approval `body_parts_total=1`, so a re-sent PR card can arm before its verbatim parts arrive
Where: CR §12.4 delta 5 ("signature extended by delta 7 only"), §12.4.3 (`reissue(cid, *, tg_chat_id, actor, body_parts_total=1, keep_expiry)`), §5.2, §5.12, §5.13, §8.1 `/approve`; ADR-0019 Option A "Re-sending a card". Code: `approvals.reissue`, `context.create_approval` (uses `cards.part_count` per action), `bot._maybe_arm`, `outbox.mark_sent`.

Verified in code:
- `reissue()` takes one integer, default `1`, and passes it to `create()` for **every** action of the contribution.
- `cards.part_count()` gives an `open_pr` card ≥ 2 parts (body parts plus the commit message).
- `reissue()` has no production caller today. It doesn't publish anything; `on_approval_created` is left to the caller.

The CR now routes five entry points through it: Show card, Show current, `/queue → Open`, `/approve` and Retry. It also re-sends siblings of different kinds in one call (`issue_claim` + `open_pr`, `push_update` + `comment`).

With a total of 1, `_maybe_arm` mints Approve as soon as the first part is delivered. Outbox id order usually sends the remaining parts first, but a 429 or a network backoff on part 2 puts the arm edit ahead of it. That breaks ADR-0004 ("Approve appears only after every body part was delivered"), CR §5.1, AC 5 and the `recover()` predicate. A count that is too high, for example the `open_pr` count applied to a one-part sibling, means Approve is never minted.

Fix: make the part count per action part of delta 7, and give the CR one wrapper that also publishes.
```python
# approvals.py
def reissue(self, cid: str, *, tg_chat_id: int, actor: str, part_count: Callable[[str], int],
            keep_expiry: bool = False) -> list[str]:
    ...
    for a in actions:
        new_ids.append(self.create(a["id"], tg_chat_id=tg_chat_id, body_parts_total=part_count(a["id"]),
                                   actor=actor, expires_at=inherited.get(a["id"])))

# context.py: the only caller used by Show card, Show current, /queue → Open, /approve and Retry
def reissue(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]:
    ids = self.approvals.reissue(cid, tg_chat_id=self.telegram_chat_id, actor=actor, keep_expiry=keep_expiry,
                                 part_count=lambda aid: cards.part_count(self.conn, aid))
    for aid in ids:          # after the transaction committed
        self.on_approval_created(aid)
    return ids
```
- Apply the same rule to `ProposalApprovals.reissue` (no parts) and `SkillApprovals.reissue` (skill part count).
- Update §12.4 delta 7, §12.4.3, §16 item 7 and ADR-0019 Option A.
- Add `test_reissue_part_counts.py`: claim (1 part) + `open_pr` (3 parts) → totals 1 and 3. With part 2 failing once, there is no Approve until part 2 is delivered.

### 2. HIGH: long Hermes turns (and other slow I/O) would block PTB's single update slot, delaying `/kill` and making Stop impossible
Where: CR §5.5 ("PTB runs one update at a time", used as a safety argument), §6.1 (streaming, Stop via `stopped_message_generation`), §6.5 (album window 1.5 s), §6.8 (Telegraph retries 2 + 8 s), §5.19 (GitHub title fetch), D14/AC 15 (typed `/kill` immediate), AC 18. Code: `bot.build_application` sets no `concurrent_updates`; the installed PTB 22.8 builds `SimpleUpdateProcessor(max_concurrent_updates=1)` (checked in the distro); handlers are blocking by default; FEAT-005 item 4's relay has no task model.

A handler that awaits a Hermes SSE answer holds the only update slot for the whole turn: up to minutes, with a 120 s idle timeout. During that time `/kill`, `/pause`, Approve, Confirm and the `stopped_message_generation` update all queue behind it:
- Stop can never stop the stream it targets.
- An album can't be collected over 1.5 s inside one handler.

The CR never says where a turn runs, and its §5.5 argument makes inline handling look like the intended model.

Fix: add a normative "update handler budget" rule to §6.1 and §12.
- **Bounded handlers.** Update handlers do only local work: DB transactions plus at most one Telegram call.
- **Background tasks.** These run as tracked tasks through `Handlers.spawn(coro, kind)`, like today's `_run_submit`: Hermes turns, media download and metadata stripping, Telegraph publish and unpublish, GitHub reads for `/work` and link cards, and live checks where possible.
- **One turn per session.** Use one `asyncio.Lock` per `session_key`. A second message while a turn is streaming answers "Still answering — tap ⏹ Stop or wait" (or queues one turn).
- **Kill and shutdown.** `/kill` and `Handlers.drain()` cancel the chat tasks, and the relay records `chat_turns.status='stopped'`.
- **Comment saves stay inline.** The comment-save + Confirm-panel refresh (§5.5) runs inline, so its race argument still holds.
- **New AC.** "With a fake Hermes stream open for 60 s, a typed `/kill` is handled within 2 s, and `stopped_message_generation` ends the stream within 2 s." Test it in `tests/telegram/test_handlers_nonblocking.py`.

### 3. MEDIUM: S12 `⬇️ Show current` on a card superseded by a new rev or proposal version resolves to "Superseded…"
Where: CR §5.12 table (`new_package_rev`, `new_proposal_version`, `new_skill_version` → `⬇️ Show current`), §5.16, §12.2 resolution table (Re-send class: no open approval for the token's `action_id` → "Superseded…"; proposals resolve by `(cid, version)`), ADR-0019 state diagram (`Stale --> Delivering: Show current`). Code: `packaging.py` cancels the old `open_pr`/`push_update` actions and inserts a new action at a new rev.

The stale card's token carries the old `{cid, rev, action_id}`. After `new_package_rev`, that action is `cancelled`, so resolution finds nothing and answers "Superseded…" instead of re-sending r3. Proposals fail the same way, because the old version never resolves. The button's purpose isn't named either.

Fix:
- Add the purpose `stale_show_current`, with arg `{cid}` (proposals `{cid, gate: 1}`, skills `{skill}`). It resolves by **contribution**, not by action:
  ```sql
  SELECT 1 FROM approvals a JOIN outbound_actions o ON o.id = a.action_id
   WHERE o.cid = :cid AND a.status IN ('pending','armed') LIMIT 1;
  ```
- When a row is found, call `AppContext.reissue(cid, keep_expiry=True)` (finding 1). Otherwise use the newest open `proposal_approvals` row for the cid and call `ProposalApprovals.reissue`. If neither exists, show the toast `No open card for c_… (state IMPLEMENTING)`.
- Render the button only when such an approval exists, else render `🔁 Retry` or nothing per the table.
- Update §12.2, §5.12 and §5.16.
- Extend AC 10: "after r3 is packaged, ⬇️ Show current on the r2 card re-sends the r3 card; on proposal v1 it re-sends v2".

### 4. MEDIUM: a reply to a `comment` or `issue_claim` card becomes a rev-less review comment whose Send triggers `CHANGES_REQUESTED`
Where: CR §4.4 rule 5 (kinds `card` and `body_part` → review comment when pending or armed), rule 7 (`[📝 Use as comment]` when "exactly one approval is pending"), §5.5 (drafts keyed by `(cid, rev)`; Send → `CHANGES_REQUESTED`), §14.2 (`review_comments.rev NOT NULL`). Code: `comment`/`issue_claim` payloads have no `rev` (`cards.view_for_action`); `handlers._reject` shows these kinds live in `WATCHING`, `VETTED` or `PACKAGING`, not `AWAITING_APPROVAL`.

The case: during watch, the user replies "make it shorter" to the drafted maintainer-reply card.
- The reply is stored with no rev.
- Sending the draft requests a re-implementation of the contribution, which is not what he meant.
- From `WATCHING` or `VETTED`, that transition is invalid.

These cards deliberately have no Request changes (§12.4.1), so the result is undefined behaviour reached through a common gesture.

Fix:
- Split rule 5 by the view's action kind:
  - `open_pr`/`push_update`: a review comment, as now;
  - `comment`/`issue_claim`: a confirm panel, `Replace the comment text with your reply? [✅ Replace] [↩️ Cancel]`. Replace runs FEAT-005's `edit_comment` path (normalization, `policy.check_text`, a new card; the old one goes S12 `text_edited`).
- Offer `use_as_comment` only when the single pending approval is an `open_pr` or `push_update`.
- Add an AC for both the confirm panel and the `use_as_comment` restriction.

### 5. MEDIUM: `reissue()`, Retry and typed `/approve` can create approvable cards for contributions that can't be submitted, and can kill a sibling mid-submission
Where: CR §5.12 (Retry "if the action is still `pending_approval`"), §8.1 (`/approve [cid]`, typed), §12.2 (Re-send and `stale_retry` rows), §12.4.3. Code:
- `reissue` selects actions in `pending_approval|approved` (`approved` is never written anywhere) and invalidates `pending|armed|confirmed` approvals;
- `state_machine` invalidates approvals on entering IMPLEMENTING but leaves the r2 action `pending_approval` until the next `/package`;
- `sweep_expired` moves a contribution to EXPIRED without cancelling its `open_pr` action;
- `submit._step0` transitions `AWAITING_APPROVAL → APPROVED`.

What goes wrong:
- **Revived cards.** A typed `/approve c_…` during S5 (r3 being built), or 🔁 Retry on an expired card after the 7-day sweep, mints a fresh approvable card for r2. Confirm then raises `TransitionError` inside `_step0`. The handler reports "🚨 Submission crashed", and the approval stays `confirmed`.
- **Killed sibling.** A Show card tap between a sibling's Confirm and its `consume()` (the submit task awaits network I/O) invalidates the confirmed sibling, so it fails with "Submission refused".

Fix: one eligibility predicate, used by `reissue`, `stale_retry` and `/approve`.
- **Eligible actions:** `status = 'pending_approval'` AND the contribution state is `AWAITING_APPROVAL` for `open_pr`/`push_update`. For `comment` and `issue_claim`, use the states the kind is created in (`WATCHING`, `VETTED`, `PACKAGING`, as in `handlers._reject`). Ineligible actions are skipped, and if none remain the toast reads `Nothing to re-send (state IMPLEMENTING)`.
- **Confirmed siblings:** if any approval of the cid is `confirmed`, refuse the whole re-send with `Submitting… try again in a moment` and never invalidate a confirmed approval from a re-send.
- **Expiry sweep:** the transition to EXPIRED cancels the contribution's `pending_approval` actions, so S12 drops 🔁 Retry.
- Add `test_reissue_eligibility.py` covering IMPLEMENTING, EXPIRED and confirmed-sibling.

### 6. MEDIUM: proposal Skip, Later and Never (and their Undo) have no defined effect on the decision row or the contribution state
Where: CR §5.15 (Skip → `⏭ Skipped` + `[↩️ Undo]` 10 min; Later → snoozed + Undo; Never → confirm), §12.4.2 ("Skip/Never/Later are kit actions"), §14.2 (`proposal_approvals.status`); CR-004 states `PROPOSAL_REJECTED`, `PROPOSAL_SNOOZED` with no edge back to `PROPOSED`.

The CR leaves three questions open:
- What status does the open `p_` approval take, and do its Start-work tokens die?
- Which CR-004 transition runs?
- How does Undo bring back a card with a working `✅ Start work`: the same approval, or a new one with which expiry?

CR-004 has no `PROPOSAL_REJECTED|SNOOZED → PROPOSED` edge, so Undo can't be implemented as written.

Fix:
- **Decisions.** Skip, Later and Never (after its confirm) call `ProposalApprovals.reject(approval_id, reason ∈ {skipped, snoozed, never_repo, never_kind})`. That sets the status to `rejected` and runs `_kill_tokens`. The contribution moves to `PROPOSAL_REJECTED` or `PROPOSAL_SNOOZED` (actor human, audited).
- **Undo** is a single-use kit token, valid 10 min, for Skip and Later only. It requires CR-004 to add human-only edges `PROPOSAL_REJECTED|PROPOSAL_SNOOZED → PROPOSED` within 10 min of the decision. It removes the snooze or never record, then calls `ProposalApprovals.reissue(cid, keep_expiry=True)`, which creates a new `p_` approval with the old expiry and a fresh card.
- **Fallback.** If CR-004 won't add the edge, drop Undo and say "re-request with /work <url>".
- Add an AC.

### 7. MEDIUM: in flat mode, the zero-setup default, Hermes output sits next to approval cards without the Reviews protection
Where: CR D1 and §4.1 (topics need the @BotFather setting; `telegram.yaml topics: auto`), U6 bullet 2 (flat mode: a 🤖 header line, body fully rendered), U12 ("flat mode: the assistant section with its 🤖 header"; "never next to approval cards"), AC 35, AC 48.

Topics must be turned on in @BotFather, so a fresh install runs in flat mode: approval cards, Hermes answers and feature cards share one chat. There, a prompt-injected rich answer with a `🧩 Ready for review · … ✅ CI passed` heading appears directly under real cards with only a 🤖 line above it. That is exactly what rule 7 prevents in ✅ Reviews (header plus quote). U12's guarantee can't hold in flat mode, and the CR doesn't say so.

Fix:
- **Rendering.** In flat mode, render every Hermes answer and every unsolicited Hermes message like rule 7. The gatekeeper header `🤖 <b>Assistant</b> · …` comes first, and the body sits inside a quote:
  - rich: every body block nested in one `blockquote` or `expandable_blockquote` block;
  - classic: `<blockquote expandable>`.

  Follow-up and feature buttons stay allowed, because they are gatekeeper tokens.
- **Docs.** Restate U6 and U12 for flat mode as "separation by rendering, not by topic". Have `docs/SETUP.md` recommend enabling topics.
- **AC.** Add a flat-mode case to AC 48.

### 8. NIT: the dedupe keys of disarm and S1 re-renders are unspecified, but `tg_outbox.dedupe_key` is UNIQUE forever
`Outbox.enqueue` uses `ON CONFLICT(dedupe_key) DO NOTHING`. If `disarm_expired`'s S1 edit reused `arm:<aid>` or a fixed `disarm:<aid>`, the second time-out of the same approval (Approve → wait → Approve → wait) would be silently dropped, leaving a dead Confirm panel.

Fix:
- Key the disarm edit as `disarm:<aid>:<armed_at>`.
- State in §12.2 that dedupe keys are permanent, so every repeatable edit must include a per-occurrence component.

### 9. NIT: §7.1 and §7.3 disagree on where quiet-hours P2 goes
§7.1 says P2 is "held for the digest". §7.3 and AC 17 say it is held until the window end and then batched into one message.

Fix: change the §7.1 cell to "held until quiet hours end, then batched (§7.3)".

### 10. NIT: `CardPublisher` dispatch for `p_` and `s_` cards, and housekeeping's access to it, are unspecified
`_card_sent` and `_maybe_arm` call `ctx.approvals`, `cards.view_for_approval` and `keyboards.card_rows(kind)`, all of which are PR-specific. `housekeeping_once(ctx)` gets only `AppContext`, while `CardPublisher` lives in `TelegramFrontend`.

Fix:
- `CardPublisher` resolves `set_card_message`, `issue_card_tokens`, the renderer and the keyboard through `ctx.decisions.for_id(aid)`.
- Add `AppContext.card_recover: Callable[[], None]` (set in `TelegramFrontend.start`, like `on_approval_created`) for `housekeeping_once`.

### 11. NIT: "`Invalidated` is unchanged" conflicts with the services returning `action_id=None` (and skills having no cid)
`Invalidated(approval_id: str, action_id: str, cid: str)` is typed under the strict mypy override for `ossbot.gatekeeper.*`.

Fix: `action_id: str | None` and `cid: str | None` (skills), with `AppContext.reissue_cards` skipping `None`. Alternatively, define a separate `DecisionInvalidated`.

### 12. NIT: the `tg_views.kind` CHECK doesn't list every kit view, and a CHECK failure inside `mark_sent` would cause duplicate sends
Several views have no kind in the CHECK list: `/queue`, `/settings`, `/size`, `/filters`, `/memory`, `/articles`, reminders and the Telegraph "Published" reply. If an insert fails inside `mark_sent` after a successful send, the row stays `pending` and is sent again every tick.

Fix:
- Map every view to a listed kind (pickers and lists → `picker`; others → `fyi`), or add the kinds.
- Validate `view_json.kind` with a pydantic `Literal` that mirrors the CHECK at enqueue time.
- Add a test.

### 13. NIT: `✂️ Split (adjust plan)` "pre-filled" ForceReply is not possible
ForceReply can't pre-fill text. Its only hint is `input_field_placeholder`, 1–64 characters, and the proposed text is 76.

Fix: send the fixed split feedback directly, as `Make it smaller` does. Then offer an optional ForceReply with the placeholder "Anything to add? (optional)".

### 14. NIT: forwarded third-party text becomes `/work` notes with the owner's authority, unseen
`🛠 Work on this` passes "the rest of the message text" as notes, and the link card never shows it. With a forward, that is someone else's text going to scout as "👤 requested by you".

Fix: when `forward_origin` is set, the notes are empty. Otherwise show `Notes: "<first 80 chars>"` on the link card.

### 15. NIT: falling back to classic while editing an existing rich message is unspecified
Explorer and file views are edited in place. If `editMessageText(rich_message)` returns 400, editing that message into a classic one may be impossible.

Fix: on a 400 for a rich edit, send the classic view as a new reply to the card, stub the old message, and update `tg_views`. Add a `[VERIFY]` for whether `editMessageText` can switch between rich and classic.

### 16. NIT: "feature topic" in the Telegraph eligibility rule is undefined
§6.8 says "the answer is not in a feature topic or in ✅ Reviews", but no "feature topic" exists.

Fix: replace it with "the thread is not ✅ Reviews and the turn produced no feature card (no `POST /v1/features/*/cards` for its `session_key` during the turn)", or drop the clause.

## Verified assumptions
- **Ids**
  - `ossbot.common.ids.new_id` accepts exactly one lowercase letter, and `ID_RE = ^[a-z]_[0-9A-HJKMNP-TV-Z]{10}\Z`, so `p_`/`s_` are valid.
  - Outbound actions use `x_` (`new_action_id`), matching §14.6.
- **Tokens and card purposes**
  - `issue_card_tokens` mints `CARD_PURPOSES` (which includes `edit_title`, `edit_body`, `clear_edits`) via `mint_token`, `single_use=True` by default.
  - `check_token` rejects used, expired, wrong-purpose and NULL-bound tokens. `use_token` consumes only single-use rows.
  - `_kill_tokens` touches only rows with that `approval_id`.
- **Approval lifecycle**
  - `cancel()` sets `pending`, clears `armed_at` and marks the `confirm` tokens used; the handler then calls `rotate_card_tokens` and edits directly.
  - `approve()` has no running check; `_approve` refuses paused, killed, draining and down with the toasts the CR quotes.
  - `confirm_text` produces exactly the four LIVE sentences and the DRY RUN sentence in §5.9.
  - `create(action_id, *, tg_chat_id, body_parts_total, actor)` matches §12.4.3's base signature.
  - `reissue(cid, *, tg_chat_id, actor, body_parts_total=1)`:
    - invalidates `pending|armed|confirmed` through `invalidate_contribution`;
    - re-creates approvals for `pending_approval|approved` actions with a fresh TTL;
    - has no production caller and doesn't publish.
  - `AppContext.create_approval` uses `cards.part_count` per action. `reissue_cards` re-creates only `pending_approval` actions and skips unknown action ids.
- **Outbox and card publishing**
  - `tg_outbox.dedupe_key` is `UNIQUE`; `enqueue` is `ON CONFLICT DO NOTHING`.
  - `mark_sent` counts parts in its own transaction.
  - `OutboxWorker.run_once` calls `on_sent` only for rows it sent in that call.
  - `_card_sent` enqueues `part:<aid>:<i>`, and `_maybe_arm` enqueues `arm:<aid>`.
  - `set_card_message` and `mark_part_delivered` are not keyed by the outbox row, and `mark_part_delivered` has no production caller.
- **Invalidation call sites:** `config_snapshot.py` (startup reconcile and the human reload); `control.py` (kill, resume-before-reconcile, resume-log, flag reconcile; `set_dry_run` separately); `packaging.py` `new_package_rev` + action cancel; `state_machine.py` `entered_implementing`, `fix_round_failed`; `handlers.py` `live_check:*`; `approvals.py` `text_edited`, `reissued`, `binding_changed`, `binding_unavailable:*`. All match §5.12 and delta 9.
- **Expiry and submission**
  - `sweep_expired`'s reference is the newest approval's `created_at`.
  - The move to EXPIRED doesn't cancel `open_pr` actions.
  - IMPLEMENTING invalidates approvals but leaves the old action `pending_approval`.
  - `submit._step0` requires `pending_approval` and transitions `AWAITING_APPROVAL → APPROVED`.
- **Runtime model**
  - `housekeeping_once` runs every 10 s (reconcile, `expire_due`, `sweep_expired`, retention) and receives only `AppContext`.
  - `db.transaction` joins an outer transaction without savepoints.
  - `build_application` doesn't set `concurrent_updates`, and the installed PTB 22.8 defaults to `SimpleUpdateProcessor(max_concurrent_updates=1)`, as the CR says.
- **Telegram plumbing**
  - `ALLOWED_UPDATES = ["message","callback_query"]`, and `TelegramSender` sends through `do_api_request`.
  - `escape._INVISIBLE` includes U+2060.
  - `auth.authorized` requires `effective_user`.
- **Notify API:** `api/notify.py` has no priority. `NotifyRequest` has only `text` (≤ 4,000), the role prefixes are 🤖/ℹ️, and the limit is 10/h.
- **Schema:** `ui_tokens.approval_id` has no foreign key, `purpose` is free text, `tg_outbox.status` has no CHECK, and `approvals.armed_at` exists.
- **Dependencies:** `pyproject.toml` pins PTB 22.8, Jinja2 3.1.6, pydantic 2.14.1 and httpx 0.28.1, so "no new runtime dependency" holds.
- **Plans:** FEAT-005 item 3 plans `/approve` as a same-approval reset; delta 5 states the override, and DESIGN §14.5 is amended. FEAT-005 item 4's relay specifies no task model.
- **Mockups:** every label in 00–12 fits §11.2 (the longest multi-button labels are `🙋 Ask maintainers first` = 24 and `⬜ Breaking change` = 17). Rows are ≤ 6, `callback_data` is 24 bytes, and the text budgets match §11.4.

## Unverified or wrong assumptions
- **Wrong:** "`reissue()` … signature extended by delta 7 only" is safe to reuse for every re-send. It carries one `body_parts_total=1` for all re-created actions (finding 1).
- **Wrong by omission:** the CR leans on one-update-at-a-time processing but never moves long Hermes turns out of the handler, so typed `/kill` and Stop are not immediate (finding 2).
- **Wrong:** S12 `⬇️ Show current` works for `new_package_rev` and `new_proposal_version`. The §12.2 resolution answers "Superseded…" (finding 3).
- **Wrong:** "Retry … if the action is still `pending_approval`" is enough. Actions stay `pending_approval` during IMPLEMENTING and after the EXPIRED sweep (finding 5).
- **Wrong:** "`Invalidated` is unchanged" with `action_id=None` returned by the new services (finding 11).
- **Wrong:** a ForceReply can be "pre-filled" (finding 13).
- **Unverified (not re-checked this round, no web access):** the Bot API 10.1–10.3 facts, Mini Apps Ed25519 validation and `sendData`, Telegram Serverless, the PyPI versions and dates, PTB #5261, aiogram #1890/#1840/PR #1848, the Telegraph API. They are cited in §19 and Addendum A.6, and round 2 checked them live.
- **Unverified:** "replies across topics are not supported (`ReplyParameters`)" (§4.1). It is plausible but not cited.
- **Unverified (already `[VERIFY]` in the CR or ADR, accepted as implementation-time checks):** rich rendering and quote-replies, draft rate accounting, effect IDs, `.html` opening, Hermes stream, stop and session endpoints, Kiro audio, the mirror permission minimum and probe codes, the Notifications API token type, the Telegraph host list, U+2060 defusing per client, EXIF on photos, label truncation.
