# ADR-0020: Telegram client library: keep python-telegram-bot 22.8 with a typed Bot API 10.1–10.3 adapter; no Pillow in the gatekeeper

Status: Accepted (2026-10-18; CR-003 approved and merged into DESIGN.md §14; design loop ended after round 6, remaining findings carried as build criteria in docs/ux/cr003-carried-findings.md; "CR-003 §N.x" in this ADR = DESIGN §14.(N+6).x). Was: Proposed (CR-003), revision 3 after design review round 5 · Date: 2026-10-18 · Facts verified on the web on 2026-10-18 (sources below) · Design: CR-003 §1, §6.1, §6.5, §12.5, §14.1, §14.4 · Related: ADR-0004, ADR-0019 · Supersedes: ADR-0019 Addendum A (moved here unchanged in substance; ADR-0019 keeps a back-reference)

## Context
The gatekeeper is the only Telegram poller (ADR-0004). One `getUpdates` long-polling stream carries Approve, Confirm, `/kill`, `/pause` and every assistant message, so the client library sits on the safety-critical path.

The code today is built on python-telegram-bot (PTB) 22.8:
- `telegram/bot.py`: `TelegramSender`, `PollTrackingRequest`, `build_application`;
- `handlers.py`: PTB `CallbackQueryHandler` and `MessageHandler`;
- `auth.py`: PTB `Update`;
- the fake-Bot-API tests in `tests/telegram`, which inject a `BaseRequest`.

CR-003 needs Bot API 10.1–10.3 features: rich messages and rich drafts, `can_stop`/`keep_on_stop`, the `stopped_message_generation` update, `DisabledButton`, `force_reply` on inline keyboards, and `RichTextButton` in incoming rich messages. It also needs the 9.3/9.4 private-chat topics. **PTB 22.8 supports Bot API 10.0 and none of the 10.1+ features** (issue #5261, paused until PTB's v23 refactor).

The user lifted the "no new runtime dependency" rule and asked for an evaluation of aiogram and pyTelegramBotAPI. A swap is acceptable if the code is refactored cleanly.

## Decision criteria
In priority order:
1. **Unknown-field tolerance on the long-polling path.** One new field, a new union member or a missing optional field in any update must never stop `getUpdates`. If it does, `/kill` and Approve stop working until a rebuild.
2. **Bot API 10.1–10.3 coverage**, typed or reachable, for everything CR-003 uses.
3. **Licence.** It must be compatible with open-sourcing ossbot later under any licence (DESIGN, ADR-0015).
4. **Migration cost and risk.** The Phase 1-reviewed transport, auth, poll-health and test code is the baseline.
5. Maintenance, bus factor and how fast new Bot API versions are adopted. This criterion breaks ties only.

## Candidates (current facts)

| | python-telegram-bot | aiogram 3.x | pyTelegramBotAPI (telebot) |
|---|---|---|---|
| Latest release | 22.8 (2026-06-12) | 3.31.0 (2026-08-26) | 4.37.1 (2026-10-17) |
| **Bot API 10.1–10.3 coverage** | **None.** 10.0 only. Issue #5261 "Full Support for Bot API 10.1" is open; on 2026-06-18 the maintainers paused it "until we complete major refactors" (v23). Community PRs #5263, #5275, #5356 and #5359 are pending. Reachable through `Bot.do_api_request`, which the outbox already uses for every send | Full 10.3, typed (3.29.0 = 10.1 on 2026-06-14, 3.30.0 = 10.2 on 2026-07-17, 3.31.0 = 10.3 on 2026-08-25) | Full 10.3, typed (4.35.0 = 10.1 on 2026-07-12, 4.36.0 = 10.2 on 2026-07-18, 4.37.0 = 10.3 on 2026-09-21), e.g. `Update.stopped_message_generation` → `MessageGenerationStopped` |
| **Unknown or new fields in updates** | **Lenient.** In the v22.8 source, `TelegramObject._de_json` moves keys that are not constructor parameters into `api_kwargs` instead of raising. Unknown `Update` keys such as `stopped_message_generation` survive there | **Strict.** Models are pydantic with `extra="allow"`, so unknown *fields* pass. But a new **union member**, or a missing required field, fails validation of the whole `Response[list[Update]]`. #1890: the 10.3 `RichTextButton` raised `ClientDecodeError` and "the polling offset never advances", blocking the bot until the upgrade to 3.31. #1840 (a `MessageOriginHiddenUser` without `sender_user_name` blocks polling) is open, and the generic fix, PR #1848 (skip invalid updates), is unmerged | Lenient. `de_json` reads known keys with `obj.get(...)` and drops the rest (they are not kept) |
| **Licence** | LGPL-3.0-only: fine as an unmodified dependency of a project under any licence | MIT | **GPL-2.0**: distributing ossbot with it ties ossbot to GPL-2.0-compatible terms (not Apache-2.0, not GPL-3.0-only) |
| **Migration cost from `src/ossbot/gatekeeper/telegram/`** | **None** | High: `bot.py`, `handlers.py`, `auth.py`, the poll-health tracking and every Telegram test rewritten against aiogram's dispatcher and router model | High: the same, against a less typed API |
| asyncio beside FastAPI over UDS | Native asyncio `Application`, already running next to FastAPI | Native asyncio; a good fit | `AsyncTeleBot` exists (aiohttp); the sync `TeleBot` is the primary API |
| Test and mocking support | Injected `BaseRequest`, which `tests/telegram` already uses | A custom `BaseSession` or aiohttp mocking; tests to rewrite | Monkeypatching `apihelper`; tests to rewrite |
| Adoption speed of new Bot API versions | Slow now (10.1 still missing after 4 months) | Fastest: 1–3 days | 3–4 weeks |
| Maintenance and bus factor | Team project. Top contributors: Bibo-Joshi (532 commits), leandrotoledo (526), jh0ker (486), tsnoam, jsmnbom. Dependency and security updates land weekly (Oct 2026) | One dominant maintainer: JrooTJunior, 1,662 commits vs 91 for the next | Two active maintainers: Badiboy (1,027), coder2020official (892) |

## Options
1. **Keep PTB 22.8 and add a thin typed adapter:** `telegram/rawapi.py` plus `telegram/ui/blocks.py`.
   - Request models: pydantic with `extra="forbid"`, one per 10.1–10.3 method and field we send.
   - Response models: `extra="ignore"`, for the few results we read.
   - Calls go through `Bot.do_api_request`, as the outbox already does.
   - Incoming 10.x shapes (`stopped_message_generation`) are read from `update.api_kwargs` with a pydantic model.
2. **Switch to aiogram 3.31.** It has full typed 10.3 today, the fastest adoption and the MIT licence. But its strict decoding has already blocked long polling twice in 2026 (#1890 fixed by an upgrade, #1840 still open). ossbot pins exact versions, so every future Bot API union addition could stop Approve, Confirm and `/kill` until a rebuild. Its bus factor is 1.
3. **Switch to pyTelegramBotAPI 4.37.** Typed 10.3 and tolerant decoding. But GPL-2.0 constrains the open-source-later licence, the async API is secondary, and the migration cost is as high as aiogram's.
4. **Own minimal client** (httpx, our pydantic models and our own poll loop). Maximum control, and tolerant by design. But it rewrites the transport, retry, rate-limit and poll-health code that PTB already provides and that Phase 1 reviewed. Option 1's adapter gives the same typing for the new features without that.

### Scoring against the criteria

| Criterion | 1 PTB + adapter | 2 aiogram | 3 pyTelegramBotAPI | 4 own client |
|---|---|---|---|---|
| 1 Unknown-field tolerance | ✅ keeps unknown keys (`api_kwargs`) | ❌ union additions block polling (#1890, #1840) | ✅ drops unknown keys | ✅ by design |
| 2 Bot API 10.1–10.3 | ✅ through the typed adapter (about a dozen methods and fields) | ✅ native | ✅ native | ✅ through our own models |
| 3 Licence | ✅ LGPL-3.0, unmodified | ✅ MIT | ❌ GPL-2.0 | ✅ ours |
| 4 Migration cost | ✅ none | ❌ high | ❌ high | ❌ high, and it replaces reviewed code |
| 5 Tie-breakers | ⚠️ slow Bot API adoption; strong team | ⚠️ bus factor 1 | ⚠️ secondary async API | ⚠️ we maintain everything |

## Decision
**Option 1: keep python-telegram-bot 22.8, and reach Bot API 10.1–10.3 through the typed adapter `telegram/rawapi.py`.** aiogram, pyTelegramBotAPI and an own client are rejected.
- **Tolerance first.** Lenient decoding matters most for a long-polling safety bot: one unknown field must never stop the update stream that carries `/kill`. PTB has it. aiogram has shown in production that it lacks it.
- **No migration.** The Phase 1-reviewed transport, auth and tests stay as they are.
- **Licence.** LGPL-3.0, used as an unmodified dependency, keeps every licence option open for ossbot.
- **Bounded adapter.** The 10.1–10.3 surface CR-003 uses is about a dozen methods and fields. When PTB v23 ships typed 10.1+ support, the adapter's call sites switch to typed methods behind the same kit API, as a separate reviewed change.
- **Update processing stays sequential.** The application keeps PTB's default `concurrent_updates` (one update at a time; `SimpleUpdateProcessor(max_concurrent_updates=1)` in the installed 22.8). Long work runs outside the update slot, under CR-003 §12.5's update-handler budget. Back-to-back processing means a message and a `/kill` from one `getUpdates` batch can cancel a spawned task before it starts, so the budget puts every task's cleanup (freeing its session, closing its `chat_turns` row) in the done callback registered by `Handlers.spawn`, which asyncio runs for every outcome. That rule, like the rest of the budget, is a property of asyncio and of sequential dispatch, not of PTB, so it holds whichever library is used later.
- **Revisit triggers:**
  - PTB v23 is released: switch the adapter to typed calls.
  - PTB still lacks 10.1 support by 2027-04 **and** aiogram merges tolerant `getUpdates` (#1848 or an equivalent): re-run this evaluation.

## Pillow: not added to the gatekeeper
**Decision: drop (not added) from the gatekeeper and from every component that holds keys. It is allowed only inside `--network none` sandbox images for phase-2 image tools.**
- **Resizing isn't needed.** The only needs are metadata stripping and resizing for vision input. Telegram already supplies re-encoded JPEG `PhotoSize` variants, and the gatekeeper picks the largest one ≤ 1,600 px. Image *documents* are capped at 5 MB, so they need no resizing either.
- **Stripping needs no decoding.** A pure-Python container walker (JPEG markers, PNG chunks, WebP RIFF chunks; `chat/imgmeta.py`, CR-003 §6.5) removes EXIF, XMP and text chunks without decoding pixels. No image decoder runs in the TCB that holds the approval keys. Pillow 13.0.0 is current, and image decoders have a long CVE history. The walker is fuzzed with Hypothesis.
- **HEIC** needs a Pillow plugin anyway. It is rejected with "send it as a photo".
- **Image tools that do decode** (OCR, background removal; phase 2, CR-003 §13 item 12) run in a `--network none` sandbox job, whose image may include Pillow.

## Consequences
- **Good:**
  - No migration, and lenient decoding on the safety-critical poller.
  - Every licence option stays open.
  - Pillow stays out of the TCB.
- **Costs:**
  - Raw Bot API calls for 10.1–10.3 features, behind `rawapi.py` (pydantic models), until PTB v23.
  - A test per raw method checks the request model against the documented field list.
  - `stopped_message_generation` is read from `api_kwargs`, not a typed attribute.
- **Risks:**
  - If PTB v23 slips for a long time, the adapter grows. The revisit trigger bounds that.
  - A Telegram change to a field we *send* is caught by `extra="forbid"` request models in tests, not at runtime.

## Verification items
1. End to end: `stopped_message_generation` arrives through PTB 22.8 in `api_kwargs` and is accepted in `allowed_updates`. The PTB side is verified from source; CR-003 §17 item 3 covers the rest.
2. Every `rawapi.py` request model is checked against the live Bot API reference at build time (fields, types and optionality, 10.3).

## Responses to design review round 4
- **Decision unchanged.** The review found the evaluation complete (every criterion scored, Pillow decided). The library stays python-telegram-bot 22.8 with the typed `rawapi.py` adapter; aiogram, pyTelegramBotAPI and an own client stay rejected; Pillow stays out of the gatekeeper.
- **Finding 2 (task cancelled before it starts):** recorded under "Update processing stays sequential" above. The fix is in CR-003 §12.5 (`spawn(…, on_done)` and the session map) and does not depend on the library.
- **Finding 3 (`command` task kind):** CR-003 §12.5 only; no library impact.

## Responses to design review round 5
- **Decision unchanged.** The review confirmed the library decision (keep python-telegram-bot 22.8 with the typed `rawapi.py` adapter; aiogram, pyTelegramBotAPI and an own client rejected) and the Pillow decision (not in the gatekeeper; allowed only in `--network none` sandbox images).
- **No library impact.** The round-5 fixes (the hold-back for Stop partials and truncated answers, the PR title as publish bytes, the shared secret scanner used by packaging, the whitespace-pattern load check, the comment-refresh re-render after `/reload`, the Telegraph announcement after `/kill`, the U12 time bound and the token-use amendment note) are gatekeeper logic in CR-003 §24. The Telegraph `asyncio.shield` and done-callback behaviour they rely on is plain asyncio, not PTB.

## Sources (accessed 2026-10-18)
- PyPI JSON for python-telegram-bot, aiogram, pyTelegramBotAPI and Pillow (versions, upload dates, licence classifiers): https://pypi.org/project/python-telegram-bot/ , https://pypi.org/project/aiogram/ , https://pypi.org/project/pyTelegramBotAPI/ , https://pypi.org/project/Pillow/
- PTB #5261 and its comments (the pause on 2026-06-18; pending PRs): https://github.com/python-telegram-bot/python-telegram-bot/issues/5261
- PTB v22.8 source, `TelegramObject._de_json` and `Update.de_json`: https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/_telegramobject.py , https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/_update.py
- PTB `ApplicationBuilder.concurrent_updates` (default: one update at a time): https://docs.python-telegram-bot.org/en/v22.8/telegram.ext.applicationbuilder.html#telegram.ext.ApplicationBuilder.concurrent_updates , and the installed 22.8 `telegram/ext/_applicationbuilder.py`
- PTB releases: https://github.com/python-telegram-bot/python-telegram-bot/releases
- aiogram releases (Bot API level per version): https://github.com/aiogram/aiogram/releases ; base model config (`extra="allow"`): https://github.com/aiogram/aiogram/blob/v3.31.0/aiogram/types/base.py
- aiogram #1890, #1840 and PR #1848: https://github.com/aiogram/aiogram/issues/1890 , https://github.com/aiogram/aiogram/issues/1840 , https://github.com/aiogram/aiogram/pull/1848
- pyTelegramBotAPI releases and `types.py` (`Update.de_json` with `stopped_message_generation`): https://github.com/eternnoir/pyTelegramBotAPI/releases , https://github.com/eternnoir/pyTelegramBotAPI/blob/master/telebot/types.py
- Contributor counts: the GitHub contributors API for each repository
- Bot API changelog (10.3 on 2026-08-24 is the latest; `RichTextButton`, `DisabledButton`, `force_reply`, `can_stop`, `MessageGenerationStopped`): https://core.telegram.org/bots/api-changelog

## Implementation addendum (FEAT-009, PLAN item 44, 2026-10-19)
Checked against the live Bot API reference (https://core.telegram.org/bots/api; Bot API 10.3 of 2026-08-24 is still the current version on 2026-10-19) before `telegram/rawapi.py` and `telegram/ui/blocks.py` were written (DESIGN §14.23, §23 item 32):
- **Request fields.** `sendRichMessage` (chat_id, message_thread_id, rich_message, disable_notification, protect_content, message_effect_id, reply_parameters, reply_markup; plus business, ephemeral and suggested-post fields ossbot never sends), `sendRichMessageDraft` and `sendMessageDraft` (`draft_id` must be non-zero; `can_stop` and `keep_on_stop` are 10.3), and `editMessageText` with `rich_message`. `tests/telegram/test_rawapi.py` pins each request model to the documented field list.
- **`InputRichMessage`**: exactly one of `blocks`, `html`, `markdown`; `skip_entity_detection` turns off URL, mention, hashtag and bot-command detection. ossbot's model has no `html`/`markdown` field and always sets `skip_entity_detection: true` (U1).
- **Blocks used**: paragraph, heading, pre (`language`), footer, divider, list, blockquote (`blocks`), `expandable_blockquote`, details (`summary`, `blocks`, `is_open`). RichText is a string, an array, or bold/italic/code/url/date_time objects; `date_time_format` follows `r|w?[dD]?[tT]?`, the same rule as the classic `<tg-time format>`.
- **§14.23 item 16 (partial):** `InputRichBlockExpandableBlockQuotation` carries one `text: RichText`, not nested blocks; only the plain `blockquote` nests blocks. The flat-mode and Reviews quote wrapper (`answer_view`, FEAT-010) uses `blockquote` for multi-block answers or the classic `<blockquote expandable>`, as DESIGN allows.
- **Reply markup**: `InlineKeyboardButton.style` (`danger|success|primary`), `copy_text` (`CopyTextButton`) and `disabled` (`DisabledButton`, no fields); `InlineKeyboardMarkup.force_reply` (10.3) can't be changed when the keyboard is edited, so ForceReply prompts set it when they are sent.
- **Incoming** `stopped_message_generation` = `MessageGenerationStopped {chat, message_thread_id?, draft_id}` with no `from` (U10), parsed from `Update.api_kwargs`.
- **getMe**: `has_topics_enabled` and `allows_users_to_create_topics` are returned only by `getMe`. PTB 22.8 types both (checked in the installed `telegram/_user.py`), but ossbot reads them through `rawapi.MeResult` (`extra="ignore"`) so the topic decision has one code path.
- **Classic `<tg-time unix format>`** is a documented parse-mode-HTML tag; the dashboard footer uses the relative format `r`.
- PTB warns "Please use 'Bot.<method>'" for raw calls of methods it knows. `TelegramSender` silences only that warning: one raw code path for every outbox method is deliberate.

## Implementation addendum (post-v1, 2026-10-20): where the dashboard pin lands (owner check O1)

Owner check O1: Threaded mode on, the system topics were created, but the pinned dashboard showed in "All", not in 🚦 Control.

Research (2026-10-20):
- Bot API reference, `pinChatMessage` (https://core.telegram.org/bots/api#pinchatmessage, re-read 2026-10-20): the parameters are `business_connection_id`, `chat_id`, `message_id` and `disable_notification`. There is **no `message_thread_id`**; in private chats every non-service message can be pinned and notifications are always off.
- `unpinAllForumTopicMessages` (https://core.telegram.org/bots/api#unpinallforumtopicmessages) clears the pinned messages of one topic "in a forum supergroup chat or a private chat with a user", so a private chat's topics keep their own pinned lists.
- Bot API changelog (https://core.telegram.org/bots/api-changelog): 9.4 added topics in private chats; up to 10.3 no version added a thread parameter to `pinChatMessage`.
- Secondary reports (hermes-agent issue 22022, openclaw issue 79455, 2026-05) say Bot API 10.0 sometimes rejected `message_thread_id` in private topics ("message thread not found"), so replies fell back to the main view. Treated as a hint only.

Decision:
- The pin follows the message: a dashboard is pinned only when its `sendMessage` result carries Control's `message_thread_id`. The result's `message_thread_id` and `is_topic_message` are recorded in the dashboard's `tg_views.state_json` (`sent_thread`), and the pin is logged `dashboard_pinned thread=<id>`.
- With Threaded mode on, a dashboard whose result is outside Control (the thread was not resolved) is retired, not pinned, and re-sent into Control once per gatekeeper start (`dashboard_resent_to_control`). A second miss in the same start is pinned where it landed, and in flat mode the dashboard is pinned in the one chat as before. A pin failure is still logged `dashboard_pin_failed`.
- If the pinned message is in Control and a client still shows the pin in "All" only, that is how the client displays topic pins; the Bot API offers no way to change it. The pin is kept (tapping it jumps to the dashboard in Control) and no second copy is pinned. This is documented in README, SETUP "Threaded mode" and FINAL_REPORT O1. Which of the two causes the owner saw is visible after a redeploy in the gatekeeper journal (`scripts\ossctl.ps1 logs gatekeeper -Grep dashboard_`).
- Tests: `tests/telegram/test_dashboard.py` (`test_threaded_mode_pins_only_a_dashboard_in_control`, `test_pin_failure_is_logged`; flat mode in `test_dashboard_is_sent_pinned_and_golden`).
