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

## Implementation addendum (post-v1, 2026-10-21): commands typed in "All" (owner check O1, PV1-A3)

Owner re-check O1: with Threaded mode on, `/status` typed in "All" left a new empty topic named "/status", and the card landed in All. In 🚦 Control it answered in place.

Research (2026-10-21):
- Bot API reference (https://core.telegram.org/bots/api, re-read 2026-10-21) and changelog 9.3 (https://core.telegram.org/bots/api-changelog): `message_thread_id` and `is_topic_message` are valid in private chats with topics; `ForumTopic` and `ForumTopicCreated` (https://core.telegram.org/bots/api#forumtopiccreated) carry `is_name_implicit`, "True, if the name of the topic wasn't specified explicitly by its creator and likely needs to be changed by the bot". Telegram creates a topic, named from the text, for a message typed in the main view; the bot cannot prevent that, because the topic exists before the update arrives. The docs do not say which thread fields such a message carries. `direct_messages_topic` belongs to channel direct-messages chats, not bot private chats.
- `deleteForumTopic` (https://core.telegram.org/bots/api#deleteforumtopic) deletes a topic with all its messages "in a forum supergroup chat or a private chat with a user"; admin rights are needed only in supergroups. It needs the topic's `message_thread_id`, and no method lists a private chat's topics. `sendMessage` without `message_thread_id` lands in All; `0` is not documented for private chats and is not used. Nothing documents that a `reply_parameters` reply inherits the replied message's topic, so ossbot does not rely on it.
- python-telegram-bot 22.8 types `ForumTopicCreated.is_name_implicit` (checked in the installed `telegram/_forumtopic.py`).
- Secondary, hints only: openclaw issue 19144 (inbound `message_thread_id` + `is_topic_message` for such messages, https://github.com/openclaw/openclaw/issues/19144); Telegram bug 58238 (mobile clients omit `message_thread_id` on a new topic's first message, https://bugs.telegram.org/c/58238); tdlib/telegram-bot-api issue 847 (Bot API 10.0 "message thread not found", not seen by ossbot).
- Journal (`scripts\ossctl.ps1 logs gatekeeper -Since 3d`): incoming thread fields were not logged; the `/status` reply was accepted (no "thread not found"), so it was sent without a thread id.

Cause: `handlers._incoming` dropped `message_thread_id` unless `is_topic_message` was set, and no code handled Telegram's implicit topic.

Decision:
- A command's reply target is `topics.Topics.command_target`: no thread id → All; a system topic → that topic (never deleted); a topic Telegram named implicitly (`reply_to_message.forum_topic_created.is_name_implicit`) whose first owner message is this command (`tg_thread_seq` n == 1) → All, then `deleteForumTopic` on it through the outbox, queued after the reply (a permanent failure logs `stray_topic_delete_failed` with the error class and is never retried); any other thread id → that thread, whether or not `is_topic_message` is set. Flat mode is unchanged. Chat routing (`Incoming.thread_id`) is unchanged; the raw facts travel in `Incoming.raw_thread_id`, `is_topic_message` and `implicit_topic`.
- Every owner message logs `tg_thread_fields` (message id, `message_thread_id`, `is_topic_message`, `reply_topic_created`, `is_name_implicit`, `has_direct_messages_topic`, `is_command`, the key names of unknown fields; never the text, chat id or user id) and every command logs `command_reply_target` (`where=all|thread|system|stray|flat`, `reply_thread`, `delete_thread`), so the next owner check shows the real update shape.
- Limit: if a client omits `message_thread_id` (bug 58238), the bot cannot learn the new topic and the empty topic stays; send commands in 🚦 Control. The log shows this as `thread=none`.
- Tests: `tests/telegram/test_status_is_dashboard.py` (All, All with an implicit topic with and without `is_topic_message`, Control, Reviews, a user topic, `is_topic_message` absent, a second message in an implicit topic, `/help` and `/title` from All, flat mode, delete failure, the log without text) and `tests/telegram/test_topics_fallback.py` (`test_command_target*`).

### Second fix (2026-10-21): the real update shape and the correlation

The first fix (4724ac7) did not work live. The owner's re-check and the journal (`tg_thread_fields`, 2026-10-20T19:03-19:16Z, five attempts) show that a command typed in All arrives as **two updates**, about 300 ms apart:
- Update A, message N: `message_thread_id` = a new thread id, `is_topic_message` true, no text. After the second fix, the log identifies it as the `forum_topic_created` service message (`content_keys=["forum_topic_created"]`, `from_is_owner` true, `from_is_bot` false, `topic_created={name_len: 6 or 7, name_is_command: true, icon_color: 7322096, has_icon_custom_emoji: false, is_name_implicit: true}`).
- Update B, message N+1: the command, with no `message_thread_id` and `is_topic_message` false. It has no `reply_to_message`, so the flag the first fix read is never present.

Bot API (re-read 2026-10-21, https://core.telegram.org/bots/api and https://core.telegram.org/bots/api-changelog):
- `deleteForumTopic` (https://core.telegram.org/bots/api#deleteforumtopic) works in "a private chat with a user" (9.3). Rights are needed only in supergroups.
- `closeForumTopic` (https://core.telegram.org/bots/api#closeforumtopic) and `hideGeneralForumTopic` are supergroup-only. A private chat has no close or hide fallback, so a refused delete is logged and left alone.
- 9.4 added bot-side `createForumTopic` and a @BotFather setting that stops users creating and deleting topics (`User.allows_users_to_create_topics`). 10.1-10.3 changed nothing about topics.

Decision:
- `telegram/implicit_topics.py` correlates the pair in either order. A pair needs the same chat and sender, B's message id == A's + 1, a gap of at most 2 s, and A's topic name equal to B's command (`/cmd@botname` allowed) or `is_name_implicit`.
- The pending side lives in memory only: at most 64 entries per side, a 2 s TTL, and replay and done guards. The reply is never held: B is answered first, and the delete is queued when the pair completes.
- Update A is offered only for a thread the gatekeeper had never counted (`tg_thread_seq`), never a system topic, and only in topics mode. Before the delete, the code re-checks that the thread is not a system topic and holds no bot send (`tg_outbox`).
- The delete goes through `rawapi.DeleteForumTopic` and the outbox (dedupe `topic_delete:<thread>`, so at most once). A failure logs `stray_topic_delete_failed` with the thread, the error class and Telegram's reason, and is never retried.
- Logs carry no text or topic name: `implicit_topic_pending|ignored|expired|match` (order, `gap_ms`, `name_matches_command`, `is_name_implicit`, action) and `stray_topic_deleted`.
- Tests: `tests/telegram/test_implicit_topics.py`.

Live result (2026-10-21, after `bootstrap.ps1 -Only provision`): the owner sent `/status` in All several times. All three journal pairs matched (`order=topic_first`, `gap_ms` 314, 1255 and 317, `name_matches_command=True`, `implicit=True`, `action=delete`) and each was followed by `stray_topic_deleted`, with no failure. The card landed in All, and the topic disappeared within about 2 s. Client cache: the Telegram client may keep showing a deleted topic until the chat is left and reopened. That is client rendering, not server state, and the bot cannot affect it.

## Implementation addendum (post-v1, 2026-10-21): can the bot prevent the implicit topic in "All"? (research and live probe)

The owner asked for prevention, not delete-afterwards. `/chat <subject>`, and extending the cleanup to plain text, were to be built only if prevention exists for **commands and plain text typed in All** (`.ossbot-local/postv1/followups.md`).

**Research (2026-10-21).** Client source was read at pinned commits (tdesktop `78ceaa54`, Telegram-Android `0aae9598`, Telegram-iOS `f1dd7a2d`, TDLib `175048b6`, hermes-agent `5a282c6c`, openclaw `bead2a8e`). Everything fetched was treated as untrusted data and nothing was run. Findings are paraphrased.

Sources checked (all on 2026-10-21 unless noted):

| # | Source | Finding |
|---|---|---|
| 1 | Repo: the owner's follow-up note for this run | `/chat` and "nothing in All creates a topic" only if prevention exists and is proven live; otherwise keep 9884df9. |
| 2 | Repo: `git show 9884df9`, `telegram/implicit_topics.py`, `handlers.py` | Live shape: update A (message N) is `forum_topic_created` with `is_name_implicit` in a new thread; update B (N+1) is the command without `message_thread_id`; paired within 2 s and the thread deleted. |
| 3 | Repo: this ADR, addenda of 2026-10-20 and 2026-10-21 | Bot API 9.3/9.4 topic fields, `deleteForumTopic` in private chats, close/hide supergroup-only. Refinement: the bot cannot stop the client creating a topic for a non-reply message, but it can try to make the next message a reply. |
| 4 | Repo: `telegram/ui/send.py`, `telegram/rawapi.py` `ForceReply` and `InlineKeyboardMarkup.force_reply` | ossbot already sends ForceReply and inline + `force_reply` (10.3) for prompts; no new API surface needed. |
| 5 | https://core.telegram.org/bots/api-changelog (9.3, 9.4, 10.1-10.3) | 9.3 private-chat topics and `is_name_implicit`; 9.4 bot-side `createForumTopic` and the @BotFather setting behind `User.allows_users_to_create_topics`; 10.3 `force_reply` on inline and reply keyboards; 10.2 ephemeral messages are group-only. No setting or send parameter stops implicit creation while users may create topics. |
| 6 | https://core.telegram.org/bots/api#forcereply | ForceReply asks the client to show a reply interface to the bot's message; it has `input_field_placeholder` (1-64 chars) and `selective`; valid in private chats. |
| 7 | https://core.telegram.org/bots/api#sendmessage | Only `reply_markup` (ForceReply or a keyboard) can influence the user's next send; `message_thread_id` and `reply_parameters` only place the bot's own message. |
| 8 | tdesktop `history_widget.cpp` `HistoryWidget::prepareSendAction` | In a bot forum where users create topics, a reply uses the replied item's topic; only a non-reply reserves a new bot topic. |
| 9 | tdesktop `history_item.cpp` `topicRootId`, `apiwrap.cpp` | An off-thread item maps to the general id, sent as `top_msg_id = 0`: a reply to an off-thread bot message stays off-thread. |
| 10 | tdesktop `history_widget.cpp` `replyTo()` and the force-reply keyboard branch | A pending ForceReply makes the composer a reply by default; dismissing the reply bar clears it. |
| 11 | tdesktop `history_item.cpp` `definesReplyKeyboard`, `bot_keyboard.cpp` | Inline markup with `force_reply` counts as a reply keyboard on Desktop. |
| 12 | tdesktop `data_histories.cpp` `sendCreateTopicRequest`, `data_peer.cpp` `IsBotUserCreatesTopics` | The client creates the topic (`messages.createForumTopic` with `title_missing`) and sends into it; with `bot_forum_can_manage_topics` off it shows "Off-thread message" and creates none. |
| 13 | tdesktop `history_widget.cpp` `sendBotCommand` | Command links and reply-keyboard taps overwrite the prepared reply target; from reading alone unclear whether a topic is created (not relied on). |
| 14 | Telegram-Android `BotForumHelper.java` `beforeSendingFinalRequest` | Intercepts only when the bot forum view is on and users may manage topics; a request carrying a reply is sent unchanged; otherwise it creates a topic titled `text[:16]+"..."` (`title_missing`) and re-points the message. Matches the A-then-B journal shape. |
| 15 | Telegram-Android `ChatActivity.java` `checkBotKeyboard`, `TLKeyboardHelper.isForceReply` | ForceReply (plain or keyboard with `force_reply`) opens the reply panel; Android remembers an answered ForceReply per dialog. |
| 16 | Telegram-Android `ChatActivityEnterView.java` | A reply-keyboard press in a bot forum is a reply only if the user is already replying: a plain ReplyKeyboard does not prevent a topic. |
| 17 | Telegram-iOS `ChatControllerNode.swift` | With a reply target the message goes to the replied message's thread (off-thread for All); without one it goes to a new topic created with `isTitleMissing` and title `text.prefix(16)`. |
| 18 | Telegram-iOS `MessageUtils.swift` `requestsSetupReply`, `UpdateChatPresentationInterfaceState.swift` | Plain ForceReply sets the reply subject; inline markup with `force_reply` does not on iOS at this commit. |
| 19 | TDLib `telegram_api.tl`, `UserManager.h` | `user.bot_forum_view`, `bot_forum_can_manage_topics`, `title_missing` on topic creation: the server only records what the client asked for; no server-side "auto topic" flag. |
| 20 | https://github.com/openclaw/openclaw/issues/169443 (and PR #169444) | Same two-update shape from Android; OpenClaw adopts the root message into the new topic within 3 s (keeps the topic). No prevention. |
| 21 | https://github.com/openclaw/openclaw/issues/169969 (issue of 2026-10-12) | Asks for commands in the root DM and topics for chat; calls the All view Telegram behaviour; open. No prevention. |
| 22 | openclaw #7352, #19144, #6417, #6597 (web search results) | Threaded DM mode makes each new message a new thread; the rest is reply routing. No prevention. |
| 23 | NousResearch/hermes-agent `gateway/run_topics.py`, `gateway/slash_commands_session.py` | Root DM treated as a lobby (reminder, recovery to the last topic, optional auto-transfer); `/topic` refuses when users may not create topics. No prevention. |
| 24 | NousResearch/hermes-agent #22022, #2598 (web search results) | "Message thread not found" fallback to All, and the feature request. No prevention. |
| 25 | MaksBloxX/Telegram-Thread-Vault README (web search result) | Recommends Threaded mode on with user topic creation off: the only prevention seen in the wild (the @BotFather toggle). |
| 26 | python-telegram-bot, aiogram, grammY, telegraf, pyTelegramBotAPI issue trackers and code | Nothing about implicit topics in private chats; the frameworks only type `is_name_implicit` and `allows_users_to_create_topics`. |
| 27 | GitHub code search `force_reply allows_users_to_create_topics` | Only Bot API type definitions; no project uses ForceReply to keep All free of topics. |
| 28 | https://bugs.telegram.org/c/58238, https://bugs.telegram.org/c/59731 (search snippets) | 58238: mobile clients omit `message_thread_id` on the first message of a new topic (our update B); 59731: on iOS a new bot-DM topic sometimes misses the sidebar. No avoiding setting. Card bodies seen as snippets only. |
| 29 | Coding-agent Telegram bridges (pavel-molyanov/telegram-ai-agent, chatopic, a topic-per-agent gist; web search) | Use forum supergroups or one topic per conversation; none handles All in bot DMs. |
| — | Not checked | Telegram Web K/A (`morethanwords/tweb`, `Ajaxy/telegram-tt`): `bot_forum_view` seen but not traced; unofficial clients not traced; full bugs.telegram.org card text not fetchable. |

Candidates, ranked:
1. **ForceReply "reply-anchored All".** Every bot message in All carries ForceReply, so the owner's next message is a reply and the client skips topic creation (rows 6, 8-10, 14-15, 17-18). Caveats: iOS honours only plain ForceReply, not inline + `force_reply`, so a card with buttons would need a separate anchor; the reply is consumed once used and must be re-armed; dismissing the bar or typing before the anchor arrives still creates a topic; owner messages show a reply quote. No production use found (row 27).
2. **"Allow users to create topics" OFF in @BotFather.** Clients then never create a topic from All, and `/chat` could use bot-side `createForumTopic` (rows 5, 12, 14, 17, 25). Cost: the owner can no longer create or delete topics in the client; Hermes's `/topic` refuses this mode (row 23). The only approach used in production; rejected because the owner keeps the setting ON.
3. **Manual reply.** The owner replies to a bot message before typing; same mechanism as 1, zero code, relies on habit. Used as the probe control.
4. **Desktop command links and reply-keyboard taps** (row 13). Speculative and Desktop only (Android creates a topic for keyboard taps, row 16).

Rejected: `message_thread_id` 0 or 1 on bot sends (places only the bot's message); `hideGeneralForumTopic` and `closeForumTopic` (supergroup-only); a plain ReplyKeyboard (Android still creates a topic, row 16); ephemeral messages (group-only, row 5); Mini App or Web App input (no-exposure invariant); OpenClaw-style adoption (the opposite of the goal at the time; see the owner decision below, which later adopted it for plain text only).

**Live probe (2026-10-21, 01:30-01:35Z).**
- **Setup:** a temporary, flag-guarded `/probe_all` in the gatekeeper, deployed with one `bootstrap.ps1 -Only provision`. It sent a 7-step chain of `ForceReply` messages to All and logged `reply_to_message_id` (ids and flags only). Replies to probe messages skipped the delete, so created topics stayed visible.
- **Session state:** `getMe` was `has_topics_enabled` true and `allows_users_to_create_topics` true (`probe_all_start mode=topics allows_user_topics=True`).
- **Journal:** `ossctl.ps1 logs gatekeeper`, events `tg_thread_fields`, `probe_all_reply`, `implicit_topic_*` and `stray_topic_deleted`. Message ids:

| Owner action in All | Message | `reply_to_message_id` | `forum_topic_created` before it | Result |
|---|---|---|---|---|
| `/probe_all` typed (control) | 605 | none | 604, thread 125089 | topic created, then deleted (9884df9) |
| `/status` typed after step 1 arrived | 608 | none | 607, thread 125092 | topic created, then deleted |
| tapped the `/status` command link inside the step-1 bot message (606) | 610 | **606** | **none** | **no topic** |
| "probe two" typed | 613 | none | 612, thread 125097 (name_len 9) | topic created and kept (plain text is never deleted) |
| `/help` typed | 616 | none | 615, thread 125100 | topic created, then deleted |
| "probe four" typed | 619 | none | 618, thread 125103 (name_len 10) | topic created and kept |
| `/probe_all` typed again | 622 | none | 621, thread 125106 | topic created, then deleted |
| `/status` typed | 625 | none | 624, thread 125109 | topic created, then deleted |

- **ForceReply:** the reply bar did **not** open by itself on the owner's client, so no typed message went out as a reply.
- **Command link:** a tapped `/command` link inside a bot message was sent as a reply to that message (610 → 606), and no topic was created.
- **Stray topics:** 125097 and 125103 still exist. They should be deleted once, by hand or by the bot, only after the owner confirms.

**Result: prevention for typed input does not work.**
- **Typed text:** a message typed in All without a reply always creates an implicit topic, whether it is a command or plain text, and ForceReply is not a reliable way to turn it into a reply.
- **What does prevent a topic:** actions started from the bot's own UI. These are inline-keyboard buttons (a callback sends no message) and `/command` links inside bot messages, which the client sends as replies (msg 610).

**Immediate consequence (before the owner decision below):**
- Typed commands in All keep 9884df9's delete-and-answer and no `/chat` topics are added.
- Proposal for the plan step: make the common commands tappable in All, and treat the implicit topic of typed plain text as that conversation's own resumable Hermes session, with no deletion.
- The probe code was removed from the working tree after the run.

### Owner decision (2026-10-21, after the probe)

The owner approved the proposal on 2026-10-21, after the probe (relayed by the parent session; it supersedes "if prevention isn't possible, change nothing in behaviour"): build tappable command shortcuts, keep the 9884df9 delete for typed commands, and turn the implicit topic of typed plain text into its own resumable Hermes session that is never deleted, all behind `telegram.yaml` flags; no `/chat`; the stray probe topics stay. Built on main, DESIGN §14.10.5:
- **Shortcuts (3544da9 flags, 46677cf shortcuts).** Inline buttons send no message, so they create no topic; a tapped `/command` link is sent as a reply and creates none either (msg 610 above). New actions `sc_status`, `sc_work`, `sc_size`, `sc_filters`, `sc_model`, `sc_help` (`telegram/shortcuts.py`) plus the existing `dash_queue`, `dash_pause` and `dash_resume`. They run the same command functions as typing. Placement: two extra dashboard rows `[🛠 Work][📏 Size][🔎 Filters]` / `[🤖 Model][❓ Help]` (pinned card and `/status` copies), a button menu on `/help`, and a compact `[🚦 Status][📥 Queue][❓ Help]` row on command replies sent to All that have no rows of their own (so `/queue`, which has its sort row, gets none). Kill keeps its two-tap and stays on the dashboard cards only; Resume and Model open their existing confirm.
- **Typed commands in All:** unchanged (9884df9: answered in All, their implicit topic deleted).
- **Typed plain text in All (364f6fe).** The message is paired with its update A (`forum_topic_created`, B = A + 1, gap ≤ 2 s, same chat and sender, `is_name_implicit` or A's name a prefix of the text after stripping a trailing `...`/`…`). It is re-pointed at A's thread and answered there as a user topic with session `tg-<uid>-t<thread>`, so the owner can come back later and continue. Adopted topics are never deleted (`_delete_implicit` refuses them). If B arrives first, the rest of the handling runs in a spawned task that waits at most 2 s; without A in time the message is answered in All exactly as before. Albums follow their first item. The path runs only in topics mode and only when `getMe` reports `allows_users_to_create_topics` (read at start: with it off the client creates no topic, row 12, so B would only time out). Links and photos typed in All follow the same path.
- **Flags** (`config/telegram.yaml`, `TelegramConfig`): `all_text_mode: topic_session | legacy` (default `topic_session`) and `command_shortcuts: true | false` (default `true`). **Revert:** set `all_text_mode: legacy` and/or `command_shortcuts: false`, then a human-confirmed `/reload`; or `git revert 364f6fe 46677cf` (the flags commit 3544da9 can stay). With both flags off every golden equals the previous output.
- **Why `/chat <subject>` was not built.** Typing `/chat x` in All would itself create an implicit topic that then has to be deleted, the create-then-delete UX the owner rejected. Typing the subject as plain text already gives a topic named after it with its own session.
- **VERIFY V7** (https://core.telegram.org/bots/api#replyparameters, #sendmessagedraft): `sendMessageDraft` takes `message_thread_id`; `allow_sending_without_reply` is documented as always false for replies in another forum topic; the docs do not say whether B (delivered without `message_thread_id`) counts as inside A's thread. Every reply already sends `allow_sending_without_reply` true (`ui/send.py`), so no code change. Docs checked; live confirmation is the owner's O1 re-check.
- **VERIFY V8** (https://core.telegram.org/bots/api#inlinekeyboardmarkup): `callback_data` is 1-64 bytes (ours: 24); the Bot API sets no per-row or per-message button count. `ui/limits.py` (3 per row, 6 rows) is stricter and is kept, hence the dashboard's two shortcut rows instead of one.
- **VERIFY V9** (rows 12, 14, 17 and the probe's `name_len` 9 and 10): Android titles `text[:16]+"..."`, iOS the first 16 characters, short texts in full, Desktop `title_missing` (pairing relies on `is_name_implicit` there, as for commands). This fixes the prefix rule.
- No design assumption turned out false.
- **Stray probe topics** 125097 and 125103 are left for the owner; they are deleted once, only after he confirms.
- **Deployed probe:** `/probe_all` is gone from the deployed copy (`/opt/ossbot/app`) since the retention redeploy at 75a4b10, which shipped the tree without it. Shortcuts and topic sessions reached the live bot at the close-out redeploy (2026-10-21, 84334be); the V7 live answer is part of the owner's O1 re-check.
- Tests: `tests/telegram/test_shortcuts.py`, `tests/telegram/test_all_topic_sessions.py`, `tests/telegram/test_implicit_topics.py` (text side), `tests/unit/common/test_telegram_config.py`.
