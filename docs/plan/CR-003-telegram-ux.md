# CR-003: Telegram user experience and the PR review flow

Status: Proposed, revision 2 (user request, 2026-10-18; revised after design review round 1, `docs/ux/cr003-review.md`, and to present CR-004). Applies on top of DESIGN.md, CR-001, CR-002 including Amendment A, and CR-004 (proposal gate and change-size budget). Where this CR conflicts with DESIGN §14 (Telegram design) or ADR-0004, this CR wins and both must be updated to match. The review-surface decision is **ADR-0019**; the Telegram client-library decision is ADR-0019 **Addendum A** (to be filed as ADR-0020, §20). Mockups, one per key screen, are in `docs/ux/` (index in §18). Responses to every review finding are in §20.

## 0. Summary
The bot is Amsal's daily driver for two things: approving OSS contributions and using Hermes as a general multimodal assistant. Today the UX is a flat chat with one decorated card per candidate and escaped text replies. This CR makes it a structured workspace:
- **Topics:** Control, Reviews, OSS feed and Assistant, plus user-created chat topics.
- **A pinned live dashboard.**
- **A review flow you can do on a phone:** explorer, per-file diffs, line comments, interdiff, a mode-labelled two-tap confirm, and a live PR status card.
- **Two human gates (CR-004):** a proposal card before any implementation (gate 1) and the PR preview card (gate 2), with the size estimate shown against the actual diff (code and test lines split; an overrun is highlighted, not blocked), user-directed `/work` and forwarded links, and `/size` and `/filters` pickers for autonomous discovery.
- **A streaming assistant** with sources, follow-ups and feature cards.
- **Notification discipline:** priorities, quiet hours, batching and a digest.
- **Pickers instead of typed arguments.**
- **Clear offline and error states.**
- **A small UI component kit** so later features render consistently.

The deterministic approval path keeps its rules: the same token format, binding and Confirm sentences. The six code deltas the UI needs in the token layer (non-approval tokens without an approval id, a confirm-expiry disarm, the new card purposes, separate skill and proposal services, `reissue()` as the one `/approve` semantics, and atomic token binding at send) are listed in §12.4 and are the only exceptions to §16. The default needs zero extra setup. Richer layers, the private GitHub review mirror (ADR-0019 option C) and phase-2 items, are opt-in.

## 1. What the research changed
Model knowledge was stale. Everything below was checked against the live documentation on 2026-10-18 (sources in §19). "PTB" means python-telegram-bot 22.8, the pinned version and the latest release (PyPI, 2026-06-12), which fully supports Bot API 10.0. The current Bot API is 10.3 (2026-08-24). PTB's native 10.1 work (issue #5261) is paused until its v23 refactor, so 10.1–10.3 methods and fields go through `Bot.do_api_request`, which the outbox already uses for every call (`telegram/bot.py: TelegramSender`), with typed pydantic payload models in `telegram/ui/blocks.py` and `telegram/rawapi.py`. The library was re-evaluated now that new dependencies are allowed: PTB stays, aiogram 3.31 and pyTelegramBotAPI 4.37 were rejected (ADR-0019 Addendum A).

| Capability (Bot API version) | What ossbot uses it for | PTB 22.8 | Limits and notes |
|---|---|---|---|
| Rich messages: `sendRichMessage`, `sendRichMessageDraft`, `editMessageText(rich_message)` (10.1–10.3) | Read-only views: explorer, file diffs, interdiff, tests, assistant answers, feature cards | **No**: raw call | ≤ 32,768 UTF-8 characters, ≤ 500 blocks, ≤ 16 nesting levels, ≤ 50 media, ≤ 20 table columns. Three input forms: `blocks`, `html` or `markdown`. Rich Markdown "can contain arbitrary HTML", including `<tg-button>`, so **only `blocks` is used** for interpolated content (U1). Entities are auto-detected unless `skip_entity_detection` is set. There is no `link_preview_options` parameter |
| Rich blocks: `details`, `expandable_blockquote`, `pre` + `language`, `table`, `anchor`, `buttons` (10.2–10.3) | Collapsible hunks and logs, numstat tables, hunk anchors | No | In-body `tg-button`s are **not used** for any decision (U3) |
| `sendMessageDraft` (9.3; all bots since 9.5) with `can_stop`/`keep_on_stop` (10.3); `stopped_message_generation` update (10.3) | Assistant streaming with a Stop button | Draft yes; `can_stop` and the update **no** | Private chats only. The draft is a **30-second ephemeral preview**: the final text must be sent with `sendMessage`/`sendRichMessage`. Draft text is 0–4,096 characters; empty text shows "Thinking…" |
| Topics in private chats: `createForumTopic`, `message_thread_id`, `getMe.has_topics_enabled`, `getMe.allows_users_to_create_topics` (9.3/9.4) | Information architecture (§4.1) | Yes (`allows_users_to_create_topics` arrives in `api_kwargs` if PTB lacks the field) | Enabled per bot in @BotFather (Mini App setting, 9.4). Topic names are 1–128 characters. The bot can stop users creating or deleting topics |
| Button `style` = `success`/`danger`/`primary` (9.4) | Approve green, Confirm LIVE red, Kill red | Yes | Colour is never the only signal (§11) |
| `DisabledButton` (10.3) | Stale cards ("⛔ Not approvable") | **No**: raw | |
| `force_reply` on `InlineKeyboardMarkup` (10.3) | Comment prompts that also need buttons | **No**: raw | Can't be changed by a later keyboard edit |
| `date_time` entity / `<tg-time>` (9.5) | Every timestamp; relative "updated 3 min ago" that keeps ageing while the laptop sleeps | Parsed server-side | Format `r` (relative) or `w?[dD]?[tT]?` |
| Expandable blockquote, `pre` with a language (classic HTML) | Classic fallback for all views | Yes | Languages per libprisma (includes `diff`) |
| Ephemeral messages (10.2/10.3) | **Not used** (§8.3) | No | Designed for groups. `message_id` is 0, delivery is not guaranteed, and they may disappear on app restart |
| Guest mode (10.0), inline mode | Off (phase-2 hook, §13) | Yes | |
| `setMessageReaction` | Acknowledge received media with 👀 → 👌 (or 🤷 on unsupported input) | Yes | One reaction per message for bots; only the `ReactionTypeEmoji` list is allowed (✅ is not on it) |
| `message_effect_id` | Optional 🎉 on "merged" | Yes | Private chats only. Effect IDs are not documented in the Bot API; **[VERIFY]** |
| Checklists (`sendChecklist`) | **Not usable** | Yes | Business accounts only. Rich task-list items are used for display instead |
| Menu button, `setMyCommands` with 7 scopes | Owner-scoped command menu that changes with control state | Yes | ≤ 100 commands; command 1–32 `[a-z0-9_]`; description 1–256 |
| Pinned messages | Live dashboard | Yes | Any message can be pinned in a private chat; pin notifications are always off there |
| `copy_text` button | Copy title, branch, PR URL | Yes | 1–256 characters |
| `TextQuote` on replies | Line-level review comments | Yes | `text` + `position` (UTF-16) |
| General limits | | | Text 4,096 and caption 1,024 characters after entity parsing; `callback_data` 1–64 bytes; `answerCallbackQuery` text ≤ 200; about 1 message/s per chat; unfetched updates kept ≤ 24 h; bot download ≤ 20 MB, upload ≤ 50 MB |
| Mini Apps | Phase-2 only (ADR-0019 B3) | Partial | Init data is verified by HMAC with the bot token **or** by Ed25519 `signature` with Telegram's public key (no token needed). `DeviceStorage` 5 MB per user; `SecureStorage` 10 items (Keychain/Keystore). Methods are refused from foreign origins since 2026-07-20. `Telegram.WebApp.sendData` works **only** for Mini Apps launched from a reply-keyboard `web_app` button, carries ≤ 4,096 bytes and closes the app; inline-button and menu-launched Mini Apps can't use it |
| Telegram Serverless (2026-10-06) | **Rejected** for this bot | n/a | BotFather manages a webhook, and `getUpdates` is refused while a webhook is set. That breaks ADR-0004 (ADR-0019) |
| Telegraph API (`api.telegra.ph`) | Opt-in public articles for long assistant answers only (§6.8) | n/a (plain HTTPS) | Pages are **public to anyone with the URL**. The page path is derived from the title and date (`Title-MM-DD`), so URLs are guessable unless the title carries randomness. Methods: `createAccount` (returns an `access_token`), `createPage`, `editPage`, `getPage` (no token), `getViews`, `revokeAccessToken`. There is **no delete**: blanking via `editPage` is the only option. Content ≤ 64 KB; title 1–256 characters. Tags: `a aside b blockquote br code em figcaption figure h3 h4 hr i iframe img li ol p pre s strong u ul video`. The only attributes are `href` and `src`, so there is no syntax highlighting, no tables and no collapsible blocks. Pages open as Instant View inside Telegram |

Prior art:
- `SanNw/opencode-telegram` keeps the agent server on loopback. It tunnels only an authenticated Bridge, protected by Telegram-signed init data, an owner check and a P-256 trusted-device key, and its HTML previews are static and sandboxed.
- `RichardAtCT/claude-code-telegram` uses an allowed-users list and an optional inbound webhook server.

ossbot keeps the owner-only rule and the static, script-free previews. It does **not** add an inbound component by default.

## 2. Decisions
- **D1 Topics.** When `getMe().has_topics_enabled` is true (the user turns topics on in @BotFather), the gatekeeper creates and uses four system topics: 🚦 Control, ✅ Reviews, 📈 OSS feed, 💬 Assistant. A topic the user creates is a separate Hermes conversation. With topics off, everything works in one flat chat with section headers (§4.1).
- **D2 Commands.** Menu button = `MenuButtonCommands`. Commands are set only for `BotCommandScopeChat(owner)`; `BotCommandScopeDefault` is empty. The menu is re-set when control state changes; in killed mode it holds only `/status /resume /logs /help /start` (`/settings` is hidden while killed, §4.2).
- **D3 Dashboard.** One pinned message in Control, edited in place: at most once per 30 s and only when its content hash changes (§4.3).
- **D4 Review surface (ADR-0019).** Native Telegram by default: classic card plus rich read-only views with classic fallback. The private GitHub mirror is opt-in. The Mini App is phase-2. Serverless is rejected.
- **D5 Approval surfaces.** The approval card and Confirm stay classic HTML messages whose reply-markup tokens are bound to the message ID. They are never rich, never drafts, never ephemeral, and never use in-body buttons (U3).
- **D6 Comments.** Review comments are reply/quote messages and hunk-picker prompts. They collect into a draft review and are sent as one change request.
- **D7 Interdiff.** Every revised candidate shows a gatekeeper-computed interdiff first. It is display-only.
- **D8 Assistant streaming.** `sendRichMessageDraft` (or `sendMessageDraft` when rich is unavailable) with `can_stop`. The final message is sent as a rich message built from Hermes Markdown by our own converter, never through Telegram's Markdown or HTML modes (U1).
- **D9 Follow-up buttons.** Allowed only as gatekeeper tokens whose single effect is to send the button's text to Hermes as the user's next message. The relayed text is **byte-for-byte the visible label** (≤ 64 characters), so nothing the user did not see is sent with his authority. This amends ADR-0004 ("never with buttons") (U2).
- **D10 Feature cards.** Hermes supplies typed data; the gatekeeper validates it and renders the card. Hermes never supplies markup or buttons.
- **D11 Notifications.** Four priorities (P0–P3), quiet hours, `/quiet` and `/notify`, coalescing and batching, plus a daily and weekly digest. P0 is never silenced (U7).
- **D12 Arguments.** Every command argument has an inline-keyboard picker. Typed arguments still work and are validated the same way.
- **D13 Confirmations.** Destructive and "go" actions use in-place confirm panels and toasts. Ephemeral messages are not used (§8.3).
- **D14 Kill.** Typed `/kill` stays immediate. The dashboard 🛑 button needs a second tap within 10 s.
- **D15 Offline signal.** The pinned dashboard's relative `<tg-time>` keeps ageing on the client while the laptop is off. There are two named "down" states (CR-001), and only one of them is offline:
  - **`💤 DOWN (workers)`**: Telegram `/down`. Workers, Hermes and sandboxes stop; the gatekeeper keeps polling and answering. The short description stays "🟢 Online".
  - **`💤 DOWN (graceful)`**: full down (`down.ps1` / `ossctl down`), the gatekeeper itself stops. Only this state sets the "💤 Offline since…" short description.

  On reconnect, pending updates are processed with their age shown.
- **D16 Component kit.** All rendering goes through `ossbot.gatekeeper.telegram.ui`: card, list, pager, confirm, picker, progress, toast, doc, rich builder. Features register through an entry-point registry.
- **D17 Telegraph articles** (user suggestion, evaluated in §6.8). Off by default (`telegraph.enabled: false`). When enabled, `📖 Open as article` is offered **per message** only on eligible assistant answers, and publishing needs a tap plus a confirm tap. The gatekeeper publishes through a new egress category, after secret and personal-data scans. Telegraph is never used for review content, digests, images, health or nutrition data, or anything else. There is **no automatic publishing**: a length threshold only decides whether the button is offered. This overrides the "automatic above a threshold" option, because a public, undeletable publication must be a deliberate act, and rich messages already show answers up to 32,768 characters in the chat.
- **D18 Proposal gate (CR-004 incl. Amendment B).** Gate 1 is a classic, message-bound **proposal card** in ✅ Reviews, with the same two-tap, single-use token mechanics as the PR card. `✅ Start work` is the only button that advances the pipeline; it binds the proposal hash, its version and the estimated class. Adjustments produce a new proposal version, sent as a reply to the old card with a deterministic version diff. The PR preview card (gate 2) shows the estimate against the gatekeeper-measured diff, with code and test lines split (`+142 / −30 code · +210 test`). An overrun is **highlighted on the preview card, not blocked** (`size.on_overrun: flag`, the default). A `RESCOPE_REQUIRED` card exists only in the opt-in `reapprove` mode, and a ceiling-hit card only when the hard `policy.diff` ceiling is reached (§5.15–§5.18).
- **D19 Size preference and filter tuning (CR-004 Amendment B).** Size classes are soft estimates and ranking preferences for **autonomous** discovery only. `/size` sets `size.autonomous_max_class` (`xs`…`xl`), `/filters` sets the discovery filters, and `/repos <repo>` sets per-repo overrides; all three affect autonomous discovery only. The hard ceiling (`policy.diff`: 80 files, 3,000 code lines, 3,000 test lines) is shown read-only and is changed only in config (§8.4).
- **D20 Telegram library.** python-telegram-bot 22.8 stays the transport and dispatcher; Bot API 10.1–10.3 calls go through a thin typed adapter over `do_api_request` (ADR-0019 Addendum A). Pillow is **not** added to the gatekeeper: metadata stripping is a pure-Python segment walker that never decodes pixels (§6.5).
- **D21 Hermes-originated messages** (feature cards, notifications, reminders) go only to 💬 Assistant or user chat topics, always marked 🤖 in the header line, and their target thread is resolved from the gatekeeper's own `chat_turns` row, never from Hermes input (U12).
- **D22 User-directed work (CR-004 Amendment B).** `/work <url> [notes]`, a forwarded or pasted GitHub link with a `🛠 Work on this` button, and `ossctl work <url>` create items marked `👤 requested by you` on every card. They skip ranking, daily caps, the size preference and the filters, and still pass both gates, forbidden paths, the AI-contribution ban, the CLA confirmation, the one-open-PR-per-repo check (overridable with a confirm) and the hard ceiling (§5.19).

## 3. Safety invariants
Unchanged and binding (DESIGN I1–I9, ADR-0004, ADR-0014):
- **S1** Approve, submit, resume, dry-run off, skill activation and proposal Start work (CR-004 gate 1) are deterministic gatekeeper actions. Each is two taps (Approve, then Confirm) on opaque `t:<22 chars>` tokens (24 bytes): single-use, expiring, bound to `tg_message_id`, and accepted only when `from.id` is the owner in the owner's private chat. No LLM output, chat text, Mini App, GitHub review or mirror event can approve.
- **S2** Every approval binds the exact commit SHA, the derived patch hash and the title/body hashes, plus the mode (`dry_run` recomputed at Approve, at Confirm and before every write). Any edit, dry-run toggle, reload or new rev invalidates it. A proposal approval (CR-004) binds the proposal hash, its version and the approved size class; a new proposal version, an expiry or a reload invalidates it.
- **S3** Nothing public happens before approval. Dry-run is on by default, and the mode is printed on every **public-action** card (`open_pr`, `push_update`, `comment`, `issue_claim`) and in its Confirm sentence. Cards for local changes (skill activation, proposal Start work) also show the mode badge, marked "n/a: local change", and their Confirm says dry-run does not apply (§5.15, §9), so the badge is never missing but never implies a publish.
- **S4** No inbound exposure by default. Any internet-reachable component (phase-2 Mini App) must meet all of these: optional, off by default, read-only, Ed25519 init-data verification, no secrets, no bot token, never able to approve.

New in this CR. Each invariant names the layer that enforces it:

| # | Invariant | Enforced by | Why there |
|---|---|---|---|
| U1 | **Untrusted text is data, never markup.** This covers Hermes output, issue and PR text, logs, skill text, mirror comments and feature payloads. Classic messages escape it (`escape.esc`, `make_visible`, `defuse_commands`). Rich messages carry it as plain strings in `blocks` with `skip_entity_detection: true`. Telegram `markdown`/`html` rich modes and MarkdownV2 are never used with interpolated content | `telegram/ui/` renderers; an import-linter contract keeps `gatekeeper.chat` and `gatekeeper.features` from calling the outbox except through `telegram.ui` | One choke point that can be tested exhaustively |
| U2 | **Only the gatekeeper mints buttons.** Every `callback_data` is a gatekeeper token. Labels from untrusted sources are sanitized (§11.3) and never contain approval words. A follow-up token can only relay its stored text to Hermes; it never reaches the command registry | `telegram/ui/kit.py` `button()`, `chat/followups.py` | Hermes could otherwise forge a convincing "Approve" |
| U3 | **Approval and Confirm surfaces are classic messages with reply-markup tokens bound to their message ID.** They are never drafts, never rich, never ephemeral, never in-body buttons. Approve (and the proposal's `Start work`) appears only on the card | `telegram/cards.py`, `telegram/proposal_cards.py`, `CardPublisher` | Keeps the reviewed token paths; the only deltas are listed in §12.4. Ephemeral messages have `message_id` 0 |
| U4 | **Display artifacts are never bound or trusted.** Rich views, interdiff, HTML diff and mirror commits are all derived from `derived/<cid>/<rev>/`. Only `patch_sha256`, `head_sha` and the title/body hashes are bound | `review/` modules read only the derived directory | ADR-0014 |
| U5 | **The mirror can write only to the configured private repo.** The token is scoped by GitHub to that repo and proven by the enable-time probe. The writer refuses any other path. The repo must be private with Actions disabled before every push. Mirror content never goes upstream. GitHub reviews never approve | GitHub token scoping + `review_mirror_writer.py` (import-linter: only `review_mirror.py` imports it) + `review_mirror.py` pre-push checks | Defence in depth: token, code and runtime check |
| U6 | **Model-authored text is labelled.** Planner rationale, risk claims, self-review, "coder says" and Hermes text are prefixed 🤖 and shown in a quote. Gatekeeper-verified facts carry ✅ and no 🤖 | `telegram/ui/kit.py` `claim()` vs `fact()` | Prompt injection must not look like verification |
| U7 | **P0 alerts are never silenced** by quiet hours, `/quiet` or `/notify` | `telegram/notify.py` | Safety |
| U8 | **Bot-command links in untrusted classic text are defused.** U+2060 WORD JOINER goes after `/` when a letter follows. This is display only and is never applied to bytes that will be published (those are always in `<pre>`). Because U+2060 is also in `escape._INVISIBLE`, any text that re-enters the gatekeeper (a quote copied into a comment, an echoed follow-up, a `Use as comment` source) first has the defuse marker removed by `sanitize.undefuse()`: every U+2060 that directly follows `/` is deleted **before** `make_visible`, so the user never sees `⟦U+2060⟧` from our own marker. Any other U+2060 is still shown | `escape.defuse_commands`, `ui/sanitize.undefuse` | A tap on a link like "/dryrun" inside a Hermes answer would send that command |
| U9 | **Opt-in surfaces stay off until confirmed.** The mirror, Telegraph articles and the phase-2 Mini App are enabled only through `config/telegram.yaml` plus a human-confirmed `/reload`, plus a passing doctor probe | config snapshot (DESIGN §7.9) | Security-relevant config never changes silently |
| U10 | **Topic routing never changes authorization.** Every update still needs `from.id == owner`, `chat.type == private` and `chat.id == owner`. `message_thread_id` decides presentation and routing only. **One exception:** `stopped_message_generation` (`MessageGenerationStopped`) has no `from` field, so it is checked by `authorized_chat_only()`: `chat.type == "private" and chat.id == owner`, read from `update.api_kwargs` (§6.1). Its only effect is to stop a stream that the owner's own message started, and it is accepted only for an active `draft_id` | `telegram/auth.py`: `authorized()` unchanged; new `authorized_chat_only()` used by that one handler | A stop update can't carry a user, and a private chat whose id equals the owner's id can only be the owner's |
| U11 | **Nothing is published to the web except approved GitHub actions and explicitly confirmed Telegraph articles.** An article can be created only by the gatekeeper, from an eligible `chat_answer` view (§6.8), after a publish tap and a confirm tap, and after the secret and personal-data scans pass. LLM components can't reach Telegraph at all (`telegra.ph`, `api.telegra.ph` and `edit.telegra.ph` are always denied for them) | `gatekeeper/telegraph.py` (the only module importing `telegraph_writer.py`, import-linter) + egress categories | The publish is public and irreversible, and its token stays with the deterministic component |
| U12 | **Hermes-originated messages can't choose where or how loudly they land.** Feature cards, Hermes notifications and reminders go only to 💬 Assistant or user chat topics (flat mode: the assistant section), never to ✅ Reviews, 🚦 Control or 📈 OSS feed. Each carries 🤖 in its header line (§6.7). The target thread comes only from the gatekeeper's own `chat_turns` row for the calling session (§6.7). Hermes P1 is capped at 5 per day, then sent as P2 | `gatekeeper/api/notify.py`, `api/features.py`, `telegram/notify.py` | A prompt-injected Hermes must not be able to place lookalike content next to approval cards or wake the user at night |
| U13 | **The size ceiling can't be changed from Telegram** (CR-004 Amendment B). `policy.diff.max_files / max_changed_lines / max_test_lines` have no picker, no token purpose, no typed argument and no `/reload` shortcut; they change only by editing `config/policy.yaml` plus the normal human-confirmed `/reload`. `/size`, `/filters` and `/repos … size` change only autonomous-discovery preferences (`xs`…`xl`; `xxl` is never selectable) and never touch an approved proposal, a user-directed item, a gate or the ceiling | `gatekeeper/discovery_prefs.py` (the only writer of preferences; rejects ceiling keys) + the strict config schema (CR-004) | The ceiling is a runaway-agent guard, not a preference; a prompt injection or a mis-tap must not move it |

## 4. Information architecture

### 4.1 Topics (D1)
At startup the gatekeeper calls `getMe`.

If `has_topics_enabled` is true:
- It ensures the four system topics exist and stores their `message_thread_id`s in `tg_topics`:
  - 🚦 Control (icon colour 0x6FB9F0)
  - ✅ Reviews (0x8EEE98)
  - 📈 OSS feed (0xFFD67E)
  - 💬 Assistant (0xCB86DB)
- Any other thread is an assistant conversation, created by the user or by `/new`.
- The recommended BotFather setting (the @BotFather Mini App topic setting, Bot API 9.4) lets users create topics, since each new topic is a new chat with Hermes. The gatekeeper reads `getMe().allows_users_to_create_topics` at startup; when it is false, `ossctl doctor` warns "users can't create chat topics: enable it in @BotFather → Bot Settings" and `/new` still works (the bot creates the topic itself).

**Where replies go.** A command reply, picker or toast-backed edit is sent in the **originating** `message_thread_id` (the topic the command was typed or tapped in), through `telegram.ui.send`. Today `handlers._send_reply` and `handlers._edit` call PTB directly; they are rewritten to build a `View` and send through the kit, so topic routing, priorities and limits apply to them too. The single exception is the U3 path: the Approve and Confirm edits of an approval or proposal card keep their reviewed direct `editMessageText`/`editMessageReplyMarkup` calls. P0 alerts and lifecycle summaries always go to 🚦 Control regardless of where the triggering command came from.

| Topic | Contains | Default sound |
|---|---|---|
| 🚦 Control | Pinned dashboard, command replies, P0 alerts, lifecycle summaries (CR-001), "local operator action" FYIs | P0 loud, rest silent |
| ✅ Reviews | Every approval card (proposal, PR, push_update, comment, claim, skill), proposal version diffs, `RESCOPE_REQUIRED` cards, explorer and file messages, draft reviews, Confirm panels, live PR status cards, drafted replies, merged/closed summaries. A candidate's whole lifecycle, from proposal to merge, stays in one topic because replies across topics are not supported (`ReplyParameters`) | P1 loud (rate-limited), updates silent |
| 📈 OSS feed | Discovery, vetting, stage progress, non-critical failures, digests | Silent (P2/P3) |
| 💬 Assistant | Default Hermes conversation (session `tg-<uid>-t<thread_id>`) | Normal (the reply to your own message) |
| 💬 user topics | One Hermes session each; renamed with `/title` | Normal |

Flat mode (topics off, or a topic call fails with "thread not found" after one re-create): every message is sent without a thread, with the section icon as its first character (CR-002 Amendment A, separation). Hermes sessions use `tg-<uid>-<n>` and `/new` increments `n`. The mode is re-checked at each start and on every "thread not found" error.

### 4.2 Menu button and command scopes (D2)
- `setChatMenuButton(owner, MenuButtonCommands)`.
- `setMyCommands(scope=BotCommandScopeChat(owner))` with the registry's commands. Descriptions are ≤ 60 characters, plain, verb first.
- `setMyCommands(scope=Default)` is set to the empty list, so strangers see nothing.
- On a control-state change (running, paused, killed, draining, down (workers)) the owner's command list is re-set:
  - killed: `/status /resume /logs /help /start`. `/settings` is **hidden** while killed, because its sub-pickers include config actions that killed mode refuses anyway; typing it answers with the killed message.
  - down (workers) or draining: adds `/up` or `/undrain`.
- Outside killed mode the global commands `/start /help /settings` are always present (Telegram's recommended global commands). `/start` and `/help` are present in every state.

### 4.3 Pinned live dashboard (D3, D15; mockup `06-pinned-dashboard.svg`)
One message in 🚦 Control, recorded in `tg_views(kind='dashboard')` and pinned silently. Content (classic HTML, ≤ 1,500 characters):
```
🟢 <b>ossbot</b> · UP · 🧪 <b>DRY RUN</b>                  (🟡 DRAINING · 💤 DOWN (workers) · 💤 DOWN (graceful) · ⏸ PAUSED · 🛑 KILLED (soft|full))
Egress: open · Hermes: self_improve · Rich views: on · Auto size ≤ s
Running: c_01JDEF implement 12m · c_01JGHI validate 3m   (≤ 3 lines, then "+N more")
Approvals: 2 pending · 1 proposal · oldest expires <tg-time r>
Queue: 5 vetted · 1 watching (PR #5678)
Telegram: last poll <tg-time r> · 1 reconnect today
Models: chat sonnet-5.5 · coder opus-5 · vision sonnet-5.5
<i>Updated <tg-time unix=… format="r">14:02</tg-time></i>
```
- Keyboard:
  - Row 1: `[🔄 Refresh] [📥 Approvals (2)]`
  - Row 2: `[⏸ Pause]` or `[▶️ Resume…]`, then `[🌙 Quiet 1h]`
  - Row 3: `[⬇️ Down…] [🛑 Kill]` (Kill styled `danger`; two taps, D14)
- Edits happen at most once per 30 s, only when the rendered content's sha256 changes, and are skipped while the outbox has due P0/P1 rows.
- If the message was deleted (edit returns "message to edit not found"), a new dashboard is sent and pinned, and the row is updated.
- `/dashboard` re-sends and re-pins it.
- The two "down" states (CR-001) are named everywhere (dashboard, `/status`, §10, mockups 06 and 09):

| State | Trigger | Gatekeeper | Dashboard header | Short description | Assistant |
|---|---|---|---|---|---|
| `💤 DOWN (workers)` | Telegram `/down` (or the dashboard `⬇️ Down…`) | **Up**, polling and answering | `💤 DOWN (workers) since <tg-time>` + `[⬆️ Up…]` | unchanged: "🟢 Online" | "🤖 Assistant is stopped (workers down) — /up" |
| `💤 DOWN (graceful)` | `down.ps1` / `ossctl down` (gatekeeper stopping) | Stopping, then gone | `💤 DOWN (graceful) since <tg-time>`, edited **before** the outbox is flushed | `setMyShortDescription("💤 Offline since <HH:MM> — wake the laptop to reconnect")` | no answers (offline) |

  On `up` (either path) the dashboard returns to 🟢 and, after a graceful down, the short description returns to "🟢 Online". Because the footer is a relative `<tg-time>`, a stale dashboard visibly ages ("Updated 3 hours ago") even when the bot is unreachable.
- The `Auto size ≤ s` fact is the effective global `size.autonomous_max_class` (CR-004 Amendment B); it changes only through `/size` or `ossctl size set`, and it does not apply to `👤` work.

### 4.4 Update routing table (normative)
`telegram/routing.py` classifies every authorized update in this order. The first match wins.

1. `callback_query`: the token handler (existing).
2. `stopped_message_generation`: the chat stream with that `draft_id`, cancelled (§6.1). Authorized by `authorized_chat_only()` (U10). An unknown `draft_id` is ignored at DEBUG.
3. A reply whose `reply_to_message` is an **open** `pending_input` prompt (ForceReply correlation, 15 min; FEAT-005 table, widened in §14.2): that input kind (`changes`, `edit_title`, `edit_body`, `reject_other`, `edit_comment`, `comment`, `line_pick`, `review_send_text`, `body_file`, `feature_fix`, `proposal_adjust`, `proposal_snooze_days`). This rule runs **before** the command rule, so a comment that starts with `/` ("/api/v1 is wrong") is a comment, not an unknown command.
4. A message whose text starts with `/`: the command registry (existing). This applies in any topic.
5. A reply to a `tg_views` message of kind `file`, `explorer`, `interdiff`, `card`, `body_part` or `commit_msg` whose approval is `pending` or `armed`: a **review comment** (§5.5). Replies to `body_part` and `commit_msg` messages are general comments, with the user's quote kept in `quote`. A reply to a `proposal` view is a proposal adjustment (§5.15). A document reply to a "Body as file" prompt is rule 3.
6. A non-reply message whose text or caption contains `github.com` issue, PR or repo URLs (in any topic, forwards included; at most 3 are carded): a **link card** per URL with `[🛠 Work on this]` (§5.19). When the message is nothing but the URL(s), routing stops here; otherwise it continues with rule 7 or 8.
7. Free text in ✅ Reviews that is not a reply: relayed to Hermes in session `tg-<uid>-reviews`. When exactly one approval is pending, the answer carries one single-button row `[📝 Use as comment]`, and the answer text names the target ("Tap to add your message above to c_01JABC r2's draft review"). The `use_as_comment` token converts **the user's own free-text message** (its stored text, after `undefuse`), never the Hermes answer.
8. Everything else goes to Hermes, in the session of its topic (or the flat session): text, photos, albums, documents, voice, locations, and replies to any other bot message (the quoted context is passed, CR-002).

**Stale messages.** If `message.date` is older than 10 minutes when processed (the laptop was offline):
- read commands answer with "(sent <tg-time r>)";
- go and config commands still only produce their confirm button, and the prompt says "sent 2 h ago while I was offline";
- stop commands run (stopping is always safe), and the reply notes the delay;
- assistant messages are answered with a "↪️ replying to your message from <tg-time r>" header.

Updates older than 24 h are dropped by Telegram itself, which is documented in §10.

## 5. PR review flow (normative; ADR-0019 states S1–S12)
Two state models run side by side. The **approval lifecycle** (DESIGN §12.4: `pending → armed → confirmed → consumed`, or `invalidated|expired|rejected`) is unchanged and authoritative. A per-approval **review UI state** lives in `tg_views.state_json` and is presentation only: `delivering, ready, exploring, commenting, changes_requested, armed, submitting, live, stale`. A UI state never gates an approval transition.

Every view in this section is rendered only from gatekeeper data:
- the contribution row and policy results;
- `derived/<cid>/<rev>/` (patch, numstat, raw, commit message, interdiff);
- validation records;
- model-authored fields (planner rationale, risk, reviewer report, implementer notes), always labelled 🤖 (U6).

### 5.1 S1: Preview card (`01-pr-preview-card.svg`)
This replaces the DESIGN §14.4 example layout; the fields are the same. Classic HTML, ≤ 3,900 escaped characters (cards today are about 1,500), sent to ✅ Reviews.
```
🧩 <b>Ready for review</b> · c_01JABC · r2 · 🧪 <b>DRY RUN</b>          (🔴 <b>LIVE</b>)
👤 <b>requested by you</b> · /work 14 Oct                              (only for user-directed work)
<b>langfuse/langfuse</b> ⭐ 36.0k · 14 ext PRs merged/90d · 1st review ~9h
Issue <a href="https://github.com/langfuse/langfuse/issues/1234">#1234</a> <b>Fix trace export when spans are empty</b>
🤖 <i>Why: export crashes on empty span lists; maintainer confirmed in thread.</i>
✅ CONTRIBUTING · AI policy (disclosure) · DCO signed · unclaimed · no competing PR
Risk 🤖 low (0.82) · Change ✅ 3 files +42 −7 · Tests ✅ lint · unit 212 · smoke
Size ✅ s · est ~40 code → actual +31 / −7 code · +11 test · 3 files
Self-review 🤖 PASS (sonnet-5.5 ≠ coder) · 0 blocking · 2 nits fixed
Commit <code>abc1234</code> on main@def5678 (base as of <tg-time>10:02</tg-time>)
PR title: <code>fix(web): handle empty spans in trace export</code>
Body 2,140 chars (1 part ↓) · commit message ↓ · ⚠️ 0 hidden chars
⏳ Approval expires <tg-time unix=… format="r">in 3 days</tg-time>
```
Notes on the lines:
- **"✅" lines** are gatekeeper facts: policy results (DESIGN §12.6), derived numstat and validation records.
- **"🤖" lines** are model claims.
- **Issue titles** and other untrusted strings are escaped and defused (U8).
- **The verbatim parts** follow as messages 2..K+1: the body as `PR body (i/K)` and then the commit message, unchanged from DESIGN §14.4. Each part's message ID is recorded in `tg_views` (kinds `body_part`, `commit_msg`) by the outbox `mark_sent` transaction (§12.2), which is what lets S12 stub them later.
- **The size line (CR-004 Amendment B)** is a gatekeeper fact computed from the derived numstat with CR-004's counting rules: generated files, lockfiles, snapshots and vendored code (`size.exclude_globs`) are left out, and **test lines are shown separately and never count toward the class**. Format: `Size <badge> <class> · est ~<code> code → actual +<added> / −<deleted> code · +<test> test · <files> files`. `<class>` is the measured class; the estimate is the one bound by the approved proposal and is never re-estimated here. When the item is user-directed, the header line carries `👤 requested by you`.

| Measured vs approved proposal | What the card shows | Effect |
|---|---|---|
| Measured class ≤ estimated class, code lines ≤ estimate × (1 + `size.overrun_tolerance`), all files inside the proposed areas | `Size ✅ s · …` | Normal card |
| Over the tolerance, or a larger class than estimated (`on_overrun: flag`, the default) | `Size ⚠️ m (est s) · est ~40 code → actual +142 / −30 code · +210 test · 6 files` and, on the next line, `⚠️ <b>+330% over estimate</b> · 🤖 coder: "<reason ≤ 120 chars>"` | Normal card. The highlight is a fact plus a labelled claim, never a gate |
| Files outside the proposed areas | `⚠️ 2 files outside the plan: web/src/config.ts, docs/x.md` (≤ 3 paths, then `+N`) | Normal card |
| Tests: none added | `Tests ⚠️ none added · docs-only change` (the self-review's exemption reason: docs-only, config-only, or "repo has no tests") | Normal card. A missing test without an exemption is a self-review rejection, so no card is sent |
| Larger class than approved **and** `on_overrun: reapprove` (opt-in) | no preview card | `RESCOPE_REQUIRED` card instead (§5.17) |
| Above the hard ceiling (`policy.diff`), any mode, autonomous or `👤` | no preview card | Ceiling-hit card (§5.18) |
| No proposal (a `push_update`/`comment` action, or an item created before CR-004) | the line shows only `Size <class> · +a / −d code · +t test · n files` | n/a |

  The ⚠️ line is placed directly under the size line and is never dropped by the kit's budget rule (§12.1): it is a required block, like the header and the expiry.

Keyboard, in two phases. The minting rule is unchanged: Approve is minted only when `body_parts_delivered == body_parts_total`. The set of card purposes changes (§12.4 delta 3): `approve, reject, reject_reason, changes, explore, files, edit_text_pick` (`interdiff` replaces `files` on revised cards). While delivering, the card carries no tokens at all: only one `DisabledButton` `⏳ delivering 1/2…` (10.3; without 10.3 the card simply has no keyboard, as today). Delivery takes seconds, and minting nothing before it keeps `_maybe_arm` the only place card tokens appear.
```
while delivering:  [⏳ delivering 1/2…](disabled)
full:              [✅ Approve & Submit](success) [❌ Reject]
                   [🧭 Explore] [🧾 Files (3)]
                   [✏️ Request changes] [📝 Edit text]
                   [🐙 Review on GitHub ↗](url, only when the mirror is on and pushed)
```
Changes from DESIGN §14.4:
- `View diff` becomes `Files`.
- `Test logs` moves into the explorer (section 7).
- `Edit title`, `Edit body` and `Clear my edits` move behind `Edit text` (S7).
- Labels stay ≤ 20 characters, with ≤ 2 buttons per row and ≤ 4 rows.

Silent or loud follows §5.13. The four action kinds keep their headers and Confirm texts from DESIGN §14.4a:
- `push_update`: 🔁 "Fix ready for PR #N".
- `comment`: 💬 "Comment ready".
- `issue_claim`: 🙋 "Issue claim ready".

### 5.2 S2: Explorer (`02-diff-explorer.svg`)
`🧭 Explore` sends **one explorer message** as a reply to the card (`reply_parameters.message_id = card`), so the quoted header jumps back to the card. If an older explorer exists for this approval, it is edited to the stub "🧭 moved ↓". The explorer is a rich message when rich views are on (§11.3), else classic HTML. Navigation edits it in place.

Breadcrumb line: `🧭 <b>c_01JABC r2</b> · 3/9 Signals`. Sections:

| # | Section | Content (source) |
|---|---|---|
| 1 | Summary | The card facts, plus the mode badge and the approval expiry |
| 2 | Why this issue | Issue title and labels; the first 600 characters of the issue body (escaped, quoted, untrusted); maintainer confirmation facts (vetter); planner rationale 🤖 |
| 3 | Signals | Repo facts (stars, push recency, external PR merge rate, median first review, open PRs by us); policy checks ✅/❌ (CONTRIBUTING, AI policy, CLA, DCO, claim, competing PRs); CODEOWNERS for touched paths |
| 4 | Risk | Deterministic risk tags from derived numstat/raw: CI or workflow paths, dependency manifests or lockfiles, security-sensitive paths (policy list), binary, generated or vendored files, new files, deletions > 50 lines, files > 400 changed lines. Then planner risk and confidence 🤖 |
| 5 | Files | A numstat table (rich `table` or classic aligned `<pre>`): path · +/− · tags; 6 per page with a pager; each row has an `Open` button (button `i`) |
| 6 | Diff | Hunk index per file (`@@` headers and sizes); `Open file` buttons; `📄 .patch` and `🌐 HTML` document buttons |
| 7 | Tests | Each validation command: ✅/❌, duration, counts. Failing commands show the last 40 redacted lines in `details` (rich) or an expandable blockquote (classic). `📄 Full logs` sends the `.txt` (zipped if > 45 MB, existing). Stack validation n/a. "Upstream CI runs after submit" |
| 8 | Self-review | Reviewer model ≠ coder model ✅ (fact); round i/N; verdict, blocking items and nits 🤖 (each ≤ 200 characters, at most 10) |
| 9 | PR text | Title in `<code>`; title/body "edited by you" flags; disclosure block present ✅; hidden-character count; "body parts are the messages under the card"; `📄 pr_body.md`; `📋 Copy title` (`copy_text`, ≤ 256 characters) |

Explorer keyboard (multi-use `nav` tokens, 24 h, bound to the explorer message):
```
[◀] [3/9 ▾] [▶]
[🧾 Files] [🧪 Tests] [🤖 Review]
[💬 Comment] [⬇️ Show card]
```
- `3/9 ▾` swaps the keyboard for a section picker (3 per row, 9 sections).
- `⬇️ Show card` re-sends the card at the bottom with **`/approve` semantics**, which this CR fixes as the code's `ApprovalService.reissue()` (§12.4 delta 5): in one transaction the open approvals of that contribution are invalidated with reason `reissued` (their tokens die), fresh approvals are created for the same `(cid, rev)`, and the card and its body parts are re-sent (N4). The old card becomes "moved to a newer message" (S12). DESIGN §14.5 is amended to match. Because explorer, file and draft-review tokens carry `approval_id = NULL` and `{cid, rev}` (§12.2), they keep working against the new approval.
- **The explorer never carries Approve** (U3).

### 5.3 S3: File view and diff (`02-diff-explorer.svg`)
There is **one file-view message per approval** (per `(cid, rev)`), sent as a reply to the card and recorded in `tg_views(kind='file', cid, rev, path, page)`. It shows one file at a time:
- `Open` (explorer Files) or `Open file` (explorer Diff) **reuses** that message when it exists and its `message_id` is within the last 20 message IDs of the chat (message IDs are sequential in a private chat): it is edited in place to the chosen file. When it is older, or doesn't exist, the file view is sent at the bottom and an older one is edited to the stub "🧾 moved ↓" (the explorer rule, §5.2). Either way there is at most one live file view per `(cid, rev)`.
- `◀ Prev file` / `Next file ▶` **edit the same message** to the previous or next file in numstat order and update `tg_views.path`, `page` and `state_json` (page text and line map, for quote mapping).
- `📂 1/3` swaps the keyboard for a **file picker**: one button per file (`1 trace.ts`, `2 trace.test.ts`, …, labels ≤ 24, 2 per row), ≤ 8 per page with a pager row, plus `[↩️ Back]`. A tap edits the message to that file.

Rich mode (one message, ≤ 30,000 characters, ≤ 450 blocks):
- a heading `🧾 web/src/export/trace.ts` · file 1/3 · +30 −5 · tags;
- per hunk, an `anchor` (`h1`, `h2`, …) and a section heading with the `@@` line;
- a `pre` block with `language: "diff"` holding the hunk's lines exactly as in the derived patch (invisible and bidi characters made visible by `make_visible` first);
- hunks longer than 60 lines inside a closed `details` block whose summary is "Hunk 2 · @@ -88,3 +93,5 @@ · 64 lines";
- a footer "r2 · derived by gatekeeper · base def5678".

A file whose rendering exceeds the limits is paged (part i/K) with the same pager, editing in place.

Classic mode: pages ≤ 3,500 characters inside `<pre><code class="language-diff">`, ◀ ▶ by `editMessageText` (existing DESIGN §14.3), with a header line stating the file, page and hunk range.

File keyboard:
```
[◀ Prev file] [📂 1/3] [Next file ▶]
[💬 Comment] [📄 .patch] [🌐 HTML]
```
A hint line in the message: "Long-press lines → Reply with quote to comment on them."

### 5.4 Documents
- `📄 .patch`: the gatekeeper-derived `FORMAT_PATCH` output, `sendDocument`, filename `<cid>-r<rev>.patch` (existing).
- `🌐 HTML`: `ossbot-review-<cid>-r<rev>.html` from `gatekeeper/review_html.py`:
  - Jinja2 with `autoescape=True` and no `|safe` on any variable that is not a literal of the template. A unit test parses the template AST.
  - Inline CSS, no script. `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">`.
  - Light and dark via `prefers-color-scheme`.
  - Contents: a header with the card facts; a TOC with anchors; per file, a `<details open>` with a unified diff table (old/new line numbers, +/− colouring, `white-space: pre-wrap; overflow-wrap: anywhere`); then the PR title, body and commit message in `<pre>`.
  - Capped at 10 MB, with per-file truncation notes beyond that.
  - Caption (≤ 900 characters): "🌐 Full diff · 3 files +42 −7 · r2 · opens in your browser or Files app".
  - **[VERIFY]** in-app opening on Amsal's devices.

### 5.5 S4: Comments and the draft review
Three ways to comment, all ending in `review_comments`:
1. **Reply to a file message**, optionally with a quote (the user selects lines and taps Reply).
   - Only a **manual** quote anchors lines: `quote.is_manual == true`. A quote the server added automatically (`is_manual` absent) is kept in `quote` but the comment is file-level.
   - The gatekeeper first applies `undefuse` (U8), then searches `quote.text` (≤ 1,024 characters) in the text of the page shown, which is stored in `tg_views.state_json` as page text and line map.
   - One match anchors the comment to `path` and `line_start..line_end`: new side (`side='new'`) for `+` and context lines, old side (`side='old'`) for a span of only `-` lines. A span that mixes `+` and `-` lines anchors to the **new side** using its `+`/context lines, and `comments_json` records both ranges (`new_start..new_end`, `old_start..old_end`).
   - Several matches are resolved by `quote.position` (approximate, UTF-16). If still ambiguous, a line picker offers the candidate line numbers, ≤ 6 buttons (`pending_input(kind='line_pick', line_candidates_json)`).
   - No quote, an automatic quote, or no match anchors the comment to the file (`line_start` NULL).
2. **`💬 Comment`** on a file message opens a hunk picker (`H1 @@ -40,7 +40,12 @@` …, ≤ 8 per page). A hunk tap sends a ForceReply prompt (`pending_input(kind='comment', path, side, hunk)`; placeholder "Comment on hunk 2 (≤ 4000 chars)"). On the explorer the button gives a general comment with no path.
3. **Reply to the card, the explorer, a body part or the commit message**: a general comment (body-part and commit-message replies keep the user's quote).

Draft comments are keyed by **`(cid, rev)`**, not by approval: `review_comments.approval_id` is informational (the approval current when the comment was written). So a `reissue()` (Show card, `/queue → Open`, `/approve`) or a Cancel never orphans a draft review. A new rev starts an empty draft; the old rev's unsent comments are listed once in the S6 card as "2 unsent r2 comments were not sent" and then marked `discarded_at`.

Limits (validated at save time, §14.6):
- ≤ **30 comments** per draft review. The 31st reply gets "Draft review is full (30); send or discard first" and nothing is stored.
- The deterministic `change_requests.text` built at Send is ≤ **60,000 characters**. If it would be longer, Send is refused with the size ("Review is 71,240 characters; the limit is 60,000. Remove or shorten comments first").

Each saved comment is acknowledged in place: `📝 Comment 2 saved · trace.ts L44–46` with `[↩️ Undo]`. The comment text is echoed through `make_visible`.

All pending comments for the approval are summarized in **one draft-review message**, a reply to the card that is edited in place:
```
📝 <b>Draft review</b> · c_01JABC r2 · 3 comments
1. trace.ts L44–46 — log a debug line when spans are empty
2. trace.ts (file) — prefer early return
3. general — add a test for empty spans
[📨 Send 3 comments]
[👁 Show all] [🗑 Discard]
```
`📨 Send` opens a ForceReply: "Anything to add? Reply, or tap Send now". It is a raw `force_reply` inline-keyboard markup with `[📨 Send now]` (10.3), falling back to two messages without 10.3. Sending creates **one** `change_requests` row:
- `text` is deterministic: the optional free text, then numbered comments `path:line — text`, each followed by its quoted lines in a fenced block;
- `comments_json` holds `[{path, side, line_start, line_end, quote, text, source}]`.

Then existing DESIGN §14.4 behaviour applies: the contribution goes to `CHANGES_REQUESTED`, approvals are invalidated, and the card becomes S5.

`✏️ Request changes` on the card is the same path, with the draft review included automatically. While an approval is `armed`, new comments are stored but the draft message says "Confirm pending — comments will apply if you cancel". The Confirm panel warns "⚠️ 3 unsent comments will be discarded". Confirm marks them `discarded_at`. A Cancel, or a confirm expiry (`disarm_expired`, §5.9), keeps them.

### 5.6 S5: Changes in progress (`04-interdiff.svg`, upper part)
The old card is edited:
- header `✏️ <b>Changes requested</b> · c_01JABC · r2 → r3 in progress`;
- the keyboard is replaced by a single disabled `⛔ Not approvable` (10.3; on failure it is simply removed);
- one progress line block, edited at most once per 5 s by the `Progress` component as stages report: `⏳ implement 2m · ⬜ validate · ⬜ self-review · ⬜ package`.

A failure in the round shows `❌ validate failed (attempts exhausted)` and the existing redirect behaviour (DESIGN 9.2).

### 5.7 S6: Revised candidate and interdiff (`04-interdiff.svg`)
The new card (r3) is sent **as a reply to the r2 card**. It is the S1 layout with two blocks added right after the header:
```
🔁 <b>Revised for review</b> · c_01JABC · r3 · 🧪 <b>DRY RUN</b>
<b>You asked</b> (3 comments, <tg-time>14:10</tg-time>):
1. trace.ts L44 — log a debug line · ✅ touched · 🤖 coder: added debug log
2. trace.ts — prefer early return · ✅ touched · 🤖 coder: done
3. add a test for empty spans · ✅ new file trace.test.ts · 🤖 coder: added 2 tests
<b>What changed since r2</b> ✅ (gatekeeper interdiff, base unchanged def5678):
  trace.ts +2 −0 · trace.test.ts +18 (new)
<b>Full change vs base</b>: 4 files +62 −7 — you approve the full patch, not the interdiff
```
- **Bounds of the "You asked" block:** at most **5 items**. Each item's comment text is cut to **80 characters** + "…" (after `make_visible`), its location label to `file Lnn–mm`, and its coder claim to 60 characters. More than 5 comments gives a last line `+N more → 🧭 Explore · 9 PR text`, where the explorer lists every comment with its touched flag. The kit treats the block as **optional** (§12.1 budget rule): when the card would exceed 3,900 characters, the "You asked" block is dropped first (replaced by `You asked: 12 comments → 🧭 Explore`), then the 🤖 lines; the header, the mode line, the PR title line and the expiry line are never cut.
- "✅ touched" is deterministic: in `INTERDIFF` mode, the interdiff changes the commented file inside `line_start-3 .. line_end+3` (new-side lines), or adds the file a general comment named. "⚠️ not touched" otherwise. In `RANGE_DIFF` mode, see below.
- "🤖 coder:" text is the implementer's report, ≤ 120 characters each before the 60-character display cut, untrusted.
- Row 2 of the revised card is `[🧭 Explore] [🔀 Interdiff]`: `Interdiff` replaces `Files`. It opens the interdiff as the file-view message (kind `interdiff`). In `INTERDIFF` mode it has the same comment features as a file view. In `RANGE_DIFF` mode it is a single `pre` view with file-level comments only (below).
- How the interdiff is computed (gatekeeper, at `/package` of rev N; ADR-0019):
  - when `base_sha` is unchanged: `ossbot.common.gitcmd.INTERDIFF = ["-c","diff.algorithm=myers","-c","diff.renames=false","-c","core.quotePath=true","diff","--no-ext-diff","--no-textconv","--no-color","--full-index", old_head, new_head]`;
  - otherwise: `RANGE_DIFF = [<same -c>, "range-diff","--no-color", f"{old_base}..{old_head}", f"{new_base}..{new_head}"]`, and the block says "base moved def5678 → 9a1b2c3".
  - Both run in the rev N mirror with a temporary `objects/info/alternates` line for the rev N−1 mirror, which is removed right after.
  - Output goes to `derived/<cid>/<N>/interdiff.patch` and `interdiff.json` (`mode, base_changed, files[{path, added, deleted}]`).
  - If it fails (previous mirror gone, git error), the block reads "Interdiff unavailable (<reason>) — review the full diff" and the card is still sent (WARNING).
- **Base-moved (`RANGE_DIFF`) mode.** `git range-diff` prints commit-pair headers and a diff of diffs, not a patch, so the file list and touch flags are computed separately and the view is simpler:
  - `files[]` comes from comparing each path's hunk text in the two derived `FORMAT_PATCH` outputs (`old_base..old_head` vs `new_base..new_head`), after removing `index` lines and replacing every `@@ -a,b +c,d @@` header with `@@`. A path is in `files[]` when its normalized text differs or it exists on one side only. `added`/`deleted` count the normalized hunk lines present only in the new or only in the old patch.
  - "touched" means the commented path is in `files[]`. There is no line window, and the block says so: `<b>What changed since r2</b> ✅ (base moved def5678 → 9a1b2c3; line-level touch check unavailable)`.
  - The `🔀 Interdiff` view is the range-diff output in `pre` blocks with **no language** (it is not a valid `diff`), paged like a file view. Replies to it are **file-level only**: a reply with a quote anchors to the path whose range-diff section contains the quote, otherwise it is a general comment. Line comments go through the full-diff file views.
  - `interdiff.json` records `mode: "range_diff"` and `touch_granularity: "file"`.
- **Retention amendment (DESIGN §12.5):** the newest rev whose card was shown keeps its mirror until the next `/package` of the contribution or a terminal state. It is no longer deleted at `IMPLEMENTING`.

### 5.8 S7: Edit title or body
`📝 Edit text` sends a picker (a reply to the card):
```
[✏️ Title] [✏️ Body]
[📄 Body as file] [📋 Copy title]
[↩️ Clear my edits]   (only when an override exists)
```
- **Title and Body:** a ForceReply with the current text quoted, unchanged from DESIGN §14.4 (title 1–256 characters, single line; body ≤ 60,000; normalization at storage; `policy.check_text` before storing, P1-4).
- **Body as file:** the prompt asks for a `.md`/`.txt` document reply.
  - Validation: ≤ 64 KiB, `mime_type` `text/markdown`, `text/plain`, or a filename ending in `.md`/`.txt`; strict UTF-8 decode; then the same normalization and policy check.
  - Downloaded via `getFile` (≤ 20 MB API limit; our cap is far below it).
  - Rejections reply with the exact reason.
- Any accepted edit invalidates the approval and sends a new card ("title/body edited by you"). The old card becomes S12.

### 5.9 S8: Approve and Confirm (`03-approve-confirm.svg`)
`✅ Approve & Submit` edits the card in place to the Confirm panel. The text is the card's first 3 lines, then:
```
🧪 <b>DRY RUN — nothing will be published</b>                     | 🔴 <b>LIVE — this opens a public PR</b>
Confirm: DRY RUN — nothing will be published. Expires in 5 min.   | Confirm: LIVE — open PR on langfuse/langfuse as Amsal1 (author Amsal Khan). Expires in 5 min.
Reviewed: summary ✅ · files 3/3 ✅ · tests ✅                     | Reviewed: summary ✅ · files 2/3 ⚠️ · tests ✅
                                                                  | ⚠️ 1 unsent comment will be discarded
Expires <tg-time format="r">in 5 minutes</tg-time>
[🧪 Confirm dry run](primary) [↩️ Cancel]                          | [🚀 Confirm LIVE](danger) [↩️ Cancel]
```
- The Confirm sentence is exactly the DESIGN §12.4 string, computed at that moment.
- The "Reviewed" coverage line is informational only, from `tg_views` opens; it never gates anything.
- Toasts (`answerCallbackQuery`, ≤ 180 characters) on refusal keep their current wording ("Paused or killed: nothing was approved. /resume first.", the lifecycle text, "Superseded: …").
- **Cancel** returns to S1 with rotated tokens (existing `cancel()` + `rotate_card_tokens`).
- **Confirm expiry** (no tap within `confirm_ttl_seconds`, 5 min) also returns to S1. Today nothing does this, so the housekeeping tick (≤ 10 s) runs the new `ApprovalService.disarm_expired()` (§12.4 delta 2): for every approval with `status='armed' AND armed_at <= now - confirm_ttl_seconds` it sets `status='pending', armed_at=NULL`, marks that approval's unused `confirm` and `cancel` tokens used, runs `rotate_card_tokens`, edits the card back to S1 with the fresh tokens, and audits `approval_disarmed {approval_id, reason:"confirm_expired"}`. The card shows one extra line, `↩️ Confirm timed out — tap Approve again`, until the next edit. A Confirm tap racing the disarm loses cleanly: the token is already used, so it gets the existing "token used" toast.

### 5.10 S9: Submitting, then submitted (`05-submitted-status.svg`)
After Confirm the card shows the existing progress, edited per step: `⏳ Submitting (LIVE)… fork ✅ · push ✅ · PR ⏳`. When submission finishes, the card is finalized:
- LIVE: `🚀 Submitted · PR #5678`, with url button `[🔗 Open PR ↗]` and `copy_text` `[📋 Copy link]`.
- Dry run: `🧪 Dry run done — nothing was published` with `[📄 Would-be payload]` (the recorded JSON as a document).

Then the **live status card** (S10) is sent as a reply.

### 5.11 S10/S11: Live status card, drafted replies, merged or closed
One message per open PR in ✅ Reviews (`tg_views(kind='status')`), edited in place at most once per 60 s, only when its content changes:
```
🚀 <b>PR #5678 open</b> · langfuse/langfuse · 🔴 LIVE
<b>fix(web): handle empty spans in trace export</b>
CI: ✅ 9 · ⏳ 2 · ❌ 0   <i>(details ↓)</i>
Reviews: 👀 @maintainer requested changes · 0 approvals
Comments: 2 from maintainers · last <tg-time r>
Watch: round 1/3 · next poll <tg-time r>
<blockquote expandable>✅ build · ✅ lint · ⏳ e2e (chromium) · …</blockquote>
[🔗 Open PR ↗] [🔄 Refresh]
[📜 Timeline] [🛑 Stop watching]
```
- **P1 events** (a maintainer review, a comment mentioning Amsal1, a ❌ check that blocks merge) are sent as **new messages that reply to the status card**. These are loud, subject to quiet hours.
- **Drafted replies** (`comment` action) and fix rounds (`push_update`) arrive as normal approval cards, replying to the status card, and use the same S1/S8 flow.
- `🛑 Stop watching` is unchanged (M4, confirm tap). `📜 Timeline` edits in place to a list of the PR's events.
- **Merged:**
  - The status card is finalized as `🎉 <b>Merged</b> by @maintainer · <tg-time>` with no buttons except the PR link.
  - A summary message follows: time to merge, review rounds, lines changed, contribution number this month, current streak (§7.4).
  - The summary carries `message_effect_id` when `telegram.effects.merged` is configured (**[VERIFY]** the ID).
- **Closed without merge:** finalized as `🗂 Closed` with the closer and the last maintainer comment (≤ 300 characters, quoted, untrusted), and buttons `[🔁 Re-attempt later] [🗑 Forget]`.
  - `Re-attempt later` is a config action that sets the re-attempt cooldown (DESIGN §5 AC 30 rules still apply).

### 5.12 S12: Stale approvals
On every invalidation, expiry or supersession (DESIGN §12.4 list), the card is edited in the same housekeeping tick:
- header `⚪ <b>Superseded</b> · r3 is newer`, `⌛ <b>Expired</b>`, or `⚪ <b>Invalidated</b> · <label>`, with the label taken from the reason code (table below);
- the original header struck through (`<s>`);
- keyboard `[⛔ Not approvable](disabled)` and `[⬇️ Show current]` (when a current card exists) or `[🔁 Retry]` (when the cause allows a fresh card).

| `approvals.invalidated_reason` (or status) | Label shown | Second button |
|---|---|---|
| status `expired` | `⌛ Expired` · "approval was valid 72 h" | `🔁 Retry` if the action is still `pending_approval` |
| `reissued` | "moved to a newer message" | `⬇️ Show current` |
| `text_edited` | "title/body edited" | `⬇️ Show current` |
| `binding_changed` | "content or mode changed" | `⬇️ Show current` if one exists, else `🔁 Retry` |
| `binding_unavailable:*` | "derived artifacts unavailable" | none |
| `live_check:<code>` | "live check failed: <code ≤ 40 chars>" | `🔁 Retry` |
| `dry_run_toggled` (`control.py`) | "dry-run turned on" or "dry-run turned off", chosen from the current `dry_run` flag | `⬇️ Show current` (the re-issued card) |
| `config_snapshot_changed` (`config_snapshot.py`) | "config reloaded" | `⬇️ Show current` |
| `killed` (`control.py`) | "kill" | none (refused while killed) |
| `new_package_rev` (`packaging.py`) | `⚪ Superseded · r<N> is newer` | `⬇️ Show current` |
| `entered_implementing` (`state_machine.py`) | "changes requested · r<N+1> in progress" (S5 replaces S12 here) | none |
| `fix_round_failed` (`state_machine.py`) | "fix round failed" | none |
| `proposal_*` (CR-004, proposal approvals only) | see §5.15 | — |
| any other code | "invalidated (<code ≤ 40 chars>)" | none |

These are the reason strings the code writes today (`approvals.py`, `control.py`, `config_snapshot.py`, `packaging.py`, `state_machine.py`, `handlers.py`). The build pins them in `telegram/review/stale.py: REASON_LABELS`, with a unit test that greps every `invalidate_*(…, "<literal>")` call site and fails when a literal has no row.

`[🔁 Retry]` on S12 means **a fresh card via `/approve` semantics** (`reissue()`, §5.2): a new approval for the same action, if the action is still `pending_approval` and the system is running. It is a kit token `stale_retry` (approval_id NULL, arg `{cid, rev}`), unrelated to the existing `retry` purpose, which re-runs a `failed` submission and is not used here.

Body-part and commit-message messages are edited to a one-line stub `⚪ part of superseded c_01JABC r2`, so stale verbatim bytes can't be mistaken for current ones; their message IDs come from `tg_views(kind IN ('body_part','commit_msg'))`, written at send time (§12.2). Stale explorer and file messages get the same stub and `tg_views.stale = 1`. A tap on any stale token answers "Superseded: content or mode changed since this card; review the new card." (existing).

### 5.13 Queue and priority
- **Ordering** (`telegram/review/queue.py`, deterministic, unit-tested): `(class_rank, expires_at, -target_priority, created_at)`.
  - Class 0: someone is waiting (a `comment` replying to a maintainer, a `push_update` triggered by a maintainer review).
  - Class 1: `issue_claim`, and every `👤` user-directed proposal or preview (you are the one waiting).
  - Class 2: other `push_update`, and ceiling-hit cards (§5.18), which hold a paused item.
  - Class 3: `open_pr` (gate 2 previews), `RESCOPE_REQUIRED` cards (§5.17) and skill cards (§9).
  - Class 4: autonomous proposals (gate 1, §5.15), ordered inside the class by CR-004's rank score (descending) instead of `-target_priority`.
  - Class 5: other `comment`.

  **Why previews outrank autonomous proposals:** a PR preview represents finished compute and an approval that expires in 72 h; a proposal costs nothing to leave waiting and has its own TTL (`proposal.ttl_hours`). Finishing work in flight before starting new work also matches CR-004's pacing goal. An autonomous proposal within 6 h of expiry is promoted to class 2 so it is not lost silently.
- **Loudness:** at most `telegram.loud_cards_per_hour` (default 2) new cards per hour notify with sound. Classes 0–1 always notify unless quiet hours apply, so a `👤` proposal you asked for arrives loud. Autonomous proposals (class 4) are **never loud**: each discovery cycle's ≤ `discovery.proposals_per_cycle` cards are sent silently and counted on the dashboard. The rest go out with `disable_notification: true` and are counted on the dashboard ("📥 4 pending, 2 new silently").
- **`/queue`** shows the ranked list, 5 per page, with a section header per gate: `<b>Gate 2 · PR previews (2)</b>` then `<b>Gate 1 · proposals (3)</b>`. Lines look like `1. 🔁 PR #5678 fix · langfuse · expires <tg-time r>` and `4. 💡 Proposal v2 · langfuse #1234 · s · expires <tg-time r>`. Each line has an `[Open]` button that re-sends the card with `/approve` semantics (`reissue()`, §5.2; proposals: `ProposalApprovals.reissue()`, §5.15). Sort toggles: `[⬆️ Oldest first] [🎯 By priority]`.
- **Expiry warning:** a pending approval with < 12 h left gets one silent P2 reminder in the digest batch. One with < 1 h left gets a P1 reminder: a reply to the card, `[⬇️ Show card]`.

### 5.14 Review mirror integration (only when enabled; ADR-0019 option C)
When `review_mirror.enabled` is on and the push succeeded:
- the card's row 4 is `[🐙 Review on GitHub ↗]`;
- explorer section 1 shows `🐙 Mirror PR #12 · tree-equal ✅`.

**Late mirror push.** The mirror push is asynchronous and may land after the card was sent, or even while it is armed. When it lands, the gatekeeper adds row 4 with **`editMessageReplyMarkup`, re-using the same callback tokens** already on the card (read from `ui_tokens` for that `tg_message_id`); nothing is minted or rotated, and the URL button needs no token. If the card is armed, the Confirm panel is left alone and row 4 appears when the card next renders S1 (Cancel or confirm expiry). If the card is stale or consumed, nothing is edited.

Mirror review comments arrive as described in ADR-0019:
- A submitted review becomes a change request automatically, with one Telegram note replying to the card: `🐙 GitHub review · 3 comments → change request (r3 starting)`.
- Single comments join the draft review, with source `mirror`.
- A GitHub "Approve" produces `🐙 You approved on GitHub — approvals only count here` and `[⬇️ Show card]`.

When a mirror push fails (push protection, network, token), the card shows `🐙 Mirror unavailable: <reason>` and the native flow continues.

### 5.15 Gate 1: the proposal card (CR-004; `10-proposal-card.svg`)
Scout's proposal (CR-004 §1) is rendered by `telegram/proposal_cards.py` as a classic HTML message in ✅ Reviews, ≤ 3,900 escaped characters. It is a bound decision surface, so U3 applies: never rich, never a draft, Start work only on the card. Every model-authored field is labelled 🤖 and quoted (U6); every gatekeeper fact carries ✅.
```
💡 <b>Proposal</b> · c_01JABC · v2 · 🧪 DRY RUN (n/a: local change)
👤 <b>requested by you</b> · "keep it minimal"                 (user-directed only; your /work notes ≤ 80 chars)
<b>langfuse/langfuse</b> ⭐ 36.0k · 14 ext PRs merged/90d · merge ~2 d
Issue <a href="…">#1234</a> <b>Fix trace export when spans are empty</b> · bug · good first issue
🤖 <b>Problem</b> <i>Export throws when a trace has no spans; the OTel converter maps an undefined list.</i>
🤖 <b>Approach</b>
  1. Guard empty span lists in toOtel()
  2. Return an empty export payload with the trace id
  3. Add a regression test next to trace.test.ts
🤖 <b>Likely files</b> web/src/export/trace.ts · web/src/export/trace.test.ts
Size 🤖 est <b>s</b> · ~35 code · ~40 test · 2 files · ✅ fits your auto size ≤ s
Validate ✅ repo has tests (vitest) · T0 unit · T1 n/a · 🤖 easy
✅ Signals: maintainer confirmed in thread · no competing PR · assignment not required
🤖 <b>Why it fits</b> <i>maintainer asked for a PR; 9 of 14 recent outside PRs were this size</i>
Confidence 🤖 0.78 · Risks 🤖 2 · Alternatives 🤖 1 → 🧭 Details
⏳ Proposal expires <tg-time unix=… format="r">in 3 days</tg-time>
```
Field rules (all after `make_visible` and `defuse_commands`; untrusted):

| Field (CR-004) | Source | Display bound | Overflow |
|---|---|---|---|
| Problem | scout 🤖 | ≤ 300 characters | cut + "…", full text in Details |
| Approach | scout 🤖 | 3–6 numbered bullets, each ≤ 100 characters | > 6: first 6 + "+N more" |
| Likely files | scout 🤖 | ≤ 4 paths (`code` style), each ≤ 60 characters (middle-ellipsis) | "+N more" |
| Size estimate | scout 🤖 + ✅ class check | `est <class> · ~<code> code · ~<test> test · <n> files` | n/a |
| Validation plan | ✅ repo facts (vet/scout tool output: test runner found) + 🤖 difficulty | 1 line | Details |
| Signals | ✅ vet facts | ≤ 3 facts | Details |
| Acceptance rationale | scout 🤖 | ≤ 160 characters | Details |
| Confidence | scout 🤖 | `0.00–1.00`, 2 decimals | n/a |
| Risks, alternatives | scout 🤖 | counts only | Details lists them (≤ 10 each, ≤ 200 characters each) |
| Mode badge | ✅ control state | always shown, "n/a: local change" (S3) | never dropped |

The **size check** line compares the estimate with the effective autonomous preference: `✅ fits your auto size ≤ s`, or, for a `👤` item, `👤 size preference not applied` (Amendment B). Autonomous proposals above the preference are never sent (CR-004 ranking), so there is no "too big" variant for them. The kit's budget rule drops Signals, then the rationale, then the Approach bullets after 3, before it would cut the header, the mode line, the issue line, the size line or the expiry.

**Keyboard** (4 rows × ≤ 2, labels ≤ 24):
```
[✅ Start work](success)  [⏭ Skip]
[✏️ Adjust plan]          [📏 Make it smaller]
[🚫 Never…]               [⏰ Later…]
[🧭 Details]              [🙋 Ask maintainers first]   ← only when vet found "assignment required"
```
- `✅ Start work` → the card is edited in place to the proposal Confirm panel (two taps, S1):
  ```
  Confirm: start work on langfuse/langfuse#1234 (proposal v2 · est s · sha256 1a2b…9f0c).
  Local work only — dry-run does not apply; nothing is public before you approve the PR.
  Expires in 5 min.
  [🛠 Confirm start](primary) [↩️ Cancel]
  ```
  When the repo needs a CLA, the panel adds `⚠️ CLA required by this repo — confirming means you have signed it` and the button reads `🛠 Confirm (CLA signed)`. When the one-open-PR-per-repo rule would be broken (only possible for `👤` work), it adds `⚠️ You already have PR #5678 open here — confirming overrides the limit for this item`. Confirm style is `primary`: starting work spends compute but widens nothing; the public writes still need gate 2. Cancel or a 5-minute expiry returns to the card (same `disarm_expired` housekeeping, §5.9).
- `✏️ Adjust plan` → ForceReply prompt `pending_input(kind='proposal_adjust')`, placeholder "What should change in the plan? (≤ 2000)". A reply directly to the card (routing rule 5) is the same input. The feedback goes to scout, the card's keyboard becomes `[⛔ Revising v2 → v3…](disabled)`, and the new version follows (§5.16).
- `📏 Make it smaller` → the same path with fixed feedback "Reduce the scope to fit class <next smaller class>; keep the fix minimal". It is shown only when the estimate is above `xs`.
- `⏭ Skip` → immediate. The card is finalized `⏭ Skipped` with `[↩️ Undo]` (kit token, 10 min). Re-proposed only on a material change to the issue (CR-004 M6).
- `🚫 Never…` → the keyboard swaps to `[🚫 This repo] [🚫 This kind: bug]` / `[↩️ Back]`. A choice edits the card into a confirm panel (`Never propose work from langfuse/langfuse again? Undo in /repos.` `[🚫 Never this repo](danger) [↩️ Cancel]`), then finalizes it `🚫 Never: this repo`.
- `⏰ Later…` → `[1 d] [3 d] [7 d]` / `[14 d] [Custom…] [↩️ Back]`; `Custom…` is a ForceReply (`pending_input(kind='proposal_snooze_days')`, integer 1–90). The card is finalized `⏰ Snoozed until <tg-time>` with `[↩️ Undo]`.
- `🧭 Details` → one reply message (kind `proposal_details`, rich when available, else classic) with: the full problem, approach and file list; tests to add or update; the validation plan; risks; alternatives; scout's codebase overview (layout, tooling, conventions, recent merged PR sizes, open PRs in the area) and the full version diff (§5.16). It carries `[◀] [i/N ▾] [▶]` section navigation and **no decision buttons**.
- `🙋 Ask maintainers first` → creates an `issue_claim` action, whose own approval card (S1/S8, mode-labelled, DRY RUN by default) follows as a reply. The proposal stays open.

**Delivery.** Autonomous proposals arrive silently (class 4, §5.13), at most `discovery.proposals_per_cycle` (3) per cycle and `proposal.max_per_day` (5) per day (CR-004). A `👤` proposal is P1 and loud (class 1). The v1 card of an autonomous proposal starts a new reply chain; a `👤` v1 card replies to the `Working on it` message when that is in ✅ Reviews (replies can't cross topics, so otherwise it starts a chain too). Later versions, Details, rescope, ceiling-hit and PR preview cards reply to the latest proposal card, so one candidate's history is one reply chain.

**Expiry** (`proposal.ttl_hours`, 72 h): the card is edited to `⌛ Expired · re-scouting if still relevant` with `[⛔ Not decidable](disabled)`. A tap on any of its tokens answers "This proposal expired; a fresh one will arrive if the issue is still open."

**Tokens.** Proposal decisions run through a separate `ProposalApprovals` service (§12.4 delta 4), which mirrors `ApprovalService` on CR-004's proposal-approval rows (`approval_id = 'pa_…'` in `ui_tokens`, so `_kill_tokens` on a PR approval never touches them). Purposes: `proposal_start`, `proposal_confirm`, `proposal_cancel` (single-use; start/confirm bound to the card's message ID and the proposal binding `{v:1, kind:"proposal_start", cid, version, proposal_sha256, est_class, user_directed, policy_hash}`), and kit purposes `proposal_skip`, `proposal_adjust`, `proposal_smaller`, `proposal_never`, `proposal_later`, `proposal_details`, `proposal_ask_first`, `proposal_undo`.

### 5.16 Proposal versions and the version diff
Each adjustment creates version `v<N+1>`. The new card is sent **as a reply to the previous card**, which is edited to `⚪ Superseded · v3 is newer` with `[⛔ Not decidable](disabled)` and `[⬇️ Show current]`, so the old Start work can't be used (the binding includes the version and the proposal hash). The new card has one block right after the header:
```
<b>Changed since v2</b> (diff by gatekeeper; content 🤖):
You asked: "keep the debug log, behind a flag" · 14:10
+ Approach 3: Gate the debug log behind EXPORT_DEBUG
− Approach 3: Remove the debug log
+ File: web/src/config.ts
Size: s → s · ~35 → ~48 code · ~40 → ~40 test
```
- The diff is deterministic: the gatekeeper compares the two stored structured proposals field by field (problem, each approach bullet by position, file list as a set, estimate numbers, validation plan, risks). It is computed by code, but every line quotes model-authored content, hence the label.
- Bounds: "You asked" ≤ 80 characters; ≤ 6 diff lines, each ≤ 100 characters; then `+N more → 🧭 Details`. Details carries the complete diff as a `pre` with language `diff` of the two canonical renderings.
- Unchanged fields are not listed. If nothing but the wording changed, the block says `No structural change (wording only)`.

### 5.17 RESCOPE_REQUIRED (opt-in `size.on_overrun: reapprove` only)
In the default `flag` mode an overrun is only highlighted on the preview card (§5.1). With `reapprove`, a measured class above the approved one sends the item back to gate 1: instead of a preview card, the gatekeeper sends a **proposal card variant** as a reply to the approved proposal:
```
📏 <b>Re-approval needed</b> · c_01JABC · v2 → v3 · 🧪 DRY RUN (n/a: local change)
Approved <b>s</b> (~35 code) · measured ✅ <b>m</b>: +142 / −30 code · +210 test · 6 files
⚠️ 2 files outside the plan: web/src/config.ts, docs/export.md
🤖 coder: "the empty payload needs a config migration" (≤ 120 chars)
⏳ Expires <tg-time r>
[✅ Continue at m](success) [📏 Make it smaller]
[🧭 Explore] [🗑 Drop]
```
- `✅ Continue at m` is a proposal Start work at the measured class: two taps, the Confirm sentence `Confirm: continue c_01JABC at size m (was s). Local work only — dry-run does not apply. Expires in 5 min.` After Confirm the already-built candidate goes straight to the preview card; nothing is rebuilt.
- `📏 Make it smaller` sends the "fit class s" feedback to scout and the plan → implement stages run again.
- `🧭 Explore` opens the normal explorer on the measured candidate (read-only; there is no Approve).
- `🗑 Drop` finalizes the item (confirm tap).

### 5.18 Ceiling-hit card (any mode; autonomous and `👤` alike)
When the measured diff exceeds the hard ceiling (`policy.diff`: 80 files, 3,000 code lines, 3,000 test lines), the item pauses (CR-004) and this card is sent in ✅ Reviews as a reply to the proposal:
```
🧱 <b>Size ceiling reached</b> · c_01JABC · paused
Measured ✅ 91 files · +3,180 / −240 code · +1,020 test
Ceiling (config only): 80 files · 3,000 code · 3,000 test
This guard stops runaway changes. Split the work, or raise policy.diff in config/policy.yaml and /reload.
[✂️ Split (adjust plan)] [🗑 Drop]
[🔁 Re-check] [🧭 Explore]
```
- `✂️ Split (adjust plan)` opens the `proposal_adjust` prompt pre-filled with "Split this into parts that each stay under the ceiling; propose part 1 first".
- `🔁 Re-check` re-runs only the deterministic size check against the current ceiling (after a config change and `/reload`). If it now passes, the preview card is sent; otherwise the toast says "Still over the ceiling (91 > 80 files)".
- There is no button that raises the ceiling (U13).

### 5.19 User-directed work: `/work` and forwarded links (CR-004 Amendment B)
**`/work <url> [notes]`** (class `request`, §8.1). Accepted URLs (validation in §14.6): `https://github.com/<owner>/<repo>`, `…/issues/<n>`, `…/pull/<n>`; the `https://` may be omitted when typing. The reply, in the originating thread:
```
🛠 <b>Working on it</b> · langfuse/langfuse#1234 · 👤 requested by you
"Fix trace export when spans are empty" · issue · open
Scouting now (slot 1 of 2) · the proposal card will arrive in ✅ Reviews
Size preference and filters don't apply; safety checks do.
[✖️ Cancel request]
```
The title and state come from the gatekeeper's read-only GitHub client; on a read failure the line says `(details unavailable)` and the request is still queued. `✖️ Cancel request` (kit token, valid until the proposal card is sent) cancels the scout.

**Forwarded or pasted links.** A non-command, non-reply message (text, caption or a forward) that contains one to three `github.com` issue, PR or repo URLs gets one **link card** per URL, as a reply to the message:
```
🔗 <b>langfuse/langfuse #1234</b> · issue · open · "Fix trace export when spans are empty"
[🛠 Work on this] [✖️ Dismiss]
```
- `🛠 Work on this` is exactly `/work <url> <rest of the message text ≤ 1000 chars>`: the card is edited to the `Working on it` text above.
- More than 3 URLs in one message is rule 6 too: link cards for the first 3, then one line "+N more links: use /work for each".
- If the message contains nothing but the URL(s) (and whitespace), it is **not** relayed to Hermes. Otherwise it continues to routing rule 7 or 8 as well, so "what's this issue about? <url>" still gets an assistant answer.
- The link card is a gatekeeper message built only from the parsed URL and GitHub API facts; the user's surrounding text is never echoed into it.

**Pre-checks shown to the user** (CR-004 Amendment B "still applies"), each at the point CR-004 evaluates it:

| Check | Where it shows | Effect |
|---|---|---|
| AI-contribution ban in the repo's policy | A refusal card instead of a proposal: `🚫 Can't work on this: langfuse/langfuse bans AI-assisted contributions` + the quoted policy line (≤ 300 chars, untrusted) + `[📄 Open policy ↗]` | Refused, nothing else runs |
| Forbidden paths | Proposal card line `⛔ Touches forbidden paths: .github/workflows/*` and no `Start work` button (`[⛔ Not startable](disabled)`) | Refused at gate 1; at implement time a hit stops the item with an FYI |
| CLA required | Proposal card line `⚠️ CLA required` + the Start work confirm sentence (§5.15) | Confirm means "signed" |
| One open PR per repo | Proposal card line `⚠️ You already have PR #5678 open here` + the confirm override sentence (§5.15) | Override per item, audited |
| Hard ceiling | Ceiling-hit card (§5.18) | Paused |

`ossctl work <url>` creates the same item; its proposal card shows `👤 requested by you · via ossctl`.

## 6. Assistant chat (`07-assistant-chat.svg`)

### 6.1 Streaming
1. Routing (§4.4) relays the message to `ChatBackend`. The Hermes call is `POST /v1/responses` with `stream: true` and `X-Hermes-Session-Key` (topic or flat session). The stream is SSE: `response.output_text.delta`, `function_call` and `function_call_output` items, `reasoning` summaries and `response.completed|cancelled|failed` (Hermes API-server docs). The live contract comes from `GET /v1/capabilities` (existing check).
2. Immediately: `sendRichMessageDraft` with a `thinking` block ("Thinking…"), or an empty-text `sendMessageDraft` when rich is unavailable. Both use `draft_id = turn_id` (a positive int63) and `can_stop: true`.
3. **Draft lane** (`telegram/drafts.py`, in-memory, never persisted in `tg_outbox`):
   - Drafts are coalesced (the latest state wins) and sent at most once per 1.0 s per chat.
   - They yield whenever the outbox has a due row (cards and alerts first).
   - They are refreshed at least every 20 s even without new tokens, because a draft lives for 30 s: the heartbeat text is "⏳ 45 s · reading 2 pages".
   - Content is the partial answer converted by `md2blocks` (tolerant of unclosed fences), plus one status line from tool items (§6.3).
4. **Completion:** the final answer is sent with `sendRichMessage` (`blocks`, `skip_entity_detection: true`), or with `sendMessage` chunks (classic, escaped, §11.4).
   - Answers over 30,000 characters (rich) or 3 classic parts are split at block boundaries.
   - Answers over 3 parts also get an `answer.md` document.
5. **Stop:** `stopped_message_generation` (`draft_id` must match an active turn) does three things:
   - It cancels Hermes. When `/v1/capabilities` advertises runs, the relay uses `POST /v1/runs` + `GET /v1/runs/{id}/events` and stops with `POST /v1/runs/{id}/stop`. Otherwise it closes the Responses SSE stream (**[VERIFY]** that closing cancels the turn at v0.22.2).
   - It sends the partial text as a final message with the footer `⏹ Stopped by you`.
   - `keep_on_stop` is false; we persist the partial ourselves.
6. **`allowed_updates`** gains `"stopped_message_generation"` (ADR-0004 amendment). PTB 22.8 does not model the update. Verified in the v22.8 source (`TelegramObject._de_json`): keys that are not constructor parameters are moved into `api_kwargs` instead of raising, and `Update.de_json` passes unknown top-level keys through that path. So the update reaches a `TypeHandler(Update)` with `update.api_kwargs["stopped_message_generation"] = {chat, message_thread_id?, draft_id}`, and `update.effective_user`/`effective_chat` are `None`. The handler validates the dict with a pydantic model (`MessageGenerationStopped`, `extra="ignore"`, `draft_id` int > 0, `chat.id` int, `chat.type` str). Authorization is `authorized_chat_only()` (U10): `chat.type == "private" and chat.id == owner`. A live check (§17 item 3) still confirms the end-to-end path with the real API.
7. `sendChatAction("typing")` is used only when drafts are unavailable (every 4.5 s while waiting). `upload_photo` and `upload_document` are used before sending generated media or documents.

### 6.2 Rendering Hermes output (U1)
`gatekeeper/chat/md2blocks.py` is a strict, minimal Markdown parser that runs on Hermes text and produces `InputRichBlock` JSON. Every text leaf is a plain string.

| Markdown | Rendered as |
|---|---|
| Headings | `section_heading` (levels collapse to 2) |
| Paragraphs | `paragraph` |
| `-`/`1.` lists, nested ≤ 3 levels | `list` |
| Task lists | `list` with checkbox items, display only |
| Code fences | `pre` with `language` (≤ 20 characters, `[a-z0-9+#-]`, else none) |
| GFM tables | `table` (≤ 20 columns, cells inline only, ≤ 200 rows, then "…") |
| `>` quotes | `blockquote` (or `expandable_blockquote` when > 12 lines) |
| `**b**`, `*i*`, `` `code` ``, `~~s~~` | `RichTextBold`, `RichTextItalic`, `RichTextCode`, `RichTextStrikethrough` |
| Links | `RichTextUrl` only for `http(s)://` URLs ≤ 2,048 characters. The text shows the link text followed by the domain in parentheses when it differs. Anything else stays literal text |

Never emitted: images and media from Hermes URLs (media come only from §6.5 features), raw HTML of any kind, `tg://` links, and buttons.

Classic fallback: the existing minimal converter (DESIGN §14.3) on escaped text, plus `defuse_commands` (U8).

### 6.3 Tool activity, sources and footer
- **Status line (draft only, never persisted):** from `function_call` items.
  - web search: `🔎 Searching the web (2)`
  - web fetch: `📄 Reading developer.hashicorp.com` (the domain only, from the URL argument parsed with `urllib.parse`, http(s) only)
  - memory: `🧠 Checking memory`
  - an ossbot tool: `🛰 ossbot: status`
  - anything else: `🛠 <tool name ≤ 32 chars>`

  Arguments and results are never shown. Reasoning summaries are not shown (a deliberate choice: they are noisy on a phone).
- **Sources:** URLs from `web_fetch` arguments and from links in the final answer, deduplicated and capped at 8.
  - Shown as a closed `details` block "Sources (3)" listing `domain — title-or-path` links, or as an expandable blockquote in classic mode.
  - **Classic answers only:** link previews are off (`link_preview_options.is_disabled`), except when the answer cites exactly one source: then `url` = that source, `prefer_small_media`, `show_above_text: false`. Rich answers have no link preview at all (`sendRichMessage` has no `link_preview_options`); their sources live in the `details` block.
- **Footer** (rich `footer` block or classic `<i>`): `🧠 <model actually used, from runtime.model> · 🔎 2 · 📄 3 · 14 s`, with `⚠️ fallback model` when it differs from `models.yaml roles.chat`.

### 6.4 Follow-up suggestions (D9, U2)
- The Hermes prompt (the `ossbot` plugin instructions) asks it to end with an optional fenced block:
  ````
  ```followups
  - Compare with OpenTofu
  ```
  ````
- `chat/followups.py` strips the block from the text and keeps ≤ 3 items. For each item:
  - strip controls, invisible and bidi characters (`make_visible`, then reject when any were found);
  - collapse whitespace;
  - drop items starting with `/` or containing `approve`, `confirm`, `submit`, `resume` or `dry` (case-insensitive);
  - **drop** (never cut) items longer than 64 characters. The label **is** the text: there is no separate hidden text, so what the user taps is exactly what is sent.
- Each item becomes a `chat_followup` token (24 h, single-use, bound to the answer message, `arg = {"text": <label>, "turn_id": …}`). On a tap the gatekeeper:
  1. checks `arg.text` equals the label it rendered (both come from the same stored row; a mismatch is a bug and is refused at ERROR);
  2. **first** sends the echo `↪️ <text>` as a bot message in the same topic (so the user sees what is being sent before Hermes gets it);
  3. then relays the text to Hermes as the user's next turn, recording `chat_turns.origin = 'followup_tap'` (§14.2) so audits and the Telegraph eligibility check can tell it apart from typed input.

  It never reaches the command registry.
- One button per row (single-button rows allow ≤ 64 characters, §11.2), ≤ 3 rows.

### 6.5 Photos, albums, documents, voice and locations (CR-002 Amendment A)
- **Acknowledge:** `setMessageReaction(👀)` on the user's message as soon as it is accepted. It becomes `👌` on success (or `🤷` on unsupported input) when the answer is sent.
- **Albums:** messages with the same `media_group_id` arriving within 1.5 s of each other are one Hermes turn, capped at 10 images.
- **Images:** sent as `input_image` data URLs. The vision model comes from `models.yaml roles.vision`. The footer shows `👁 vision: <model>`.
  - **Photos** (`message.photo`): the gatekeeper downloads the largest `PhotoSize` whose longer side is ≤ 1,600 px. Telegram already re-encodes photos as JPEG and supplies the smaller sizes, so no resizing is needed in ossbot.
  - **Image documents** (`message.document`): accepted only as JPEG, PNG or WebP, identified by **magic bytes** (`FF D8 FF`; `89 50 4E 47 0D 0A 1A 0A`; `RIFF….WEBP`), not by MIME type or filename, and ≤ 5 MB (the vision input cap). HEIC/HEIF, GIF, TIFF, SVG and anything else get "Send it as a photo instead (tap 🖼, not 📎)". Larger files get "Image is 12 MB; as a file the limit is 5 MB — send it as a photo".
  - **Metadata stripping** (CR-002 toggle, on by default) runs in the gatekeeper in `chat/imgmeta.py`, a pure-Python container walker that **never decodes pixels** (no image library in the TCB, D20):
    - JPEG: walk the marker segments up to SOS; drop `APP1`–`APP15` (EXIF, XMP, ICC is `APP2` and is dropped too) and `COM`; keep `APP0` (JFIF), `DQT`, `SOF*`, `DHT`, `DRI`, `SOS` and the entropy-coded data unchanged.
    - PNG: walk the chunks; drop `eXIf`, `tEXt`, `zTXt`, `iTXt` and `tIME`; keep every other chunk; verify each kept chunk's CRC.
    - WebP: walk the RIFF chunks; drop `EXIF` and `XMP ` and clear the matching flags in `VP8X`; fix the RIFF size.
    - Any structural error (bad length, truncated segment, CRC mismatch, more than 4,096 segments or chunks) rejects the image with "That image file looks damaged; send it as a photo". Never a partial strip.
    - Fuzzed with Hypothesis (random truncation, length fields and chunk orders, plus a corpus of real files with GPS tags) in `tests/unit/chat/test_imgmeta.py`: the walker either returns bytes with no metadata chunk/segment left or raises `ImageRejected`; it never raises anything else and never takes more than 50 ms per MB.
  - Pillow is not used (decision and alternatives in ADR-0019 Addendum A §A.5). Image *tools* that do need decoding (phase 2, §13 item 12) run in a `--network none` sandbox job, where Pillow may live in the sandbox image.
- **Text documents** (`text/*`, ≤ 256 KiB, strict UTF-8): sent as `input_text` with the filename.
- **PDF:** text extraction runs in a sandbox job, never in the gatekeeper. The job is `--network none`, `entrypoint=extract_text`, using `pdftotext` from `sandbox-full`, capped at 2 MiB of text. Until that entrypoint exists the reply is "PDF reading isn't available yet; send the text or a screenshot" (v1.x, §13 item 9).
- **Voice:** when transcription is available (Kiro audio input **[VERIFY]**, else the §13 transcription hook), the transcript is shown first as an expandable blockquote `🎙 You said: …` and then answered. Otherwise: "Voice isn't supported yet — send text."
- **Location:** passed as `lat, lon` text with a "📍 location shared" marker.
- **Oversize or unsupported input** gets an immediate reply with the limit ("File is 31 MB; the limit is 20 MB").

### 6.6 Sessions, /new, memory and model
- **`/new`:** in topic mode, creates a topic `💬 Chat <date HH:MM>` and replies there ("New chat started ↓"); in flat mode, increments `n`.
- **`/title <text>`**, or the `[✏️ Rename]` button on the first answer: `editForumTopic`. The name is ≤ 48 characters, sanitized, and controls are rejected.
- **`/memory`:** a card explaining that memory persists across chats in every tier. Buttons:
  - `[🧠 What do you remember?]` sends a canned prompt.
  - `[🗑 Forget this chat]` deletes the Hermes session (`DELETE /api/sessions/{id}`, **[VERIFY]** at the pinned version) after a confirm tap.
  - `[💬 New chat]`.
- **`/model`:** picker, §8.1.

### 6.7 Feature cards (phase-2 implementation; v1 ships the pattern and registry)
Features are registered in the gatekeeper (§12.3). Hermes calls the plugin tool `ossbot_feature_card(feature, kind, data)`, which makes `POST /v1/features/{feature}/cards` (role `hermes`, ≤ 30/h). The gatekeeper validates `data` with the feature's pydantic model (`extra="forbid"`, bounded fields), stores it in the feature store, renders the card with the kit, and sends it into the conversation's topic.

**Where Hermes-originated messages go (U12).** This applies to feature cards, `POST /v1/notify` from Hermes and reminders created by `ossbot_reminder_create`:
- The plugin sends the `X-Hermes-Session-Key` of the turn it is serving. The gatekeeper accepts it only if its **own** `chat_turns` table has a turn for that `session_key` with `status='streaming'`, or one that finished ≤ 10 minutes ago. The target thread is that row's stored `thread_id`. An unknown or stale key gets 409 `no_active_turn` and nothing is sent (INFO log).
- The resolved thread must be 💬 Assistant or a user chat topic (in flat mode: the assistant section with its 🤖 header). A session whose thread is ✅ Reviews (`tg-<uid>-reviews`) gets its feature cards and notifications in 💬 Assistant instead, with a first line `🤖 from your Reviews chat`. Nothing Hermes-originated is ever sent to ✅ Reviews, 🚦 Control or 📈 OSS feed.
- Every Hermes-originated message carries 🤖 in its header line: notifications and reminders start with `🤖`, and feature cards end their header with `· 🤖 assistant` (e.g. `🍽 Meal logged · lunch · 12:40 · 🤖 assistant`, keeping the one-leading-icon rule of §11.1). So none can look like a gatekeeper fact or an approval card.
- Priority: Hermes may ask for P1 or P2 (P0 is never accepted from Hermes, existing). At most 5 Hermes P1 messages per local day; after that, P1 requests are sent as P2 and counted in the digest ("Hermes asked for 3 more loud notices today").
- Reminders store the creating turn's `thread_id` at creation and fire there as P1, subject to quiet hours (they are held until quiet hours end, then sent loud).

- **Nutrition log** (`07-assistant-chat.svg`, right phone):
  ```
  🍽 Meal logged · lunch · <tg-time> · 🤖 assistant
  table: item · g · protein · kcal   (≤ 12 rows)
  Today: protein ▰▰▰▰▰▰▱▱▱▱ 82 / 140 g · kcal 1,640 / 2,300
  🤖 estimate from photo · confidence medium
  [✏️ Fix] [↩️ Undo] [📊 Today]
  ```
  `Fix` uses ForceReply with "item grams" lines, parsed deterministically. `Undo` deletes the entry. `Today` edits the card to the day totals. The day summary is included in the evening digest when enabled.
- **Reverse image search** (needs a provider; CR-002 leaves the provider undecided):
  ```
  🔍 Image matches · 5 results · provider: <name>
  numbered list: page title — domain (links)
  [🔎 More] [🖼 Similar only]
  ```
  The first use per provider asks for consent once: "This sends the photo (EXIF stripped) to <provider>. Allow?" `[Allow once] [Always] [Cancel]`.

### 6.8 Long-form content: Telegraph articles vs the other options (D17)
Long content has four possible homes. The decision is per content type:

| Content | In-chat rich message (≤ 32,768 characters) | `.md`/`.html` document | Telegraph article (Instant View) | Mini App (phase 2) |
|---|---|---|---|---|
| Long Hermes answers, research write-ups | **Default** | `answer.md` when the answer is > 3 parts | **Opt-in per message** when eligible (below) | No |
| Daily/weekly digest | **Default** | No | **No**: holds pending approvals, vetting state and personal stats | No |
| PR review content (cards, explorer, diffs, body, commit message, interdiff, vetting and maintainer notes, test logs) | Default (ADR-0019 A) | `.patch` + `.html` | **Never**: pre-approval and unreleased content; no highlighting, tables or details; 64 KB cap | Phase 2 only (ADR-0019 B3) |
| Images, health and nutrition data, feature cards, skill text | Default | No | **Never** | No |

**Eligibility.** The gatekeeper adds `[📖 Open as article]` to a `chat_answer` view only when **all** of these hold:
- `telegraph.enabled`;
- the answer is ≥ `telegraph.offer_min_chars` (default 1,500) and ≤ `max_content_kb` (60 KB) once converted;
- the turn had no attachments (`chat_turns.had_attachments = 0`);
- the turn made no `memory`, `session_search` or `ossbot_*` tool calls (they may carry personal or OSS data), checked against `chat_turns.tools_json`;
- the answer is not in a feature topic or in ✅ Reviews.

All three facts, and the answer text itself, come from the `chat_turns` row written when the turn finished (`answer_text`, `tools_json`, `had_attachments`, §14.2), never from the callback.

The user's question is never included. Only the answer text is published, plus the source links.

**Flow** (two taps, like any outward action):
1. `📖 Open as article` (token `article_publish`, bound to the answer message, 24 h) edits the button row to a confirm panel: "Publish as a public Telegraph page? Anyone with the link can read it; Telegraph can't delete pages — Unpublish only blanks it." `[📖 Publish](danger) [↩️ Cancel]`. The confirm token is single-use, 5 min.
2. On Confirm, the gatekeeper:
   - rebuilds the content from the stored answer (`chat_turns`, not from the callback);
   - runs the scans: policy secret patterns, its own secret values, the Kiro key hash check, `gitleaks stdin`, the confidentiality floor and denylist (ADR-0015), and the profile's personal values (email, phone, real name). There is no override; a hit gives "Not published: the answer contains <class>";
   - converts `md2blocks` output to Telegraph nodes. Only `p h3 h4 ul ol li pre code blockquote b i s u hr br` are used, and `a` with http(s) `href` only. No `img`, `iframe`, `video` or `figure`, so no remote media or tracking. Tables become `pre` text;
   - calls `createPage` with title = the first heading (or the first 60 characters) + ` · ` + 8 random base32 characters, which makes the path unguessable. `author_name` and `author_url` are empty by default (`telegraph.author_name` is optional), so no identity is attached.
3. A reply to the answer: `📖 <b>Published</b> · <title> · public link, not deletable (only blanked)`, with link preview on (Instant View) and `[📖 Open ↗](url) [📋 Copy link] [🗑 Unpublish]`.
4. **Unpublish** (confirm tap): `editPage` with title "Removed" and one paragraph "This page was removed by its author." The UI says plainly that cached copies, Instant View caches and anyone who opened it may still have the content.
5. **`/articles`** lists the published pages from `telegraph_pages`, with Unpublish buttons.

**Egress and secrets:**
- New egress category `telegraph` = `api.telegra.ph` for the **gatekeeper only** (proxy port 3150). It is rendered into the squid config **only while `telegraph.enabled: true`** in the active config snapshot, so the default egress surface is unchanged; enabling or disabling it is a human-confirmed `/reload`, which re-renders the proxy (the existing snapshot → render path). The `always_denied` entries below are unconditional.
- `api.telegra.ph`, `edit.telegra.ph` and `telegra.ph` join `always_denied` for every other profile, in every egress mode. The API accepts writes as plain GETs (`createAccount` and `createPage` work with query strings), so a prompt-injected `web_fetch` could otherwise create accounts and publish. The HTTPS proxy filters by hostname, not by path, so the reader host can't be split from upload paths. The reasoning is the same as for `uploads.github.com` (CR-002). This amends CR-002's egress lists. Cost: research can't read Telegraph pages. **[VERIFY]** the full Telegraph host list.
- `TELEGRAPH_ACCESS_TOKEN` lives in `gatekeeper.env` (0600). It is created by `ossctl telegraph setup`, which calls `createAccount` with `short_name: "ossbot"`. `revokeAccessToken` is available through `ossctl telegraph rotate`.
- `telegraph_writer.py` hard-codes the base URL `https://api.telegra.ph/` and the methods `createPage`, `editPage` and `revokeAccessToken`. It is importable only by `gatekeeper/telegraph.py`.

## 7. Notifications

### 7.1 Priorities
Every outbox row carries `priority` (new column). `telegram/notify.py` maps it to topic, sound and batching:

| Priority | Examples | Sound | Quiet hours | Batching |
|---|---|---|---|---|
| P0 critical | kill (any level), anomaly → pause/kill, surface check failed, firewall drift, review mirror repo public, mirror token too broad, audit anchor mismatch, submission crashed | always | **ignored (U7)** | never |
| P1 action | new approval cards (within the loud budget), a maintainer review or comment, a blocking CI failure on our PR, approval expiring in < 1 h, Hermes back after an outage that interrupted a chat, token expiry in < 14 days | yes | → silent | never |
| P2 FYI | stage done or failed (non-blocking), discovery found N, CI passed, dry-run done, mode changed (FYI), local operator action | silent | held for the digest | 2-minute window |
| P3 digest | counters, stars and followers, streaks, rehearsal counts | none | n/a | digest only |

The local-operator-action FYI (DESIGN §12.2) stays P2 but is **never** held, because it is a security FYI. It is sent silently right away.

### 7.2 Quiet hours, /quiet and /notify
- **Quiet hours:** `profile.yaml telegram.quiet_hours` (e.g. `"23:00-07:30"`) in `telegram.timezone` (IANA, e.g. `Asia/Jakarta`). During quiet hours P1 goes silent and P2 is held. The first loud message after quiet hours ends is a single P1 summary: "🌅 While you were away: 2 cards, 1 maintainer reply".
- **`/quiet`** picker: `[1 h] [3 h] [Until 08:00] [Until tomorrow] [Off]`. It stores `ux_prefs.quiet_until` (personal, immediate) and echoes `🌙 Quiet until <tg-time>`.
- **`/notify`** shows a toggle grid (✅/⬜, 2 per row) for: approvals, proposals, maintainer activity, CI, pipeline stages, discovery, digest daily, digest weekly, assistant-back notices. Toggles cover P1 and P2 only; P0 has no toggle.
- **What a toggle does: it mutes or batches, it never drops.** Turning a category off makes its messages silent (`disable_notification: true`) and, for P2, moves them into the next batch. Approval, proposal, rescope and ceiling-hit cards are **always delivered** (only their sound changes), because a card that never arrives is an approval that silently expires. The only things a toggle removes entirely are the two digest kinds (off = not generated).

### 7.3 Dedup, coalescing and batching
- **Dedup:** the existing `tg_outbox.dedupe_key`.
- **Coalescing:** an FYI with the same `coalesce_key` (`<cid>:<event_kind>`) within 10 minutes edits the earlier message instead of sending a new one, e.g. "CI: 3/9 → 7/9 → 9/9 ✅". "The earlier message" is the `result_message_id` of the newest `sent` outbox row with that `coalesce_key` (§14.2); if that row has no `result_message_id` (sent before this CR), a new message is sent.
- **Holding and batching:** a P2 row inside quiet hours or a 2-minute batch window is written with `status='held'` and a `next_attempt_at` at the window end. When the window closes, the outbox worker merges all due `held` rows of the same topic into one new row `📈 <b>N updates</b>` (OSS feed) with one line each, the full list in an expandable blockquote, and `[📥 Queue]` when any approval is involved; the merged rows get `status='merged'` and `merged_into = <new row id>` in the same transaction.

### 7.4 Digest (`digest.py`)
- **Daily** at `telegram.digest.daily` (default 21:30 local) and **weekly** on `telegram.digest.weekly` (default `Sun 18:00`). Both go to 📈 OSS feed as P2, silent.
- **Content:**
  - contributions opened, merged, closed and rejected (LIVE), plus dry-run rehearsals counted separately;
  - pending approvals with expiries;
  - PRs waiting on maintainers vs waiting on you;
  - review rounds;
  - median time to first maintainer response;
  - followers and stars gained;
  - streak;
  - top error classes;
  - Hermes usage (turns and models);
  - **proposals and merge rate by size class** (CR-004 §3 track record; weekly digest, and the daily digest when any proposal or merge happened that day).
- **Merge rate by size class** (`📏 <b>By size</b> · last 90 days · LIVE only`): a rich `table` (classic: an aligned `<pre>`), one row per class with data, from CR-004's proposal → PR → merge records:
  ```
  class  proposed  started  PRs  merged  rate
  xs           5        4    4       3   75%
  s            9        7    6       4   67%
  m            3        2    2       1   50%
  l+           1        1    1       0    —
  👤 yours     2        2    2       1   50%
  Auto size ≤ s · tune with /size
  ```
  - Rows `l`, `xl` and `xxl` are merged into `l+` while each has fewer than 3 PRs, so a single outcome doesn't read as a trend; "rate" is `—` below 3 PRs.
  - Autonomous classes count only autonomous items; `👤 yours` counts user-directed items of any class (they bypass the size preference, Amendment B).
  - "rate" = merged ÷ (merged + closed without merge), over PRs opened in the window; open PRs are counted in "PRs" but not in the rate. Dry-run rehearsals are excluded.
  - The last line names the current preference and the `/size` command; the digest has no button that changes it.
- **Followers:** `GET /users/{login}` (`followers`). **Stars:** the sum of `stargazers_count` over the user's non-fork public repos (`GET /users/{login}/repos?type=owner`, paged). Both are read with the gatekeeper's existing GitHub read client and snapshotted daily in `digest_runs.stats_json`. "Gained" is the delta against the previous snapshot.
- **Streak:** consecutive local days, ending today or yesterday, with at least one LIVE contribution event (PR opened, `push_update` pushed, maintainer-facing comment posted, PR merged).
- `/digest` → `[📨 Send now] [Daily ✅] [Weekly ✅] [⏰ Times…]`.

## 8. Controls

### 8.1 Commands
Safety classes:
- `read`: immediate.
- `stop`: immediate.
- `go` and `config`: always a confirm button (a message-bound, single-use, 5-minute token).
- `personal` (new): assistant and feature data and UX preferences. Immediate and undoable; no OSS, GitHub or control effect.
- `tune` (new, CR-004): autonomous-discovery preferences (`/size`, `/filters`, per-repo size). Immediate, audited, with `[↩️ Undo]` for 60 s, and a silent P2 FYI in 📈 OSS feed. It can only change what the bot *proposes*; it can't affect an approved proposal, a `👤` item, either gate or the hard ceiling (U13). Narrowing and widening are treated alike because neither can cause any action without gate 1 and gate 2.
- `request` (new, CR-004): `/work` and `🛠 Work on this`. Immediate and audited; it only creates a scouting item, which can't implement or publish anything before gate 1 (Start work) and gate 2 (Approve).

Arguments are chosen with pickers (`Picker`, ≤ 8 options per page, ≤ 3 per row, a pager row). Typed arguments are accepted and validated with the same pydantic model as the picker token's `arg`.

| Command | Class | Picker / flow | Notes |
|---|---|---|---|
| `/start` | read | Welcome + `/dashboard` | Global command |
| `/help` | read | Generated from the registry, grouped Control / Reviews / Assistant / Settings | Global command |
| `/settings` | personal | `[🖼 Rich views: auto] [🔕 Notifications…] [🌙 Quiet hours…] [📰 Digest…] [🐙 Mirror: off ⓘ]` | Mirror enabling is config (U9); the button explains how |
| `/status` | read | Full status text (existing) + `[📌 Dashboard]` | |
| `/dashboard` | read | Re-send and re-pin | |
| `/queue` | read | Ranked pager, gate 2 then gate 1 (§5.13) | |
| `/work <url> [notes]` | request | Typed only (the URL is the argument); without an argument: "Send /work <GitHub issue, PR or repo URL> [notes], or just paste a link" | CR-004 Amendment B; §5.19 |
| `/size` | tune | §8.4 picker | Autonomous discovery only |
| `/filters` | tune | §8.4 toggle pages | Autonomous discovery only |
| `/approve [cid]` | read | Picker of pending approvals → re-send the card (token rotation N4) | Never approves by itself (existing) |
| `/review [cid]` | read | Picker → explorer for that card | New |
| `/reject [cid] [reason]` | config | Picker → reasons `[Not valuable] [Wrong approach] [Repo unsuitable (7d)] [Other…]` → confirm | Existing behaviour |
| `/pause` | stop | Immediate | |
| `/resume` | go | Confirm: "Resume: scheduling restarts; approvals need Approve+Confirm again." | |
| `/kill [reason]` | stop | **Immediate** when typed; the dashboard button is two-tap (D14) | Soft kill (existing) |
| `/dryrun` | stop/go | Shows the current mode + `[🧪 Turn ON]` (immediate) or `[🔴 Turn OFF (LIVE)…]` → confirm: "LIVE mode: approved actions will publish as Amsal1. N open approvals will be re-issued." | Either toggle invalidates and re-issues cards (existing) |
| `/drain` · `/undrain` | stop · go | `/drain` immediate; `/undrain` confirm | CR-001 |
| `/down` | stop+confirm | Timeout picker `[60 s] [90 s] [120 s]` → confirm: "Down (workers): running stages get 90 s, then re-queue. The gatekeeper stays up so /up works." | Disruptive, so one confirm even though it is a stop. Result state: `💤 DOWN (workers)` (§4.3); the bot stays "🟢 Online" |
| `/up` | go | Confirm | Refused while killed (CR-001) |
| `/reload` | go | Diff → confirm (existing) | Also the only way to enable the mirror (U9) |
| `/egress` | config | `[allowlist] [research] [open ✅]` → confirm panel listing the categories per profile that change | CR-002 |
| `/hermes` | config | `tier` → `[locked] [research] [self_improve ✅]` → confirm with the toolset diff → re-runs the surface probe | CR-002 |
| `/model` | config | Role picker (`chat, vision, planner, coder, reviewer, summarizer`) → catalog pager (8 per page, the current one marked ✅) → effort picker when supported → confirm → writes `models.yaml` | Existing writer |
| `/repos [filter]` · `/repos <repo> size <class\|inherit>` | read/config/tune | Pager of targets with badges → repo card `[Enable/Disable] [Allow AI]` → confirm; plus `[📏 Size: m ▾]` (tune, §8.4) | Existing; the size row is CR-004 |
| `/limits` (`/budget`) | read | Usage vs limits table | |
| `/logs [cid] [stage]` | read | Contribution picker → stage picker → tail + `.txt` | |
| `/skills` | read | Staged and active skills; staged skills show `[Open card]` (§9) | New (CR-002 self_improve) |
| `/new` · `/newchat` | personal | New Hermes conversation (§6.6) | `/newchat` kept as an alias |
| `/title <text>` | personal | Rename the current chat topic | Topics mode only |
| `/memory` | personal | §6.6 | |
| `/quiet` · `/notify` · `/digest` | personal | §7 | |
| `/remind` | personal | List or cancel reminders (§13 item 3) | v1 integration |
| `/articles` | read | Published Telegraph pages with `[🗑 Unpublish]` (confirm) | Only when `telegraph.enabled` (§6.8) |

### 8.2 Confirmation patterns
- **Two-tap approval** (S1/S8) for every public action and for skill activation. Unchanged.
- **Command confirm:** the command reply carries one confirm button (existing `Reply.confirm`). The reply text states what will happen and what is invalidated. Buttons are `[✅ <verb>](danger for go/config that widens anything, primary otherwise) [↩️ Cancel]`. After use, the message is edited to the outcome, with no buttons.
- **Two-tap danger button** on the dashboard (`🛑 Kill`, `⬇️ Down…`): the first tap edits the keyboard to two single-button rows, `[🛑 Kill now · 10 s](danger)` and `[↩️ Cancel]`, and the message text gains the line `Tap 🛑 Kill now within 10 seconds to soft-kill.` The second-tap token expires after 10 s. A typed `/kill` stays immediate.
- **Skill activation Confirm** is styled `danger` (it widens what Hermes does, the rule above); proposal `Confirm start` is `primary` (it widens nothing).
- **Toasts** (`answerCallbackQuery`, ≤ 180 characters) are for transient acknowledgements ("Saved", "Cancelled; nothing was submitted."). `show_alert: true` is only for refusals that need attention ("Paused or killed: nothing was approved. /resume first.").
- **Picker confirm** for config pickers: the picker tap edits the message into the confirm panel, so there is one message per flow.

### 8.3 Why no ephemeral messages
Bot API ephemeral messages (10.2/10.3) are built for groups: a private overlay for one member. They have `message_id` 0, "it is not guaranteed that the ephemeral message will be received", and they can disappear on app restart. In a private chat with a single user every message is already private. A confirm surface must be durable and message-bound (U3).

ossbot therefore uses in-place edits plus toasts. If Telegram later supports ephemeral messages in private chats, they may be used for transient help text only, never for tokens.

### 8.4 `/size` and `/filters` (CR-004 Amendment B; `11-size-filters.svg`)
Both are `tune`-class (§8.1) pickers in one message each, edited in place, sent in the originating thread. They read and write only through `gatekeeper/discovery_prefs.py`:
- `DiscoveryPrefs.effective(repo | None) -> Prefs` returns the effective autonomous size class, preferred classes and filters (per-repo row, else the global row, else `config/policy.yaml` defaults).
- `DiscoveryPrefs.set(scope, field, value, actor) -> Change` validates, writes one row in `discovery_prefs` (§14.2), audits `discovery_pref_set {scope, field, old, new}` and returns the change for Undo. It refuses any field outside the allowlist (`autonomous_max_class`, `preferred_classes`, the filter keys); ceiling keys are not in it (U13).
- `ossctl size set` and `ossctl filters` use the same service. CR-004's design owns the semantics of these values; if it moves their storage, only `discovery_prefs.py` changes.

**`/size`:**
```
📏 <b>Size preference</b> · autonomous discovery only
Bot proposes up to <b>s</b> · ≤ 80 code lines · ≤ 4 files
Preferred: xs, s · test lines never count
👤 Your /work requests ignore this.
Per repo: langfuse/langfuse ≤ m · ollama/ollama ≤ xs
Ceiling (config only): 80 files · 3,000 code · 3,000 test
Overrun: flag on the PR card (policy.size.on_overrun)
Merged, 90 d: xs 3/4 · s 4/6 · m 1/2 → /digest
[xs ≤20]  [✅ s ≤80]  [m ≤250]
[l ≤500]  [xl ≤1000]
[⭐ Preferred…] [🏷 Per repo…]
[↩️ Undo]                                  (60 s after a change only)
```
- A class tap sets `autonomous_max_class` immediately, edits the message (the ✅ moves), and answers a toast `Auto size ≤ m. Proposals already sent keep their size.`. `xxl` is not offered. Class bounds shown are the configured `size.classes` values.
- `⭐ Preferred…` swaps the keyboard to multi-select toggles `[✅ xs] [✅ s] [⬜ m]` / `[⬜ l] [⬜ xl]` / `[↩️ Back]` (ranking preference; at least one must stay on, else the toast "Keep at least one preferred class").
- `🏷 Per repo…` swaps to a repo picker (repos with an override first, then the rest of the enabled targets, ≤ 8 per page, labels `owner/repo` cut to 24 with a middle ellipsis) and then a class picker `[Inherit (s)] [xs] [s]` / `[m] [l] [xl]` / `[↩️ Back]`. The same picker is the `[📏 Size: m ▾]` row of the `/repos` repo card. Per-repo overrides apply to autonomous discovery only.
- The merge-rate line is the digest's 90-day numbers in compact form (§7.4); classes with < 3 PRs are omitted.
- Typed: `/size m` (same validation; `xxl` → "xxl can't be a preference; user-directed work has no size limit below the ceiling").

**`/filters`** (three pages, one message; the page name is in the text, the pager row navigates):
```
🧰 <b>Discovery filters</b> · autonomous only · 1/3 Kinds
✅ = the bot may propose this kind. 👤 /work ignores filters.
[✅ Bug] [✅ Docs]
[✅ Tests] [✅ Small feature]
[✅ CI fix] [✅ Small refactor]
[⬜ Breaking change] [⬜ New dependency]
[⬜ API redesign] [⬜ Crypto/security]
[◀ More] [↩️ Undo] [Labels ▶]
```
- Page 1 **Kinds**: the union of CR-004's `allowed_kinds` and `excluded_kinds` defaults, one toggle each (✅ allowed / ⬜ not). Stored as `allowed_kinds`; `excluded_kinds` is its complement. Display names are fixed constants (≤ 16 characters), never repo or model text.
- Page 2 **Labels and tier**: `require_labels_any` toggles for `good first issue`, `help wanted`, `bug`, `documentation` and `[⬜ Any label]` (off = no label required), then the validation tier as a radio row `[T0] [✅ T0–T1] [T0–T2]`.
- Page 3 **More**: `max_issue_age_days` `[30 d] [90 d] [✅ 180 d]` / `[365 d] [Any]`; `min_maintainer_activity` `[Any] [✅ 30 d] [90 d]`; languages from `profile.yaml` as toggles (≤ 6, then "+N in config"); `max_open_prs_by_me_per_repo` `[✅ 1] [2] [3]`.
- Each toggle applies immediately, edits the message and arms `[↩️ Undo]` for the last change (60 s). Every page has ≤ 5 toggle rows plus the 3-button pager row.

Both pickers fit the §11.4 budgets: ≤ 600 characters of text, ≤ 6 rows, multi-button labels ≤ 24 characters (the longest is `⬜ Breaking change`, 17), `callback_data` 24 bytes.

## 9. Skill approval card (CR-002 self_improve; `08-skill-approval.svg`)
A staged skill (new or changed) produces an approval card in ✅ Reviews, with the same machinery as a PR card: verbatim parts, delivery before Approve, two taps, single-use message-bound tokens.

Binding: `{v:1, kind:"skill_activate", skill, version, files_sha256 (sorted path→sha256), tools_requested, tier, policy_hash}`. A new staged version, a tier change or a reload invalidates it.

**Mode.** Skill activation is a **local** change (root copies files into Hermes's read-only skills dir); dry-run governs public GitHub writes only, so activation happens the same way in both modes. The card still carries the mode badge, marked as not applicable (S3), so a missing badge never has to be interpreted.

**Machinery.** Skill decisions run through `SkillApprovals` (FEAT-009's `skill_reviews` rows; purposes `skill_approve`, `skill_confirm`, `skill_cancel`, `skill_reject`), with its own `confirm_text()`. `ApprovalService.confirm_text()` for the four outbound kinds is untouched (§12.4 delta 4).
```
🧠 <b>Skill ready for approval</b> · nutrition-log v3 (update) · s_01JXYZ · 🧪 DRY RUN (n/a: local change)
Requested by Hermes (tier self_improve) · 🤖 "<reason ≤ 200 chars>"
Tools requested: ossbot_feature_card, memory · ✅ all within tier
Files: SKILL.md +54 −12 · examples.md +20 (new) · total 4.1 KB
Checks ✅ size ≤ 64 KB · ✅ no tool beyond tier · ⚠️ 2 URLs · ✅ no hidden chars
sha256 9f2c…e1a0 · staged <tg-time r>
```
- Then the verbatim parts: each file as `<pre>` parts (`SKILL.md (i/K)`), and for an update, the diff against the active version in `language-diff` parts.
- Keyboard:
  - Row 1: `[✅ Approve skill](success) [❌ Reject]`
  - Row 2: `[🧾 Full diff] [💬 Ask Hermes why]`
- `Ask Hermes why` relays "Explain why you want skill nutrition-log v3 and what it will do" to the Assistant session. The answer is labelled 🤖 and never touches the card.
- Confirm text (`SkillApprovals.confirm_text`): `Confirm: activate skill nutrition-log v3 (sha256 9f2c…e1a0) for Hermes — local change, dry-run does not apply. Tools stay within tier self_improve. Expires in 5 min.` With LIVE mode the badge reads `🔴 LIVE (n/a: local change)` and the sentence is the same.
- Confirm button: `[🧠 Activate skill](danger) [↩️ Cancel]`. Danger, because activation widens what Hermes does (§8.2).
- After Confirm, root copies the skill to the active dir and hashes it (CR-002). The card is finalized `🧠 Active · v3` with `[↩️ Deactivate…]` (config confirm).
- Deterministic check lines:
  - URLs found (`https?://` count);
  - tool names mentioned in the text vs the allowlist;
  - size;
  - invisible characters;
  - the strings `approve`, `confirm`, `dry-run`, `token` mentioned (⚠️ only, informational).

## 10. Error and offline states (`09-offline-error.svg`)

| Situation | Detected by | What the user sees, and when |
|---|---|---|
| Laptop asleep, off, or offline | Nothing can answer: the gatekeeper is the only poller | His messages show as sent with no reply. The pinned dashboard footer keeps ageing ("Updated 3 hours ago"). After a **full** graceful down (`down.ps1` / `ossctl down`), the dashboard reads `💤 DOWN (graceful) since 22:14`, and the bot's short description (profile) reads "💤 Offline since 22:14 — wake the laptop to reconnect". A sleep without a graceful down leaves the description at "🟢 Online"; only the ageing footer shows it. Telegram keeps unfetched updates ≤ 24 h |
| Back online | Gatekeeper start / poller reconnect | Within seconds of the first successful `getUpdates`: one Control message, `⏰ <b>Back online</b> · offline 8 h 52 m (graceful down) · 2 messages waiting`, then each pending update handled per §4.4 stale rules, with its age shown. The dashboard refreshes and the short description returns to 🟢 |
| Network flaps while up | PollStats | Nothing until 3 consecutive failures over ≥ 2 min; then the dashboard line `Telegram: reconnecting (3 failures)` shows on the next successful edit. Reconnects are counted in `/status` (CR-001) |
| Soft kill | Control state | Every non-allowed command: `🛑 <b>Killed (soft)</b> since <tg-time> · reason <code>hermes_tool_surface</code>. Only /status, /resume, /logs, /help and /start work.` with `[▶️ Resume…] [📜 Logs]`. Assistant messages: `🤖 Assistant is stopped while killed.` The dashboard header turns 🛑. The command menu is reduced (§4.2) |
| Full kill / `kill.ps1` | Gatekeeper stopped | Same as offline (no answers). The FYI arrives at the next gatekeeper start ("Killed (full) at 14:02 by kill.ps1; resumed at …") |
| Paused | Control state | Approve and Confirm refused with the existing toasts; the dashboard shows `⏸ PAUSED · <reason>` |
| Draining or `💤 DOWN (workers)` (Telegram `/down`) | Lifecycle | The gatekeeper is **up** and answers commands. Approve, Confirm and proposal Start work are refused with the existing lifecycle toast ("ossbot is draining: nothing was approved. The card stays valid; retry after up.", and the same sentence with "down (workers)"); the dashboard shows `🟡 DRAINING` or `💤 DOWN (workers)` with `[⬆️ Up…]`; the short description stays "🟢 Online"; assistant messages get `🤖 Assistant is stopped (workers down) — /up` |
| Hermes not responding (connect refused, timeout, 5xx) | Chat relay | Immediately: `🤖 <b>Assistant unavailable</b> — Hermes not responding since <tg-time>. OSS commands still work.` with `[🔁 Retry]` (a token holding a reference to the message). When Hermes is back, one silent `🤖 Assistant is back.` (P1 only if a chat was interrupted). Never auto-resent |
| Hermes probe inconclusive (auth/network) | Relay gate (DESIGN §15.2) | `🤖 Assistant unavailable (safety probe pending).` No kill |
| Hermes surface drift | Surface check | P0 alert, soft kill (existing) |
| Rich message refused (400) | Outbox | Nothing visible: the classic fallback renders the same view. `rich_unavailable_until = now + 24 h`; WARNING once per day |
| Card message deleted by the user | Edit returns "message to edit not found" | The approval stays pending; `/queue` → `Open` re-sends it |
| Telegram API outage | Outbox errors | Cards and alerts persist in `tg_outbox` and are delivered on recovery (existing); the dashboard can't update |

## 11. Accessibility and readability

### 11.1 Icon vocabulary (emoji as icons)
One leading icon per line at most. Every status icon is followed by a word, so screen readers ("check mark lint") and colour-blind readers get the meaning from text. Icons never appear inside verbatim blocks.

| Icon | Meaning | Icon | Meaning |
|---|---|---|---|
| 🧩 | PR candidate | 🔁 | revision / fix round |
| 💬 | comment / chat | 🙋 | issue claim |
| 🧠 | skill / memory | 🚀 | submitted / LIVE confirm |
| 🎉 | merged | 🗂 | closed |
| 🧪 | DRY RUN | 🔴 | LIVE |
| ✅ | verified pass / approve | ❌ | fail / reject |
| ⚠️ | warning | ⏳ | pending / in progress |
| ⏭ | skipped | ⛔ | not approvable |
| 🤖 | model-authored claim (U6) | 🧭 | explorer |
| 🧾 | files / diff | 🔀 | interdiff |
| 🚦 🟢 🟡 💤 ⏸ 🛑 | control / up / draining / down / paused / killed | 🐙 | GitHub mirror |
| 📈 | feed / digest | 📥 | queue |
| 🔎 📄 | web search / page | 🌙 | quiet |
| 💡 | proposal (gate 1, CR-004) | 👤 | requested by you (user-directed) |
| 📏 | size / re-approval | 🧱 | hard ceiling reached |
| 🛠 | work request; tool (assistant status line) | 🔗 | GitHub link card |

Button colour (`style`) always repeats a word ("Confirm LIVE"), never replaces it.

### 11.2 Layout grammar
- **Message:**
  - header line: `<icon> <b>Title</b> · <id> · <MODE>`;
  - subject (1–2 lines);
  - facts, each `Label value · label value`, ≤ 8 lines;
  - details in expandable blockquote / `details`;
  - footer `<i>…</i>` with IDs and `<tg-time>`.
- **Keyboard:**
  - row 1: primary decision, positive left and negative right;
  - row 2: explore and navigation;
  - row 3: edit;
  - row 4: links (URL buttons need no token).
- **Sizes:** ≤ 2 buttons per row when any label is > 12 characters, otherwise ≤ 3. ≤ 4 rows on cards and ≤ 5 on pickers (plus one pager row).
- **Label length, one rule, asserted per row** (counted in Unicode code points after NFC; an emoji with VS16 counts 2):
  - in a row with **two or more** buttons: ≤ 20 target, **24 hard cap**;
  - in a **single-button** row (follow-ups, the Kill second tap, `Use as comment`, disabled status buttons): **≤ 64 hard cap**.
  - Over the cap, constant labels fail the unit test (they must be shortened in code); labels built from untrusted or model text are **dropped**, not cut (follow-ups, §6.4), or cut with a middle ellipsis when they are names (repo, file and topic names).
- Labels start with a verb or a noun the action is about (`Files`, `Details`).
- **Pager row:** `[◀] [i/N ▾] [▶]`. The disabled ends use `DisabledButton` when available, otherwise they are omitted.

### 11.3 Escaping and rendering modes
- **Classic** (parse mode HTML, the existing `escape.py`):
  - `esc()` for text and `esc_attr()` for `href`;
  - tag allowlist `b i u s code pre a blockquote(expandable) tg-spoiler tg-time`;
  - links only for `http(s)://`;
  - untrusted text goes through `make_visible` then `defuse_commands` (U8);
  - bytes that will be published always in `<pre>` (unchanged).
- **MarkdownV2 is never used.**
- **Rich:** `blocks` only. Every string a plain `RichText` string, `skip_entity_detection: true` on every message, links as explicit `RichTextUrl` (http(s) only), no `tg-button` blocks for decisions (U3). The rich `html` and `markdown` modes are allowed only for constant templates with no interpolation. In practice the kit never uses them.
- **Rich views setting:** `ux_prefs.rich_views ∈ {auto, on, off}`, default `auto`.
  - `auto` = rich for explorer, file, interdiff, tests, assistant and feature views, unless `rich_unavailable_until` is in the future.
  - Approval cards and confirms are always classic.
- **Button labels from untrusted or model text** (follow-ups, feature items, topic names, repo and file names): `make_visible` (reject when any hidden character is found), collapse whitespace, no leading `/`, and the §11.2 length rule for the row they are in (follow-ups: single-button rows, ≤ 64, dropped when longer).

### 11.4 Limits and chunking (enforced in `telegram/ui/limits.py`; property-tested)

| Item | Telegram limit | ossbot budget |
|---|---|---|
| Classic text | 4,096 characters after entity parsing | ≤ 3,900 escaped characters; `<pre>` parts ≤ 3,500 (existing packer, tags balanced across parts) |
| Rich message | 32,768 characters, 500 blocks, 16 levels, 50 media, 20 columns | ≤ 30,000 characters, ≤ 450 blocks, ≤ 8 levels, ≤ 20 media, ≤ 12 columns; split at block boundaries; one `pre` ≤ 12,000 characters (split at lines) |
| Caption | 1,024 | ≤ 900 |
| `callback_data` | 1–64 bytes | Always `t:<22>` = 24 bytes |
| Callback answer text | 0–200 | ≤ 180 |
| Button label | not documented | Multi-button rows ≤ 20 target / 24 hard; single-button rows ≤ 64 hard (§11.2) |
| Buttons | not documented (observed ≤ 8 per row, ~100 per keyboard) | ≤ 3 per row, ≤ 6 rows |
| ForceReply placeholder | 1–64 | ≤ 60 |
| `copy_text` | 1–256 | ≤ 256 (title, URL, branch) |
| Topic name | 1–128 | ≤ 48 |
| Commands | ≤ 100; 1–32 `[a-z0-9_]`; description 1–256 | ~30; descriptions ≤ 60 |
| Draft text | 0–4,096 (rich: rich limits) | Partial answer tail ≤ 3,500, plus the status line |
| Bot description / short description | 512 / 120 | ≤ 200 / ≤ 100 |
| Send rate | ~1 message/s per chat | Outbox ≤ 1 call/s; the draft lane yields; dashboard ≤ 1 edit/30 s; status card ≤ 1/60 s; progress ≤ 1/5 s |
| File download / upload | 20 MB / 50 MB | Attachments ≤ 20 MB (CR-002); logs zipped > 45 MB (existing) |

## 12. UI component kit (`src/ossbot/gatekeeper/telegram/ui/`)

### 12.1 Modules and API
- `kit.py`: the components. Each returns a `View`: `{method, payload (classic), rich_payload | None, buttons: list[ButtonSpec], view_kind, state}`.
  - **Budget rule.** A card is built from blocks marked `required` or `optional` with a drop priority. When the rendered text would exceed the budget (§11.4), the kit removes optional blocks in priority order (lowest value first) and re-renders; it never cuts inside a required block and never cuts trailing lines. Required on every card: the header, the mode line, the subject/title line, the size line and its ⚠️ highlight, and the expiry line. If the required blocks alone exceed the budget, rendering raises `ViewTooLarge` (a bug, ERROR) and the caller sends the card without optional blocks plus "details → 🧭". This replaces the current `cards.render` tail cut (`card[:MESSAGE_MAX-20].rsplit("\n",1)[0] + "\n…"`) for every card kind.
  - `card(header, subject, facts, sections, footer)`
  - `fact(label, value)` (✅ verified) vs `claim(label, text)` (🤖, quoted; U6)
  - `list_view(items, page, per_page)` + `pager()`
  - `confirm(sentence, mode)`
  - `picker(options, selected, multi)`
  - `progress(steps)`
  - `toast(text)`
  - `doc(path, caption)`
  - `stub(text)` (for superseded messages)
  - `button(label, purpose, arg, style=None, single_use=False, ttl)` and `url_button`, `copy_button`, `disabled_button`.
- `blocks.py`: typed builders for `InputRichBlock`/`RichText` (pydantic models mirroring Bot API 10.3, `extra="forbid"`). The only code that builds rich payloads.
- `classic.py`: the HTML renderer of a `View` (wraps the existing `escape.py` and the chunker).
- `rich.py`: the rich renderer of a `View`, plus the fallback decision.
- `sanitize.py`: `defuse_commands`, `undefuse` (U8), `label_from_untrusted`, `domain_of`, `github_urls` (the link parser for routing rule 6 and `/work`).
- `limits.py`: the constants of §11.4 and the packers.
- `icons.py`: the §11.1 vocabulary as constants. Tests forbid other leading emoji in kit templates.
- `send.py`: turns a `View` into outbox rows (`message_thread_id` from `topics.py`, `disable_notification` from `notify.py`), then mints and binds tokens (§12.2).

### 12.2 Tokens for kit components ("mint then bind")
Approval and Confirm tokens (PR card, proposal card, skill card) keep the reviewed code path: minted after delivery and attached by `editMessageReplyMarkup` (U3). Every other kit button uses "mint then bind", made crash-safe by the outbox:
1. `send.py` mints the token rows with `tg_message_id = NULL` and writes **one** outbox row carrying the payload, `bind_tokens_json` (the list of those tokens) and `view_json` (the `tg_views` row to create: kind, `cid`, `rev`, `path`, `page`, `state_json`), in one transaction.
2. The worker sends the message.
3. `Outbox.mark_sent(row, result)` runs **one** transaction that sets `status='sent'`, writes `result_message_id = result.message_id`, runs `UPDATE ui_tokens SET tg_message_id = :mid WHERE token IN (:bind_tokens) AND tg_message_id IS NULL`, and inserts or updates the `tg_views` row from `view_json` with `message_id = :mid`. The existing `on_sent` hook (card IDs, part delivery counting) still runs afterwards, but nothing the kit needs depends on it any more, so the crash window between `mark_sent` and `on_sent` can no longer lose a binding or a view. `CardPublisher.on_sent` itself is changed to read `result_message_id` when it re-runs after a restart (idempotent: `set_card_message` and `mark_part_delivered` are keyed by the outbox row).

An unbound token is never accepted, because the handler requires `tg_message_id == callback.message.message_id` and NULL never matches (verified in `check_token`). This halves the API calls per interactive message without weakening the binding.

**`approval_id` on kit tokens.** Every non-approval kit token is minted with **`approval_id = NULL`**, and with `cid` set and `arg` containing `{"cid": …, "rev": …}` plus its own fields. Its handler resolves the current approval for `(cid, rev)` at tap time (`SELECT … FROM approvals JOIN outbound_actions … WHERE cid = ? AND status IN ('pending','armed')`), and refuses with the "Superseded…" toast when `tg_views.stale = 1` or no current approval exists for that `rev`. Because `_kill_tokens(approval_ids)` only touches rows with that `approval_id`, a Cancel, a `reissue()` or an invalidation never kills explorer navigation, file views, comment prompts, draft-review buttons or Undo. Only the card's own decision tokens (`CARD_PURPOSES`, `confirm`, `cancel`) carry `approval_id`.

Token purposes added (approval_id NULL unless noted):
- `nav`: multi-use, 24 h, arg `{cid, rev, view, section|page|path}`.
- `explore`, `files`, `interdiff`, `edit_text_pick`: card buttons; they carry `approval_id` because they are minted by `issue_card_tokens` (§12.4 delta 3), but their handlers only open views, so their death on rotation is harmless (the rotated card has fresh ones).
- `comment_hunk`, `comment_line_pick`, `review_send`, `review_send_now`, `review_discard`, `comment_undo`, `file_pick`.
- `body_file`.
- `show_card`, `stale_retry`, `queue_open`, `queue_sort`, `picker`, `confirm_cmd` (existing pattern), `danger_second_tap` (10 s).
- `chat_followup`, `chat_retry`, `chat_stop_fallback`, `use_as_comment`.
- `quiet_set`, `notify_toggle`, `digest_set`, `settings_set`.
- `size_set`, `size_pref_toggle`, `size_repo_pick`, `filters_toggle`, `filters_page`, `tune_undo` (§8.4).
- `work_on_link`, `work_cancel`, `link_dismiss` (§5.19).
- `proposal_skip`, `proposal_adjust`, `proposal_smaller`, `proposal_never`, `proposal_never_confirm`, `proposal_later`, `proposal_details`, `proposal_ask_first`, `proposal_undo`, `ceiling_recheck`, `item_drop`, `item_drop_confirm` (§5.15–§5.18).
- `feature_action`.
- `article_publish` (24 h), `article_confirm` (single-use, 5 min), `article_unpublish` (§6.8).
- Decision tokens of the separate services (carry their own approval ids): `proposal_start`, `proposal_confirm`, `proposal_cancel` (`ProposalApprovals`, ids `pa_…`); `skill_approve`, `skill_confirm`, `skill_cancel`, `skill_reject` (`SkillApprovals`, ids = `skill_reviews.id`). The id prefixes (`a_`, `pa_`, the skill-review prefix) must differ, so a token can never be resolved by the wrong service; each service also checks the purpose set it accepts.
- `mirror_open`, `copy_*` and `Open PR` are URL or `copy_text` buttons, so they need no token.

A daily GC deletes expired tokens older than 7 days.

### 12.3 Guide: adding a feature (also goes into AGENTS.md)
1. **Package:** create `src/ossbot/gatekeeper/features/<name>/` with `__init__.py` exposing `FEATURE = FeatureSpec(name, version, schema_models, store_migrations, cards, actions)`. Register it with the entry point group `ossbot.telegram_features` in `pyproject.toml`.
2. **Data:** pydantic models (`extra="forbid"`, bounded strings and numbers) for every `data` payload Hermes may send. Validation errors return 422 to the plugin and are never shown raw.
3. **Store:** SQLite at `/srv/ossbot/gatekeeper/features/<name>.db`, with numbered migrations in the package. The feature may not open any other DB.
4. **Cards:** functions `View <- (data, prefs)` built only with `telegram.ui.kit`. Golden tests in `tests/telegram/features/test_<name>_cards.py` cover LIVE-independent rendering, classic and rich, and limits.
5. **Actions:** `Action(name, safety ∈ {read, personal}, handler, input=None|"force_reply")`. `go` and `config` are not allowed for features (import-linter contract: `ossbot.gatekeeper.features` cannot import `approvals`, `submit`, `github_writer`, `review_mirror_writer`, `state_machine`, `control`).
6. **Hermes side:** a skill (self-written, approved through §9, or hand-authored) or a plugin tool that calls `ossbot_feature_card(feature, kind, data)`. External providers need a secret in `setup-secrets.ps1` (documented key name, optional) and an egress category if the gatekeeper or a sandbox calls them.
7. **Docs and tests:** one line in README's feature list. Run `pytest -m "unit or telegram"`.

### 12.4 Token-core deltas (the only exceptions to §16)
The review UI needs six bounded changes in `approvals.py` and its callers. Nothing else in the token or binding core changes: the token format and checks (`TOKEN_RE`, `check_token`, `use_token`), `compute_binding`, `binding_hash`, `_recheck_binding`, `confirm_text` for the outbound kinds, `approve`, `confirm`, `consume` and `_kill_tokens` stay as they are.

| # | Delta | Where | Why | Test |
|---|---|---|---|---|
| 1 | **Non-approval tokens have `approval_id = NULL`** and carry `{cid, rev}` in `arg`; their handlers resolve the current approval at tap time (§12.2). No code change in `_kill_tokens`; the delta is a rule for every new mint site, enforced by a test | `telegram/ui/send.py`, `review/*` | Cancel, `reissue()` and invalidations kill every token with the approval's id; review navigation must survive them | `test_kit_tokens_survive.py`: arm → Cancel → explorer `nav`, file `nav`, `review_send` and `comment_undo` still work; `reissue()` → same; invalidation (`new_package_rev`) → they answer "Superseded" via `tg_views.stale` |
| 2 | **`ApprovalService.disarm_expired()`** (new), called by the housekeeping tick (≤ 10 s): `armed` and `armed_at <= now − confirm_ttl_seconds` → `pending`, `armed_at = NULL`, unused `confirm`/`cancel` tokens of that approval marked used, `rotate_card_tokens`, card edited back to S1, audit `approval_disarmed {reason:"confirm_expired"}`. `ProposalApprovals` and `SkillApprovals` get the same method | `approvals.py`, `app.py: housekeeping_once`, `CardPublisher` | No path exists today; a timed-out Confirm leaves dead buttons and an approval nobody can re-arm (`issue_card_tokens` refuses non-`pending`) | AC 29 |
| 3 | **`CARD_PURPOSES = ("approve","reject","reject_reason","changes","explore","files","edit_text_pick")`**; revised cards (rev > 1 with an interdiff) mint `interdiff` instead of `files`. `edit_title`, `edit_body`, `clear_edits` move behind the `edit_text_pick` picker as kit tokens | `approvals.py`, `keyboards.card_rows` | The regrouped card (§5.1) | Golden test of the minted purpose set per action kind and rev |
| 4 | **Separate decision services.** `SkillApprovals` (FEAT-009 `skill_reviews`; own `confirm_text`) and `ProposalApprovals` (CR-004 proposal approvals; own binding and `confirm_text`). Both reuse `mint_token`/`use_token`/`check_token` and the U3 card path; neither calls `ApprovalService.confirm_text`, whose dict lookup over the four outbound kinds would raise `KeyError` | `gatekeeper/skill_approvals.py`, `gatekeeper/proposal_approvals.py` | Skills and proposals are not outbound actions (`approvals.action_id` references `outbound_actions`) | Each service's two-tap golden tests; `confirm_text` of `ApprovalService` unchanged (existing tests) |
| 5 | **One `/approve` semantics: the code's `reissue()` wins.** `/approve <cid>`, `⬇️ Show card`, `⬇️ Show current`, `/queue → Open` and S12 `🔁 Retry` all call `reissue()`: old open approvals invalidated with `reissued`, fresh approvals for the same `(cid, rev)`, card and parts re-sent. DESIGN §14.5 ("same approval, armed → pending") is amended to match. Draft comments are keyed by `(cid, rev)` (§5.5), so they survive | `approvals.reissue` (unchanged code), DESIGN §14.5 text | `reissue()` is already reviewed (N4), works for expired approvals too, and leaves no armed state behind; keeping the same approval would need new code to reset delivery counters | `test_reissue_keeps_draft.py`: 3 draft comments → Show card → draft review still lists 3 and Send works |
| 6 | **`Outbox.mark_sent` binds tokens and writes `tg_views` in its transaction** (§12.2), plus the new `tg_outbox` columns (§14.2) | `outbox.py` | Closes the crash window between `mark_sent` and `on_sent` | `test_outbox_bind_atomic.py`: crash injected after `mark_sent` → tokens bound and view present after restart |

## 13. Integration hooks (prioritized)
v1 means it fits inside the existing architecture and is built with CR-003. v1.x is small and comes next. Phase 2 is heavier or optional. None of these adds a public GitHub write. Any that sends user data to a third party asks for consent on first use.

| # | Integration | Value | Secrets / providers | Safety notes | When |
|---|---|---|---|---|---|
| 1 | Digest stats (followers, stars, merges, streak) | Motivation and an overview in one message | None (gatekeeper GitHub read client) | Read-only GETs | **v1** |
| 2 | Own-PR activity (reviews, mentions, CI) on the status card | Never miss a maintainer | None (runner watch, existing) | Read-only; P1 batching | **v1** |
| 3 | Reminders (`/remind`, or Hermes `ossbot_reminder_create(when, text)`) | "Remind me to re-check PR #5678 tomorrow 9am" | None | Fired by the gatekeeper scheduler (Hermes cron stays off). Text is untrusted → escaped. ≤ 50 active. Personal class | **v1** |
| 4 | Food photo → protein estimate (CR-002 live check) | Quick nutrition answers | None (vision role) | Image EXIF stripped; the estimate is labelled 🤖 | **v1** (answer only) |
| 5 | Nutrition log with daily totals (first kit feature, §6.7) | Track protein intake over days | None | Personal data in a local feature DB; never synced; included in backups (DESIGN §18.8) only if `backup.include_features` | v1.x |
| 6 | Web clipping / read-later (`/clip <url>` or forward a link) | Save and summarize articles for later search | None (Hermes web via 3141) | Fetch happens in Hermes's egress; stored text capped at 200 KB per clip; untrusted content rendered as data | v1.x |
| 7 | GitHub notifications inbox (beyond our PRs) | Triage mentions and review requests | Classic token with only the `notifications` scope (the Notifications API is expected to need a classic token; **[VERIFY]**) | Read-only; a separate token in `gatekeeper.env`; P2 batching | v1.x |
| 8 | Upstream release watch for tools Amsal uses (Terraform, Kubernetes, Hermes, kiro-cli) | SRE awareness; feeds discovery | None (GitHub releases API, read token) | Read-only | v1.x |
| 9 | Document reading (PDF → text) | "Summarize this PDF" | None (sandbox `extract_text`, `--network none`) | Parsing in the sandbox, never in the gatekeeper | v1.x |
| 10 | Voice transcription | Talk instead of type | Local whisper.cpp in a `--network none` sandbox, or a provider key | Audio leaves only if a provider is chosen (consent once) | v1.x |
| 11 | Reverse image search (§6.7) | Find an image's source | SerpAPI Google Lens / Bing Visual Search / Google Cloud Vision (user decision, CR-002) | The photo goes to a third party: consent per provider, EXIF stripped, results rendered as data | Phase 2 |
| 12 | Image tools (OCR, resize, background removal) | Utilities from the phone | None (sandbox images) | `--network none` sandbox job | Phase 2 |
| 13 | Calendar (today's agenda in the digest; create events) | Plan PR review time | Google OAuth refresh token (new secret) | Read by default; creating an event is `config` with a confirm | Phase 2 |
| 14 | Healthcheck heartbeat (healthchecks.io-style) | External "laptop silent" alert during configured active hours | Ping URL (secret) | Outbound only; noisy when the laptop sleeps on purpose, so off by default | Phase 2 (optional) |
| 15 | Inline mode (`@bot query` in other chats) | Ask Hermes from any chat | None | Owner-only (`from.id` filter); assistant answers only, never OSS data; off by default | Phase 2 |
| 16 | Mini App review dashboard (ADR-0019 B3) | Full web diff viewer | Tailscale (Funnel) account | S4 rules: optional, read-only, Ed25519, no secrets, never approves. View-only by default; comments, if ever added, go through a temporary one-button reply keyboard (`sendData` works only from reply-keyboard launches, ≤ 4,096 bytes per batch, closes the app) | Phase 2 (only if A + C fall short) |
| 17 | Weather/news line in the digest | Daily context | Open-Meteo (no key) | Read-only public API via Hermes | Phase 2 |
| 18 | Telegraph articles for long assistant answers (§6.8, D17) | Instant View reading of long write-ups | `TELEGRAPH_ACCESS_TOKEN` (gatekeeper) | Public and undeletable: opt-in per message, two taps, scans, eligibility rules, gatekeeper-only egress (U11) | v1.x, off by default |

## 14. Implementation

### 14.1 Files

Python stack, locked: Python 3.12, **python-telegram-bot 22.8** (transport, long polling, dispatch and typed objects up to Bot API 10.0; Bot API 10.1–10.3 through `Bot.do_api_request` with typed pydantic payload models), FastAPI over UDS, httpx, pydantic 2, Jinja2 (HTML diff) and SQLite. The "no new runtime dependency" constraint is lifted, and the library choice was re-made on the merits in ADR-0019 Addendum A: PTB stays (lenient decoding of unknown fields, LGPL-3.0, zero migration, existing fake-Bot-API tests), aiogram and pyTelegramBotAPI are rejected, and **no new runtime dependency is added by this CR** (Pillow included, §6.5). When PTB ships typed 10.1+ support (v23), `rawapi.py` call sites switch to typed methods behind the same kit API; that upgrade is a separate change with its own review.

| Path | Change |
|---|---|
| `src/ossbot/gatekeeper/telegram/ui/{__init__,kit,blocks,classic,rich,sanitize,limits,icons,send}.py` | **New** component kit (§12) |
| `src/ossbot/gatekeeper/telegram/review/{explorer,files,comments,interdiff_view,status_card,queue,stale,edit_text}.py` | **New** review flow (§5) |
| `src/ossbot/gatekeeper/telegram/{topics,routing,dashboard,notify,digest,drafts,pickers,rawapi}.py` | **New** (§4, §6.1, §7, §8). `rawapi.py`: typed wrappers (pydantic request/response models) for every Bot API 10.1–10.3 method and field the kit uses, over `do_api_request` (ADR-0019 Addendum A) |
| `src/ossbot/gatekeeper/telegram/{proposal_cards,work_links,size_pickers}.py` | **New** (CR-004 presentation: §5.15–§5.19, §8.4) |
| `src/ossbot/gatekeeper/{proposal_approvals,skill_approvals,discovery_prefs}.py` | **New**: `ProposalApprovals` and `SkillApprovals` (§12.4 delta 4); `DiscoveryPrefs` (§8.4). The proposal and work-request data model itself is CR-004's |
| `src/ossbot/gatekeeper/approvals.py` | §12.4 deltas 2, 3 (`disarm_expired`, `CARD_PURPOSES`); nothing else |
| `src/ossbot/gatekeeper/app.py` (`housekeeping_once`) | Calls `disarm_expired()` for the three services right after `expire_due()`, then queues the S1 card edits |
| `src/ossbot/gatekeeper/telegram/auth.py` | Adds `authorized_chat_only()` (U10 exception); `authorized()` unchanged |
| `src/ossbot/gatekeeper/chat/imgmeta.py` | **New** metadata walker (§6.5) |
| `src/ossbot/gatekeeper/telegram/cards.py` | Decorated S1 layout via the kit (FEAT-005 item 22); skill card; stale rendering |
| `src/ossbot/gatekeeper/telegram/keyboards.py` | `style`, `disabled`, URL/copy buttons in `to_json`; the regrouped card rows |
| `src/ossbot/gatekeeper/telegram/escape.py` | `defuse_commands`; the `tg-time` helper |
| `src/ossbot/gatekeeper/telegram/handlers.py` | Delegates classification to `routing.py`; new token purposes; `stopped_message_generation` `TypeHandler`; `_send_reply`/`_edit` rewritten to send through `telegram.ui.send` in the originating thread, except the U3 Approve/Confirm edits (§4.1) |
| `src/ossbot/gatekeeper/telegram/bot.py` | `ALLOWED_UPDATES += ["stopped_message_generation"]`; topic bootstrap; command scopes and menu button; draft lane task; short-description updates on up/down |
| `src/ossbot/gatekeeper/telegram/commands/{builtin,registry}.py` | New commands (§8.1); safety class `personal`; picker integration; `menu()` per control state |
| `src/ossbot/gatekeeper/outbox.py` | Columns `priority`, `thread_key`, `coalesce_key`, `result_message_id`, `bind_tokens_json`, `view_json`, `merged_into`; statuses `held`, `merged`; `mark_sent` binds tokens and writes `tg_views` in its transaction (§12.2); quiet-hours hold; batching; "thread not found" recovery |
| `src/ossbot/gatekeeper/chat/{backend,hermes_api,stream,md2blocks,followups,media}.py` | FEAT-005 item 23 plus streaming, rendering, follow-ups and media (§6) |
| `src/ossbot/gatekeeper/review_html.py` + `templates/review.html.j2` | **New** HTML diff (§5.4) |
| `src/ossbot/gatekeeper/interdiff.py` | **New**: runs `INTERDIFF`/`RANGE_DIFF` at `/package` (§5.7) |
| `src/ossbot/gatekeeper/packaging.py`, `derive.py` | Call `interdiff.py`; retention amendment (§5.7) |
| `src/ossbot/common/gitcmd.py` | `INTERDIFF`, `RANGE_DIFF`, mirror plumbing argv constants |
| `src/ossbot/gatekeeper/review_mirror.py`, `review_mirror_writer.py` | **New**, opt-in (ADR-0019 C); import-linter contract "only review_mirror imports review_mirror_writer" |
| `src/ossbot/gatekeeper/telegraph.py`, `telegraph_writer.py` | **New**, opt-in (§6.8). Import-linter: only `telegraph` imports `telegraph_writer`; egress category `telegraph` (gatekeeper only); `config/egress.yaml` `always_denied` += `telegra.ph`, `api.telegra.ph`, `edit.telegra.ph` for every other profile |
| `src/ossbot/gatekeeper/features/{__init__,registry}.py` | **New** registry (§12.3); `api/features.py` route `POST /v1/features/{feature}/cards` (role hermes) |
| `src/ossbot/gatekeeper/api/notify.py`, `api/features.py` | Optional `priority` (P1/P2 only from Hermes; P0 is never accepted from Hermes); target thread resolved from `chat_turns` only, Assistant/user topics only, 🤖 prefix, P1 ≤ 5/day (U12, §6.7) |
| `hermes/plugins/ossbot/` | Tools `ossbot_feature_card`, `ossbot_reminder_create`; follow-up instruction text |
| `config/telegram.yaml` (+ schema) | **New** tracked config loaded through the snapshot (§14.3) |
| `config/profile.example.yaml` | `telegram.timezone`, `telegram.quiet_hours`, `review_mirror.repo` |
| `scripts/setup-secrets.ps1` | Optional `GITHUB_REVIEW_MIRROR_TOKEN` (and later integration keys), set by key name only |
| `src/ossbot/ctl/` | `ossctl review-mirror enable|disable|status|purge`; `doctor`: topics state, rich fallback flag, mirror probes |
| `pyproject.toml` | Import-linter contracts: features/chat cannot import `approvals`, `submit`, `github_writer`, `review_mirror_writer`, `state_machine`, `control`; only `telegram.ui.send` writes Telegram rows for chat/features |
| `README.md`, `AGENTS.md`, `docs/SETUP.md` | Telegram tour (topics, dashboard, review flow), the §12.3 guide, BotFather topic setting, mirror setup (optional) |

### 14.2 Data model (`migrations/00NN_telegram_ux.sql`, next free number)
```sql
CREATE TABLE tg_topics (name TEXT PRIMARY KEY, thread_id INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE tg_views (
  message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, thread_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('dashboard','card','explorer','file','interdiff','draft_review','status',
                                      'digest','feature','picker','progress','chat_answer',
                                      'body_part','commit_msg','fyi','confirm_prompt',
                                      'proposal','proposal_details','rescope','ceiling','work_request','link_card')),
  approval_id TEXT, cid TEXT, rev INTEGER, path TEXT, page INTEGER,
  state_json TEXT NOT NULL DEFAULT '{}', content_sha256 TEXT, updated_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE review_comments (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL, rev INTEGER NOT NULL, approval_id TEXT,
  source TEXT NOT NULL CHECK (source IN ('telegram','mirror')), path TEXT, side TEXT CHECK (side IN ('old','new')),
  line_start INTEGER, line_end INTEGER, quote TEXT, text TEXT NOT NULL,
  tg_message_id INTEGER, gh_comment_id INTEGER, outdated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, change_request_id TEXT, discarded_at TEXT);
CREATE INDEX review_comments_draft ON review_comments(cid, rev) WHERE change_request_id IS NULL AND discarded_at IS NULL;
ALTER TABLE change_requests ADD COLUMN comments_json TEXT;           -- structured comments (§5.5)
ALTER TABLE tg_outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 2;
ALTER TABLE tg_outbox ADD COLUMN thread_key TEXT;                    -- topic name, resolved at send time
ALTER TABLE tg_outbox ADD COLUMN coalesce_key TEXT;                  -- '<cid>:<event_kind>' (§7.3)
ALTER TABLE tg_outbox ADD COLUMN result_message_id INTEGER;          -- written inside mark_sent's transaction
ALTER TABLE tg_outbox ADD COLUMN bind_tokens_json TEXT;              -- tokens to bind to result_message_id (§12.2)
ALTER TABLE tg_outbox ADD COLUMN view_json TEXT;                     -- tg_views row to insert at send (§12.2)
ALTER TABLE tg_outbox ADD COLUMN merged_into INTEGER;                -- batch row id when status = 'merged'
-- tg_outbox.status has no CHECK (0001_init.sql); new values: 'held' (quiet hours / batch window), 'merged'
CREATE INDEX tg_outbox_coalesce ON tg_outbox(coalesce_key, status);
CREATE TABLE ux_prefs (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE digest_runs (kind TEXT NOT NULL, period_start TEXT NOT NULL, sent_at TEXT, stats_json TEXT NOT NULL,
  PRIMARY KEY (kind, period_start));
CREATE TABLE chat_turns (
  turn_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL, thread_id INTEGER, user_message_id INTEGER NOT NULL,
  run_id TEXT, status TEXT NOT NULL CHECK (status IN ('streaming','done','stopped','failed')),
  started_at TEXT NOT NULL, finished_at TEXT, model TEXT, answer_message_id INTEGER,
  origin TEXT NOT NULL DEFAULT 'typed' CHECK (origin IN ('typed','followup_tap','media','retry')),
  user_text TEXT,                       -- only for the tg-<uid>-reviews session (Use as comment), ≤ 4,000; GC after 24 h
  answer_text TEXT,                     -- the persisted final answer, ≤ 200 KB (§6.8 rebuild, Telegraph)
  tools_json TEXT NOT NULL DEFAULT '[]',-- tool names used in the turn (≤ 64 chars each)
  had_attachments INTEGER NOT NULL DEFAULT 0);
CREATE INDEX chat_turns_session ON chat_turns(session_key, status, finished_at);   -- U12 lookups
CREATE TABLE review_mirror (
  cid TEXT NOT NULL, rev INTEGER NOT NULL, branch TEXT NOT NULL, pr_number INTEGER, mirror_head TEXT NOT NULL,
  tree_equal INTEGER NOT NULL, pushed_at TEXT, closed_at TEXT, last_error TEXT, PRIMARY KEY (cid, rev));
CREATE TABLE reminders (id TEXT PRIMARY KEY, due_at TEXT NOT NULL, text TEXT NOT NULL, created_by TEXT NOT NULL,
  thread_id INTEGER, fired_at TEXT, cancelled_at TEXT);
CREATE TABLE telegraph_pages (path TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, turn_id INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL, published_at TEXT NOT NULL, unpublished_at TEXT);   -- audit event on publish/unpublish
CREATE TABLE discovery_prefs (          -- §8.4; CR-004 semantics; scope '*' = global, else 'owner/repo'
  scope TEXT PRIMARY KEY, autonomous_max_class TEXT CHECK (autonomous_max_class IN ('xs','s','m','l','xl')),
  preferred_classes_json TEXT, filters_json TEXT, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);

-- pending_input (FEAT-005 0002_phase2.sql) is widened with the SQLite table-rebuild procedure
-- (create pending_input_new, copy, drop, rename, recreate indexes) because a CHECK can't be altered:
--   kind IN ('changes','edit_title','edit_body','reject_other','edit_comment',
--            'comment','line_pick','review_send_text','body_file','feature_fix',
--            'proposal_adjust','proposal_snooze_days')
--   new columns: path TEXT, side TEXT CHECK (side IN ('old','new')), hunk INTEGER,
--                line_candidates_json TEXT, rev INTEGER, proposal_id TEXT
```
`ui_tokens.purpose` is free text (the comment in `0001_init.sql` lists the purposes), so no CHECK constraint changes. The new purposes are listed in §12.2.

`pending_input` is planned in FEAT-005 item 2 (`0002_phase2.sql`) with a narrower kind set and no path/hunk columns, and that feature is being built concurrently. This CR does not edit FEAT-005; its own migration (`00NN_telegram_ux.sql`, numbered after whatever FEAT-005 and FEAT-009 ship) performs the rebuild above, so it works whether or not FEAT-005 already widened the table. If FEAT-005 ships the wider definition first, the rebuild is a no-op copy (the migration test covers both starting points).

### 14.3 Configuration
- **`config/telegram.yaml`** (tracked, strict schema, unknown keys rejected, loaded via the config snapshot, changed only by a human-confirmed `/reload`):
  ```yaml
  version: 1
  topics: auto                 # auto | off   (auto = use when getMe.has_topics_enabled)
  rich_views_default: auto     # auto | on | off   (user pref in ux_prefs overrides at runtime, never for approval cards)
  loud_cards_per_hour: 2
  dashboard: {min_edit_interval_s: 30}
  status_card: {min_edit_interval_s: 60}
  progress: {min_edit_interval_s: 5}
  drafts: {min_interval_s: 1.0, heartbeat_s: 20}
  stale_message_minutes: 10
  digest: {daily: "21:30", weekly: "Sun 18:00"}
  effects: {merged: null}      # message_effect_id, [VERIFY]
  attachments: {max_mb: 20, album_window_s: 1.5}
  review_mirror: {enabled: false, poll_pending_s: 60, poll_other_s: 600}
  telegraph: {enabled: false, offer_min_chars: 1500, max_content_kb: 60, author_name: ""}   # §6.8; never automatic
  features: {enabled: []}
  ```
- **`config/profile.yaml`** (personal, gitignored): `telegram.timezone`, `telegram.quiet_hours`, `review_mirror.repo`.
- **Secrets:** `GITHUB_REVIEW_MIRROR_TOKEN` and `TELEGRAPH_ACCESS_TOKEN` (both optional) in `gatekeeper.env`.

### 14.4 python-telegram-bot gaps (raw `do_api_request`)

| Feature | Handling |
|---|---|
| `sendRichMessage`, `sendRichMessageDraft`, `editMessageText(rich_message)` | Raw payloads from `ui/blocks.py` |
| `sendMessageDraft` `can_stop`/`keep_on_stop` | Raw (PTB has the method without these params) |
| `InlineKeyboardButton.disabled`, `InlineKeyboardMarkup.force_reply` | Raw JSON in `keyboards.to_json` |
| `stopped_message_generation` update | `TypeHandler(Update)` reading `update.api_kwargs` (verified in the v22.8 source: unknown keys go to `api_kwargs`, they don't raise); listed in `allowed_updates`; authorized by `authorized_chat_only()` |
| `getMe.allows_users_to_create_topics` (9.4) | Read from the typed field when present, else from `api_kwargs` |

All raw calls go through `telegram/rawapi.py`, which owns one pydantic request model per method (`extra="forbid"`) and one response model per result we read (`extra="ignore"`, so a new Telegram field can never break parsing). Everything else is PTB-native: topics, `style`, reactions, effects, scopes, pins, `copy_text`, link previews. When PTB adds 10.1+ support, the raw calls can switch to typed methods without changing the kit's API.

Why lenient decoding matters here: the gatekeeper is the only poller, and the same `getUpdates` stream carries Approve, Confirm, `/kill` and `/pause`. One unknown field must never stop it. PTB 22.8 keeps unknown keys in `api_kwargs`; aiogram's strict pydantic decoding blocked long polling on the Bot API 10.3 `RichTextButton` (aiogram #1890, fixed by upgrading to 3.31) and still can on a missing required field (aiogram #1840 open, its fix PR #1848 unmerged). The comparison is in ADR-0019 Addendum A.

### 14.5 Error handling (per operation)

| Operation | Failure | Recoverable? | User sees | Logged |
|---|---|---|---|---|
| `sendRichMessage` / `editMessageText(rich)` | 400 (unsupported, block error) | Yes: classic fallback; `rich_unavailable_until = +24 h` | The same view in classic form | WARNING once/day + payload kind |
| Any send | 429 | Yes: `retry_after` (existing) | Delay | INFO |
| Send with `message_thread_id` | 400 "thread not found" | Yes: re-create the topic once and resend; then flat mode | Message arrives (maybe in flat mode) | WARNING |
| `createForumTopic` | 400 (topics disabled) | Yes: flat mode | Flat chat with section icons | WARNING once per start |
| `editMessageText` | "message is not modified" | Ignored (content hash avoids most) | Nothing | DEBUG |
| `editMessageText` | "message to edit not found" | Dashboard/status: send new (and pin); card: stays pending, reachable via `/queue` | New message | INFO |
| `pinChatMessage` | Any error | Yes: unpinned dashboard | Dashboard not pinned | WARNING |
| `setMessageReaction` | Any error | Ignored | No reaction | DEBUG |
| `sendMessageDraft` / rich draft | 400/429/network | Dropped (ephemeral); the final message still sent | Fewer stream updates | DEBUG; WARNING on the first 400 per start |
| `stopped_message_generation` | Unknown `draft_id` or turn finished | Ignored | Nothing | DEBUG |
| Hermes stream | Connect refused / timeout (no event for 120 s) / 5xx | Turn failed; relay marked unavailable for 60 s | "Assistant unavailable" + Retry | WARNING |
| Hermes stream | Event > 1 MB or answer > 200 KB | Truncated | "…(truncated)" note | WARNING |
| Hermes stop call | Error | Yes: close the stream | Partial answer + "Stopped" | WARNING |
| `getFile` / download | > 20 MB, type not allowed, network | Not for that file | Limit message | INFO |
| md2blocks | Parser exception | Yes: classic escaped text | Plain answer | ERROR (bug) |
| Interdiff | Previous mirror missing / git error | Yes: card without interdiff | "Interdiff unavailable (<reason>)" | WARNING |
| HTML diff render | Template or size error | Yes: no HTML document | "HTML view unavailable" toast | ERROR |
| Quote mapping | No or ambiguous match | Yes: file-level comment or line picker | Picker or "saved on file" | DEBUG |
| Body-as-file edit | Too big / not UTF-8 / policy refusal | Yes: user retries | Exact reason | INFO |
| Mirror: token missing | | Mirror off | Doctor "awaiting secret" | WARNING at start |
| Mirror: repo not private / Actions on | | Mirror disabled until re-enabled | P0 (public) / P1 (Actions) alert | ERROR |
| Mirror: probe shows token too broad | | Enable refused | P0 "revoke this token" | ERROR |
| Mirror: push protection / 422 | | Skip this rev | "🐙 Mirror unavailable (push protection)" on the card | WARNING |
| Mirror: 401/403 | | Mirror disabled | P1 alert | ERROR |
| Mirror: network/5xx | Retried 3× (2, 8, 30 s) | Skip the rev after that | Card note | WARNING |
| Mirror: tree-equality check fails | | No push | Card note "mirror skipped (integrity)" | ERROR (bug) |
| Mirror comment poll | Error | Next tick; a note after 30 min failing | "🐙 comments delayed" | WARNING |
| Mirror comment from another login | | Ignored | Nothing | WARNING + counter |
| Digest stats read | Error | Digest sent without stats | "stats unavailable" | WARNING |
| Feature card POST | Validation error | 422 to the plugin | Nothing (Hermes may answer in text) | INFO |
| Feature card POST | > 30/h | 429 to the plugin | Nothing | WARNING |
| Telegraph publish | Scan hit (secret / key / confidentiality / personal value) | No: nothing is sent | "Not published: the answer contains <class>" | WARNING (class only, never the match) |
| Telegraph publish | Token missing / `ACCESS_TOKEN_INVALID` | Feature off until `ossctl telegraph setup` | "Article publishing is not set up" | WARNING |
| Telegraph publish | Network / 5xx / `ok:false` other | Retried 2× (2, 8 s), then given up | "Publish failed (<error>)", with Retry (fresh confirm) | WARNING |
| Telegraph unpublish | Any error | Retry button | "Unpublish failed; the page is still public" | ERROR |
| `setMyCommands` / `setMyShortDescription` | Any error | Retried at the next state change | Stale menu or description | WARNING |
| `disarm_expired` card edit | "message to edit not found" / network | Yes: the approval is already `pending` with fresh tokens; reachable via `/queue → Open` | Card may still show the dead Confirm panel; a tap there gets "token used" | INFO |
| Kit token tap with no current approval for `(cid, rev)` | Approval invalidated or consumed | No (by design) | "Superseded: content or mode changed since this card; review the new card." | DEBUG |
| `mark_sent` token bind | A token in `bind_tokens_json` already bound (should not happen) | Fatal for that row's buttons: the row is marked sent, the stray tokens are killed | Buttons answer "This button expired; reopen the view" | ERROR (bug) |
| Hermes notify / feature card | `session_key` with no active or ≤ 10-min-old turn | No | Nothing | INFO; HTTP 409 to the plugin |
| Follow-up tap | Stored text ≠ rendered label | No | "That suggestion is no longer valid" | ERROR (bug) |
| Image metadata walker | Structural error / unsupported container | No (for that image) | "That image file looks damaged; send it as a photo" | INFO |
| `/work` URL or link card | Not a GitHub issue/PR/repo URL; host not `github.com` | No | Usage hint with the accepted forms | INFO |
| `/work` / link card title fetch | GitHub read error | Yes: queued anyway | `(details unavailable)` in the reply | WARNING |
| `/work` | AI-contribution ban found (CR-004 check) | No | Refusal card with the quoted policy line | INFO + audit |
| Proposal Start work | Proposal superseded, expired or binding mismatch | No | "Superseded: this proposal changed; review the new version." | INFO |
| Ceiling `🔁 Re-check` | Still over the ceiling | Yes: user splits or edits config | Toast with the measured numbers | INFO |
| `/size`, `/filters`, `/repos … size` | Value outside the allowlist (e.g. `xxl`, a ceiling key) | No | Toast with the allowed values | INFO |
| `discovery_prefs` write | DB error | No | "Not saved; try again" | ERROR |

### 14.6 External input validation

| Input | Required | Type / limits | On failure |
|---|---|---|---|
| `callback_data` | yes | `^t:[A-Za-z0-9_-]{22}$` (existing); the token row must match purpose, message, expiry and use | `answerCallbackQuery` with the rejection text; WARNING |
| Picker / nav token `arg` | yes | JSON validated by the purpose's pydantic model (`extra="forbid"`) | Treated as an invalid token |
| Review comment text | yes | Non-empty after strip; ≤ 4,000 characters; UTF-8; `undefuse` applied; ≤ 30 open comments per `(cid, rev)` | Reply with the limit ("Draft review is full (30); send or discard first"); nothing stored |
| Draft review at Send | n/a | Deterministic `change_requests.text` ≤ 60,000 characters | Send refused with the size; nothing changes |
| S6 "You asked" block (render) | n/a | ≤ 5 items; text ≤ 80, claim ≤ 60 characters; optional block (§12.1) | Cut with "…" / "+N more"; dropped first under budget pressure |
| Proposal adjust feedback | yes | Non-empty; ≤ 2,000 characters; `make_visible`; sent to scout as untrusted user text | Reply with the limit |
| Proposal snooze (Custom) | yes | Integer 1–90 (days) | Usage hint |
| Proposal fields from scout (render) | yes | Per the §5.15 table (problem ≤ 300, ≤ 6 bullets × 100, ≤ 4 paths × 60, rationale ≤ 160, confidence 0–1); scout output validated by CR-004's pydantic model first | Field cut with "…" (display only); a schema failure means no card and a scout retry (CR-004) |
| `/work <url> [notes]` | url yes | `^(?:https://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})(?:/(?:issues\|pull)/(?P<n>[1-9][0-9]{0,9}))?/?$` after stripping `?query` and `#fragment` (a missing scheme is allowed, `http://` is not); notes ≤ 1,000 characters, `make_visible` | Usage hint with the accepted forms; nothing queued |
| Link cards (routing rule 6) | n/a | Same URL regex, found anywhere in text, caption or `entities` of type `url`/`text_link`; ≤ 3 carded | Non-matching links are ignored (normal Hermes routing) |
| `/size` and per-repo class | yes | One of `xs s m l xl`, or `inherit` for a repo; repo must be a configured target | Toast with the allowed values |
| `/filters` toggles | yes | Token `arg` names a key and value from the fixed allowlist (kinds, labels, tier, age, activity, languages from profile, max open PRs 1–3) | Treated as an invalid token |
| Hermes `session_key` (notify, feature card, reminder) | yes | Must match a `chat_turns` row with `status='streaming'` or `finished_at` ≥ now − 10 min; resolved thread must be Assistant or a user topic | 409 `no_active_turn` / redirected to 💬 Assistant |
| Hermes P1 request | optional | ≤ 5 per local day | Downgraded to P2 |
| `quote` | optional | ≤ 1,024 characters; used only for mapping | File-level comment |
| Body-as-file document | optional | ≤ 64 KiB; `.md`/`.txt` or a text MIME type; strict UTF-8; normalized; `policy.check_text`; ≤ 60,000 characters | Exact reason; no override stored |
| Title edit | yes | 1–256 characters, single line (existing) | Existing |
| `/quiet` argument | optional | One of the picker values, or `<1-24>h`, or `until HH:MM` | Usage hint |
| `/title` | yes | 1–48 characters after sanitizing; no controls or bidi | Usage hint |
| `/remind` (typed) | yes | `when` parsed as an ISO date-time or `in <n>(m|h|d)`, ≤ 365 d; text ≤ 500 | Usage hint |
| `message_thread_id` | optional | int; a known system topic, or treated as an assistant conversation | n/a |
| `stopped_message_generation` | n/a | From `update.api_kwargs`; pydantic model; `chat.type == "private"` and `chat.id == owner` (`authorized_chat_only`); `draft_id` in `chat_turns` with `status='streaming'` | Ignored (DEBUG); unauthorized → the normal rejection counter and WARNING |
| Image document (assistant) | optional | Magic bytes JPEG/PNG/WebP; ≤ 5 MB; metadata walker passes | Exact reason ("send it as a photo") |
| Hermes SSE | n/a | Each event ≤ 1 MB; the answer ≤ 200 KB; unknown event types ignored; tool names ≤ 64 characters; URLs http(s) only, ≤ 2,048 | Truncate or ignore + WARNING |
| Follow-ups block | optional | ≤ 3 items; label = relayed text, ≤ 64 characters (longer items dropped, never cut); no hidden characters; not starting with `/`; no approval words | Item dropped |
| Feature payload | yes | The feature's pydantic model (`extra="forbid"`; bounded numbers, e.g. protein 0–500 g; strings ≤ 200) | 422 |
| `ossbot_reminder_create` | yes | `due_at` in the future, ≤ 365 d; text ≤ 500; ≤ 50 active | 422 |
| Telegraph article content | n/a (gatekeeper-built) | From the stored `chat_turns` answer only; eligibility rules (§6.8); title 1–256 characters (≤ 80 + suffix used); converted nodes ≤ 60 KB (API limit 64 KB); tag and attribute allowlist; `href` http(s) only | Button not offered, or publish refused with the reason |
| Mirror review/comment JSON | n/a | `user.login == profile.github_login`; body ≤ 65,536; `path` ∈ the rev's numstat; `line` int ≥ 1; `commit_id` == current `H'` else `outdated` | Ignored + WARNING |
| `config/telegram.yaml` | yes | Strict schema; times `HH:MM`; weekly `Mon..Sun HH:MM`; numbers within ranges (`loud_cards_per_hour` 0–20, intervals ≥ the minimums in §11.4) | Snapshot load or reload refused (existing) |
| `profile.yaml telegram.timezone` / `quiet_hours` | optional | IANA zone (`zoneinfo`); `HH:MM-HH:MM` (may wrap midnight) | Start refused with a message (consistent with the profile rules) |

### 14.7 Tests

**Unit** (fast, hermetic):
- `test_kit_limits.py`: Hypothesis property tests. Every View renders ≤ budget in both modes, with balanced tags, every `callback_data` ≤ 64 bytes, ≤ 3 per row, and the label rule **per row** (multi-button rows ≤ 24, single-button rows ≤ 64). Every constant label in the codebase is checked against its row. Under budget pressure the header, mode, title/subject, size and expiry lines are always present (the budget rule never cuts them), with a 5,000-character "You asked" block, 30 comments and maximal proposal fields as inputs.
- `test_kit_tokens_survive.py`, `test_disarm_expired.py`, `test_reissue_keeps_draft.py`, `test_outbox_bind_atomic.py`, `test_card_purposes.py` (§12.4 deltas 1–6).
- `test_stale_reasons.py`: every `invalidate_*(…, "<literal>")` call site has a `REASON_LABELS` row (§5.12).
- `test_followups.py` (extended): label == relayed text; items > 64 dropped; the echo is sent before the relay; `origin='followup_tap'` recorded.
- `test_use_as_comment.py`: converts the user's stored message, never the Hermes answer; `undefuse` applied.
- `test_hermes_routing.py` (U12): unknown/stale session key → 409; Reviews session → redirected to Assistant; never Reviews/Control/Feed; 🤖 prefix; sixth P1 of the day → P2.
- `test_imgmeta.py`: Hypothesis fuzzing and a GPS-tagged corpus (§6.5).
- `test_undefuse.py`: a defused `/kill` quoted back into a comment shows `/kill`, not `⟦U+2060⟧`; a foreign U+2060 elsewhere is still made visible.
- `test_proposal_cards.py`: goldens for v1, v2 with version diff, `👤` variant, CLA and open-PR warnings, forbidden-path variant, expired and superseded stubs; the field bounds of §5.15; Start work only on the card.
- `test_size_line.py`: every row of the §5.1 size table, including code/test split and the coder reason, in `flag` and `reapprove` modes; ceiling hit → no preview.
- `test_size_filters_pickers.py`: `/size` never offers `xxl`; per-repo `inherit`; `/filters` pages ≤ 5 toggle rows + pager; Undo restores the previous value; a ceiling key is refused by `DiscoveryPrefs`.
- `test_work_links.py`: URL regex accept/reject table; link-only message not relayed to Hermes; message with text relayed and carded; > 3 links.
- `test_digest_size.py`: merge-rate table, `l+` merging below 3 PRs, `👤 yours` row, rate excludes open PRs.
- `test_blocks_untrusted.py`: random untrusted strings, including `<tg-button>`, `**`, `/kill`, `tg://` and bidi characters. They come out as plain `RichText` strings with `skip_entity_detection` set and are never in `html`/`markdown` fields.
- `test_defuse_commands.py`
- `test_md2blocks.py`: golden Markdown → blocks, plus malformed input.
- `test_followups.py`: sanitization and the drop rules.
- `test_routing.py`: every rule of §4.4, including stale ages.
- `test_queue_order.py`
- `test_notify_quiet.py`: P0 always loud; wrap-around quiet hours; held P2 → batch.
- `test_quote_mapping.py`
- `test_interdiff.py`: bare repos; same base → tree diff with line-window touch flags; moved base → range-diff with `files[]` from the normalized per-file `FORMAT_PATCH` comparison, file-level touch flags, the "line-level touch check unavailable" note, and a quote-reply on the interdiff view stored as a file-level comment; missing old mirror → unavailable.
- `test_review_html.py`: autoescape, no `<script>`, CSP present, an AST check for `|safe`.
- `test_mirror_tree.py`: sparse `B'`/`H'` in bare repos; the integrity check catches a tampered blob; `.ossbot-review/` collision refused.
- `test_mirror_writer_scope.py`: any URL outside the mirror raises.
- `test_feature_registry.py`: the import-linter contract and schema enforcement.
- `test_telegraph.py`: eligibility, scans, node allowlist, random title suffix, unpublish.

**Telegram** (fake Bot API, `-m telegram`):
- golden cards S1/S6/S8/S12 in LIVE and DRY RUN for every action kind, plus the skill card;
- explorer navigation edits in place;
- file view paging (rich and classic);
- the rich 400 → classic fallback;
- topics bootstrap and "thread not found" recovery;
- dashboard edit throttling and content-hash skip;
- stale-card stubbing of body parts;
- the draft lane (coalescing, 30-s heartbeat, yielding to the outbox);
- the `stopped_message_generation` → stop path;
- the follow-up tap relaying to Hermes and never to the registry;
- the killed-mode reduced menu (`/status /resume /logs /help /start`, `/settings` hidden);
- `/down` (workers) keeps "🟢 Online" and shows `💤 DOWN (workers)`; full down sets the offline short description;
- command replies land in the originating thread;
- file view Prev/Next edits in place and `📂` opens the picker;
- proposal card two-tap Start work, Adjust → v2 reply with diff, Skip/Undo, Never confirm, Later snooze, Details without decision buttons;
- the late mirror row 4 re-uses the card's tokens (no new `ui_tokens` rows);
- comments → one change request with `comments_json`;
- the two-tap Kill button with its 10-s expiry;
- `/approve` rotating tokens (existing N4).

**Integration:** mirror end to end against a fake GitHub API (respx) and a local bare "remote": push, PR, comments → change request; repo-public → disable + P0; token-too-broad probe → refuse; a GitHub Approve is ignored.

**Live** (manual, recorded in VERIFICATION.md on Amsal's phone and desktop): rich explorer and file rendering; quote-reply on `<pre>` and on rich messages; `details` and `diff` highlighting; HTML document opening; topics; drafts with Stop; `tg-time` relative rendering; disabled buttons; the food-photo estimate (CR-002); the merged effect.

### 14.8 Milestones and mapping to PLAN.md
- **M1** (extends FEAT-005 item 22):
  - kit;
  - S1/S2/S3/S4/S5/S7/S8/S9/S12 classic;
  - queue;
  - topics + flat fallback;
  - menu scopes;
  - dashboard;
  - command pickers + `personal` class;
  - notification priorities, quiet hours, batching;
  - offline and killed states;
  - `.patch` and HTML documents;
  - interdiff (S6) + retention amendment;
  - §12.4 token-core deltas (first, before any new view ships);
  - CR-004 presentation: proposal card and versions, size line, rescope and ceiling cards, `/work` and link cards, `/size`, `/filters`, per-repo size (ships with CR-004's pipeline work).
- **M2** (extends FEAT-005 item 23, FEAT-007): chat streaming via drafts and Stop, md2blocks, sources and footer, follow-ups, media UX, `/new` `/title` `/memory`.
- **M3:** rich views for explorer, file, interdiff, tests and assistant (behind `auto`); digest; status card S10/S11; reminders (§13 item 3).
- **M4** (opt-in): review mirror (ADR-0019 C) + `ossctl review-mirror`.
- **v1.x:** Telegraph articles (§6.8; off by default, with `ossctl telegraph setup|rotate`).
- **Phase 2:** feature registry consumers (nutrition log first), §13 v1.x and phase-2 items, Mini App (only if needed).

Docs and AGENTS.md updates ship with M1. DESIGN §14 is updated to point here, DESIGN §14.5 `/approve` semantics are amended to the code's `reissue()` (§12.4 delta 5), and DESIGN §12.5 retention is amended.

## 15. Acceptance criteria
1. Every message the gatekeeper sends passes the §11.4 budgets: Hypothesis tests over all View builders, in classic and rich modes, with random untrusted input. Every `callback_data` is exactly 24 bytes, `t:` plus 22 characters.
2. No untrusted string ever appears in a rich `html` or `markdown` field, and every rich payload has `skip_entity_detection: true`. A test injects `<tg-button type="callback_data" data="x">`, `**`, `/dryrun off` and `tg://` into Hermes output, issue titles and logs, and asserts they render as literal text.
3. In classic mode, `/kill`-style text inside a Hermes answer is not a clickable command: a word joiner follows the slash. Verbatim `<pre>` bytes of PR bodies, comments and commit messages are byte-identical to the stored values.
4. The approval card and Confirm are classic messages. Approve exists only on the card; the explorer, file, interdiff, draft-review and status messages contain no approve or confirm token. A test enumerates the purposes on every kind of view.
5. Approve is minted only after all body parts are delivered (existing AC 14), with the new keyboard layout. Golden S1 cards exist in LIVE and DRY RUN for `open_pr`, `push_update`, `comment`, `issue_claim` and `skill_activate`.
6. The Confirm text equals the DESIGN §12.4 sentence for the current mode. A dry-run toggle between Approve and Confirm leaves the old Confirm token invalid (existing behaviour, re-tested through the new UI).
7. A reply to a file message with a quote that matches one line range stores a `review_comments` row with that `path` and range. An ambiguous quote shows a line picker. A non-matching quote stores a file-level comment.
8. "Send N comments" creates exactly one `change_requests` row whose `comments_json` has N entries. The contribution moves to `CHANGES_REQUESTED` and the old card shows S5 with no approvable button.
9. A revised card (rev N) shows the interdiff from `INTERDIFF` when the base is unchanged, or from `RANGE_DIFF` when it moved. With the same base, "touched" flags use the ±3-line window. With a moved base, `files[]` comes from the normalized per-file comparison of the two `FORMAT_PATCH` outputs, "touched" is file-level, the card says "line-level touch check unavailable", and a quote-reply on the interdiff view is stored as a file-level comment. The card states that the full patch is what gets approved. With the rev N−1 mirror missing, the card still arrives with "Interdiff unavailable".
10. Every invalidation reason edits the old card to S12 within one housekeeping tick (≤ 10 s) and stubs its body parts. A stale token tap answers "Superseded…".
11. `/queue` orders pending approvals by `(class_rank, expires_at, -target_priority, created_at)`. At most `loud_cards_per_hour` cards per hour go out without `disable_notification`.
12. With topics enabled in the fake `getMe`, the four system topics are created once and reused across restarts. Approval cards go to Reviews. A "thread not found" error re-creates the topic once and then falls back to flat mode, and the card is still delivered.
13. In the owner scope, the command menu matches the registry; in the default scope it is empty. In killed mode the owner's menu is `/status /resume /logs /help /start`, and `/settings` is absent.
14. The dashboard is edited at most once per 30 s and never when its content hash is unchanged. Its footer is a relative `tg-time`. A deleted dashboard is re-sent and pinned.
15. Typed `/kill` is immediate. The dashboard Kill needs a second tap within 10 s, and a second tap after 11 s is refused.
16. A go or config command sent more than 10 minutes before processing produces only its confirm prompt, with the age shown. A stop command runs and notes the delay.
17. P0 messages are loud during quiet hours and while `/quiet` is active. P1 becomes silent and P2 is held and later batched into one message.
18. An assistant turn sends a draft within 1 s, then at most one draft per second, a heartbeat at least every 20 s, and a final persisted message. `stopped_message_generation` stops the Hermes run and persists the partial answer with "Stopped by you".
19. A follow-up tap sends exactly its visible label (byte for byte) to Hermes as a new user turn, after the `↪️` echo has been sent, with `chat_turns.origin = 'followup_tap'`, and never invokes the command registry. A suggestion longer than 64 characters, starting with `/` or containing "approve" never becomes a button.
20. An album of 3 photos sent within 1.5 s is one Hermes turn with 3 `input_image` parts and EXIF removed. A 25 MB file gets the limit message.
21. The skill card binds the file hashes, the tools and the tier. A changed staged file invalidates it, and Confirm activates exactly the bound hash.
22. With Hermes down, a user message gets "Assistant unavailable" plus Retry within 2 s, and OSS commands keep working. With soft kill active, non-allowed commands get the killed message.
23. The HTML diff contains no `<script>`, has the CSP meta, and escapes a hostile filename and hunk content (`</pre><script>`).
24. Mirror (with the fakes):
    - `B'..H'` raw diff equals `base..head` outside `.ossbot-review/`;
    - a tampered blob blocks the push;
    - pushes go only to the mirror URL;
    - a public repo or enabled Actions disables the mirror with a P0 or P1 alert;
    - the enable probe refuses a token whose foreign-write probe returns 422;
    - a GitHub Approve review never changes an approval.
25. `config/telegram.yaml` rejects unknown keys and out-of-range values. `review_mirror.enabled: true` takes effect only after a confirmed `/reload` and a passing `ossctl review-mirror enable`.
26. The import-linter contracts hold: features and chat do not import the approval, submit, writer or control modules; only `review_mirror` imports `review_mirror_writer`; only `telegraph` imports `telegraph_writer`.
27. Telegraph (with a fake API):
    - with `telegraph.enabled: false` no `📖` button exists;
    - when enabled, the button appears only on eligible `chat_answer` views. It never appears on cards, explorer, file, digest, feature, Reviews-topic or attachment turns, or on turns that used memory, session_search or ossbot tools;
    - publishing needs the publish tap and the confirm tap;
    - an answer containing a secret pattern, the Kiro key, a confidentiality-floor string or the profile email is refused with nothing sent;
    - the published nodes use only the allowlisted tags, and the title ends with the random suffix;
    - Unpublish calls `editPage` with the removal text.
28. Egress: `telegra.ph`, `api.telegra.ph` and `edit.telegra.ph` get 403 from every profile except the gatekeeper's `telegraph` category, in every egress mode, including `open`. With `telegraph.enabled: false` the gatekeeper's `telegraph` category is absent from the rendered squid config too.
29. A Confirm panel left untouched for 5 min 10 s is back to S1 (card edited, `approval_disarmed {reason:"confirm_expired"}` audited) and its fresh Approve token arms it again. The same holds for the proposal and skill Confirm panels.
30. After arm → Cancel, after `⬇️ Show card` (`reissue()`), and after `/approve <cid>`, the explorer, the file view, a pending comment prompt and the draft review's `📨 Send` all still work, and the draft review keeps its comments.
31. A crash injected between `mark_sent` and `on_sent` leaves every kit token of that message bound and its `tg_views` row present after restart.
32. The S6 "You asked" block shows at most 5 items of ≤ 80 characters plus "+N more". With 30 long comments the card is ≤ 3,900 characters and still contains the header, mode, PR title and expiry lines. The 31st comment is refused; a draft whose `change_requests.text` would exceed 60,000 characters can't be sent.
33. `/down` (workers) leaves the short description "🟢 Online", shows `💤 DOWN (workers)` on the dashboard, and the assistant answers "Assistant is stopped (workers down) — /up". Only a full down sets "💤 Offline since…".
34. The skill card and its Confirm carry the mode badge with "n/a: local change", the Confirm sentence says "dry-run does not apply", the button is styled `danger`, and activation behaves the same in DRY RUN and LIVE.
35. A Hermes feature card or notification with an unknown or stale session key is refused (409). One for the Reviews session lands in 💬 Assistant. Nothing Hermes-originated ever lands in Reviews, Control or OSS feed; each carries 🤖 in its header line; the sixth Hermes P1 of a day is sent silent.
36. An image document with EXIF GPS (JPEG), `eXIf`/`tEXt` (PNG) or `EXIF`/`XMP ` (WebP) reaches Hermes with those removed and the pixel data byte-identical; a HEIC document gets "send it as a photo"; the fuzz test never raises anything but `ImageRejected`.
37. A defused command quoted back into a comment or echoed as a follow-up shows `/kill`, never `⟦U+2060⟧`.
38. **Proposal card (CR-004):** it shows problem, approach (≤ 6 bullets), likely files, `est <class> · ~code · ~test · files`, validation plan, signals, rationale, confidence, risk and alternative counts, the mode badge "n/a: local change" and the expiry, within 3,900 characters, with the keyboard of §5.15 (Start work only on the card). Start work needs two taps and binds version + proposal hash; a tap on the v1 Start work after v2 exists answers "Superseded…". Adjust plan and Make it smaller produce v2 as a reply with the deterministic "Changed since v1" block.
39. **Size on the preview card:** the size line splits code and test lines; with `on_overrun: flag` an overrun shows `⚠️` with the percentage and the coder's labelled reason and still offers Approve; with `reapprove` a larger class produces a `RESCOPE_REQUIRED` card and no preview; a diff above `policy.diff` produces the ceiling-hit card in both modes and for `👤` work, and no button raises the ceiling.
40. **User-directed work:** `/work <url>` and `🛠 Work on this` on a link card queue a scout and reply "Working on it · 👤 requested by you"; the proposal and preview cards carry `👤 requested by you`; the size preference and filters are not applied; an AI-ban repo gets the refusal card; a link-only message is not relayed to Hermes.
41. **`/size` and `/filters`:** `/size` offers `xs`…`xl` (never `xxl`) and per-repo overrides including `inherit`; a change is immediate, audited, FYI'd and undoable for 60 s; an already-approved proposal keeps its class; the ceiling is shown read-only. `/filters` toggles apply to autonomous discovery only; every page has ≤ 5 toggle rows plus the pager row; every label fits §11.2.
42. **Digest:** the weekly digest shows the merge-rate-by-size table with `l+` merging, the `👤 yours` row and rates only from ≥ 3 PRs.
43. **Approvals topic order:** `/queue` lists gate 2 items before autonomous proposals; a `👤` proposal sorts in class 1 and arrives loud; autonomous proposals arrive silent; an autonomous proposal within 6 h of expiry is promoted to class 2.

## 16. Out of scope
- Building the Mini App (ADR-0019 B), Telegram Serverless, webhooks and any inbound component.
- Inline mode, guest mode, business features, payments and Stars.
- Ephemeral messages (§8.3).
- Approving anything outside the Telegram card: GitHub reviews, Mini App, reactions, reply keyboards and voice are all excluded.
- A persistent reply keyboard. The menu button and dashboard cover it. It could be added later with fixed label → read-only command aliases.
- Multi-user support, groups and channels.
- Implementing a specific reverse-image-search provider (needs a user decision, CR-002), calendar, and the other phase-2 items in §13.
- Changing the approval/token core, the binding, or the Confirm sentences of the four outbound kinds (S1, S2). **Explicit exceptions:** the six token-core deltas of §12.4 (non-approval tokens with `approval_id NULL`; `disarm_expired`; the new `CARD_PURPOSES`; the separate `SkillApprovals`/`ProposalApprovals` services; `reissue()` as the single `/approve` semantics; atomic bind in `mark_sent`). Each is listed with its test and AC.
- Changing the hard size ceiling from Telegram in any way (U13), and CR-004's pipeline, scout, ranking and data model (CR-004 owns them; this CR owns only their presentation and `discovery_prefs`).
- Automatic Telegraph publishing, and Telegraph for any content type other than eligible assistant answers (§6.8).

## 17. Items to verify at implementation
Check by web search or live probe; record results in VERIFICATION.md or the ADR-0019 addendum.
1. Rich-message rendering on Amsal's Telegram clients (Android/iOS/desktop versions): `details`, `table`, `pre language=diff`, anchors. Quote-reply on rich messages. If quote-replies don't work there, file views default to classic when the user prefers line comments (`ux_prefs.file_view: classic`).
2. Whether `sendMessageDraft`/`sendRichMessageDraft` count against the per-chat send rate. How drafts behave in topics.
3. End-to-end check of `stopped_message_generation` with the real API (the PTB side is resolved from the v22.8 source: unknown keys land in `api_kwargs`). Confirm Telegram accepts the name in `allowed_updates` and that the update arrives without `from`.
4. Resolved: the BotFather topic setting is exposed as `getMe().allows_users_to_create_topics` (Bot API 9.4); doctor warns when false. Still open: whether a "General" topic exists in private-chat topic mode, and whether pins are per topic in topic mode.
5. Message effect IDs (🎉) for `message_effect_id`.
6. How Telegram opens `.html` documents on each client.
7. Hermes v0.22.2: SSE event names on `/v1/responses`; whether closing the stream cancels the turn; `/v1/runs/{id}/stop`; `DELETE /api/sessions/{id}`; `runtime.model` on responses.
8. Kiro V3 ACP image and audio input (CR-002), for the vision role and voice.
9. Mirror:
   - the minimum fine-grained permission for `GET /repos/{r}/actions/permissions`;
   - the foreign-write probe status codes;
   - whether code-span URLs create cross-references;
   - the push protection error shape.
10. Whether the GitHub Notifications API accepts fine-grained tokens (§13 item 7).
11. Telegraph:
    - the full host list used by the API and the web editor (`api.telegra.ph`, `edit.telegra.ph`, `telegra.ph`, and any upload host);
    - that Instant View opens for `telegra.ph` links in the user's clients;
    - current anonymous-upload behaviour;
    - whether Telegram now offers any deletion path. The documented API (2016, still current) has none.
12. That U+2060 WORD JOINER after `/` stops each client (Android, iOS, Desktop, Web) from detecting a bot command in a classic message (U8). If a client still links it, the display marker becomes U+2215 DIVISION SLASH in place of the `/` (display only, never in published bytes), and `undefuse` maps it back.
13. Whether Telegram strips EXIF from photos sent as compressed photos (expected yes); the walker runs either way, so this only affects how often it finds anything.
14. Label truncation per client for a 24-character label in a 2-button row and a 64-character label in a single-button row (Android narrow screens).

## 18. Mockups (`docs/ux/`)
SVG phone screens (390 × 844 frame) with a constraints panel on each. Labels, rows, lengths and token sizes follow §11.2 and §11.4; revision 2 updated 00–04 and 06–09 for the review findings and added 10 and 11 for CR-004.

| File | Screen |
|---|---|
| `00-information-architecture.svg` | Topic list (Control, Reviews, OSS feed, Assistant, user chat) and the owner command menu |
| `01-pr-preview-card.svg` | S1 preview card + verbatim body part + Edit-text picker note |
| `02-diff-explorer.svg` | S2 explorer and S3 file view with a quote-reply comment and the S4 draft review |
| `03-approve-confirm.svg` | S8 Confirm panel in DRY RUN and LIVE side by side |
| `04-interdiff.svg` | S5 stub of r2 and S6 revised r3 with "You asked", interdiff and touched flags; S12 stale keyboard |
| `05-submitted-status.svg` | S9/S10 live status card, a drafted reply card, and the S11 merged summary |
| `06-pinned-dashboard.svg` | Pinned dashboard in Control with the two-tap Kill |
| `07-assistant-chat.svg` | Streaming draft with tool status and Stop; final answer with sources, footer and follow-ups; nutrition feature card |
| `08-skill-approval.svg` | Skill approval card + verbatim SKILL.md part + Confirm |
| `09-offline-error.svg` | Laptop asleep → back online catch-up; soft-killed mode; Hermes unavailable; the two named down states |
| `10-proposal-card.svg` | Gate 1 proposal card v2 (`👤` variant) with the version diff and keyboard; the Start work Confirm; the overrun highlight on a preview card and the ceiling-hit card |
| `11-size-filters.svg` | `/size` picker with per-repo overrides and the read-only ceiling; `/filters` pages 1–2; a pasted GitHub link → link card → `Working on it · 👤 requested by you`, and the AI-ban refusal card |

## 19. References (accessed 2026-10-18)
- Telegram Bot API reference: https://core.telegram.org/bots/api (rich messages and formatting options; `sendMessageDraft`; `EphemeralMessageParameters`; `InlineKeyboardButton.style`/`disabled`; `InlineKeyboardMarkup.force_reply`; `TextQuote`; `LinkPreviewOptions`; `setMessageReaction`; `createForumTopic`; `setMyCommands`; `setChatMenuButton`; `pinChatMessage`; `getFile` 20 MB; `getUpdates` and webhook exclusivity and 24-h retention)
- Bot API changelog (9.3–10.3): https://core.telegram.org/bots/api-changelog
- Bot features (streaming replies, topics in private chats, ephemeral messages, command scopes, menu button, AI agents): https://core.telegram.org/bots/features
- Bots FAQ (rate limits, file sizes): https://core.telegram.org/bots/faq
- Telegram Mini Apps (init data, Ed25519 third-party validation and public keys, `DeviceStorage`, `SecureStorage`, `SecondaryButton`, fullscreen, `themeParams`, `Serverless.call`): https://core.telegram.org/bots/webapps
- Telegram Serverless (handlers, endpoints, DB, Mini App hosting, platform-managed webhook): https://core.telegram.org/bots/serverless
- Telegram FAQ (cloud chats use server-client encryption; secret chats are end-to-end): https://telegram.org/faq
- python-telegram-bot 22.8 changelog (full Bot API 10.0 support): https://docs.python-telegram-bot.org/en/v22.8/changelog.html ; releases on PyPI: https://pypi.org/project/python-telegram-bot/
- Hermes Agent API server (streaming events, tool progress, `input_image`, runs, stop, capabilities): https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
- Prior art: https://github.com/SanNw/opencode-telegram (README; `docs/en/security.md`) and https://github.com/RichardAtCT/claude-code-telegram
- GitHub fine-grained personal access tokens (resource owner, "only select repositories", only classic tokens write to public repos the user doesn't own, read access to public repos): https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- GitHub Mobile (review pull requests, edit files): https://docs.github.com/en/get-started/using-github/github-mobile
- Cloudflare Quick Tunnels (temporary, testing only, no SSE): https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
- Tailscale Funnel (`ts.net` names, ports 443/8443/10000, relays don't decrypt): https://tailscale.com/docs/features/tailscale-funnel
- Telegraph API (methods, no delete, `access_token`, 64 KB content, tag and attribute allowlist, Instant View): https://telegra.ph/api
- Telegram Mini Apps `sendData` ("only available for Mini Apps launched via a Keyboard button", ≤ 4,096 bytes, closes the app): https://core.telegram.org/bots/webapps
- Bot API `MessageGenerationStopped` (no `from`), `TextQuote.is_manual`, `ReactionTypeEmoji` list, `User.allows_users_to_create_topics`, `sendRichMessage` parameters: https://core.telegram.org/bots/api

Telegram library evaluation (revision 2; accessed 2026-10-18; details in ADR-0019 Addendum A):
- python-telegram-bot on PyPI (latest 22.8, 2026-06-12, LGPL-3.0-only): https://pypi.org/project/python-telegram-bot/ ; Bot API 10.1 tracking issue #5261 (open; native work paused 2026-06-18 "until we complete major refactors"; related PRs #5263, #5275, #5356, #5359): https://github.com/python-telegram-bot/python-telegram-bot/issues/5261 ; unknown-key handling in v22.8 `TelegramObject._de_json`: https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/_telegramobject.py
- aiogram on PyPI (latest 3.31.0, 2026-08-26, MIT; Bot API 10.1 in 3.29.0, 10.2 in 3.30.0, 10.3 in 3.31.0): https://pypi.org/project/aiogram/ and https://github.com/aiogram/aiogram/releases ; issue #1890 (10.3 `RichTextButton` → `ClientDecodeError`, polling blocked; closed "Update to v3.31"): https://github.com/aiogram/aiogram/issues/1890 ; issue #1840 (missing required field blocks polling; open) and PR #1848 (tolerant `getUpdates`; open): https://github.com/aiogram/aiogram/issues/1840 , https://github.com/aiogram/aiogram/pull/1848
- pyTelegramBotAPI on PyPI (latest 4.37.1, 2026-10-17, GPLv2; 4.35.0 = Bot API 10.1, 4.36.0 = 10.2, 4.37.0 = 10.3): https://pypi.org/project/pyTelegramBotAPI/ and https://github.com/eternnoir/pyTelegramBotAPI/releases
- Bot API changelog (10.3 = 2026-08-24, the latest): https://core.telegram.org/bots/api-changelog
- Pillow on PyPI (13.0.0, 2026-10-16; HEIC needs a plugin): https://pypi.org/project/Pillow/

## 20. Responses to design review round 1 (`docs/ux/cr003-review.md`) and to CR-004
Every finding is **addressed** in this revision; none is backlogged or ignored. One deviation in *form*: finding 13 asks for a new file `docs/adr/0020-telegram-library.md`. This revision step may only edit CR-003, ADR-0019 and `docs/ux/`, so the full ADR-0020 content is written as **ADR-0019 Addendum A** (same sections, sources and access dates). Filing it under its own number is a pure move with no content change, done by the next docs commit that may create files in `docs/adr/`.

| # | Sev. | Response | Where |
|---|---|---|---|
| 1 | HIGH | Addressed. New §12.4 "Token-core deltas" (six deltas, each with its test) listed as the only §16 exceptions. Non-approval kit tokens: `approval_id NULL` + `{cid, rev}`. `disarm_expired()` for all three services (AC 29). New `CARD_PURPOSES`. Separate `SkillApprovals`/`ProposalApprovals` with their own `confirm_text`. Draft comments keyed by `(cid, rev)`. One `/approve` semantics: the code's `reissue()`, DESIGN §14.5 amended | §5.2, §5.5, §5.9, §5.12, §9, §12.2, §12.4, §16, AC 29–30 |
| 2 | MED | Addressed. `tg_outbox.result_message_id/bind_tokens_json/view_json/merged_into`, statuses `held`/`merged`; `mark_sent` binds tokens and writes `tg_views` in one transaction; new `tg_views` kinds; `chat_turns.answer_text/tools_json/had_attachments/origin/user_text`; `pending_input` widened by this CR's own migration (FEAT-005 is built concurrently and is not edited here) | §7.3, §12.2, §14.2, AC 31 |
| 3 | MED | Addressed. Base-moved mode: `files[]` from the normalized per-file `FORMAT_PATCH` comparison, file-level "touched" with a stated note, `pre` view with file-level comments only; test case and AC 9 | §5.7, §14.7, AC 9 |
| 4 | MED | Addressed. "You asked" ≤ 5 × 80 + "+N more"; kit budget rule drops optional blocks first and never cuts header/mode/title/size/expiry (replaces the `cards.render` tail cut); ≤ 30 comments; `change_requests.text` ≤ 60,000 | §5.5, §5.7, §12.1, §14.6, AC 32 |
| 5 | MED | Addressed. Label = relayed text byte for byte, ≤ 64, one per row, longer items dropped; echo before relay; `origin='followup_tap'`; Use as comment converts only the user's own message | §4.4 r7, §6.4, AC 19 |
| 6 | MED | Addressed. One per-row rule (multi ≤ 24, single ≤ 64) asserted per row; `[📝 Use as comment]`, `[🛑 Kill now · 10 s]`; mockup 06 updated | §8.2, §11.2, §11.4, §14.7, mockup 06 |
| 7 | MED | Addressed. Two named states, `💤 DOWN (workers)` (Telegram `/down`, stays Online) and `💤 DOWN (graceful)` (full down, offline description) | D15, §4.3, §8.1, §10, AC 33, mockups 06, 09 |
| 8 | MED | Addressed. Activation is local and identical in both modes; badge "n/a: local change" on card and Confirm; Confirm says dry-run does not apply; `danger`; S3 narrowed to public-action cards | S3, §8.2, §9, AC 34, mockup 08 |
| 9 | MED | Addressed in ADR-0019: a combinations table (A+C, A+B3, A+C+B3) across all rows with marginal exposure and effort, and "Recommended: A+C" with the reason A+C+B3 is rejected | ADR-0019 Options |
| 10 | MED | Addressed. The `sendData` constraint is documented; B3 is view-only, with comments (if ever) through a temporary one-button reply keyboard; evaluation cell and Decision 3 corrected | ADR-0019, §1, §13 item 16 |
| 11 | MED | Addressed. New U12: target from the gatekeeper's own `chat_turns` (streaming or ≤ 10 min), Assistant/user topics only, 🤖 in every header line, Hermes P1 ≤ 5/day, reminders keep the creating thread | U12, D21, §6.7, §14.6, AC 35 |
| 12 | MED | Addressed via 13: no Pillow; a pure-Python JPEG/PNG/WebP metadata walker that never decodes pixels, magic-byte type check, HEIC rejected, Hypothesis fuzzing | §6.5, AC 36 |
| 13 | MED | Addressed (as ADR-0019 Addendum A, see above). Web-verified facts with URLs and access date for PTB, aiogram and pyTelegramBotAPI against every listed criterion; decision: keep PTB 22.8 + typed `rawapi.py` adapter; Pillow rejected for the gatekeeper | §1, §14.1, §14.4, §19, ADR-0019 Addendum A |
| 14 | NIT | Addressed. `authorized_chat_only()` for `stopped_message_generation`; U10 notes the single exception | U10, §6.1, §14.6 |
| 15 | NIT | Addressed. §1 table uses 👀 → 👌 / 🤷 | §1 |
| 16 | NIT | Addressed. The preview rule applies to classic answers only | §1, §6.3, mockup 07 |
| 17 | NIT | Addressed. Only `is_manual` quotes anchor lines; mixed spans anchor to the new side with both ranges recorded | §5.5 |
| 18 | NIT | Addressed. Prompt replies (rule 3) precede commands (rule 4); rule 5 covers `pending|armed`; body-part and commit-message replies are general comments | §4.4, mockup 00 |
| 19 | NIT | Addressed. Killed menu `/status /resume /logs /help /start`; `/settings` hidden while killed | D2, §4.2, AC 13, mockups 00, 06 |
| 20 | NIT | Addressed. One file view per `(cid, rev)`; Prev/Next edit in place and update `tg_views`; `📂` opens a file picker; Open reuses the message | §5.3, mockup 02 |
| 21 | NIT | Addressed. A table maps the real reason codes (found in `approvals.py`, `control.py`, `config_snapshot.py`, `packaging.py`, `state_machine.py`, `handlers.py`) to labels, plus a test over every call site; S12 Retry = fresh card via `reissue()` (`stale_retry`, not `retry`) | §5.12, mockup 04 |
| 22 | NIT | Addressed. `H'` is committed with parent `B'` (`commit-tree -p B'`); the late row 4 uses `editMessageReplyMarkup` with the same tokens | §5.14, ADR-0019 Option C |
| 23 | NIT | Addressed. `getMe().allows_users_to_create_topics`, doctor warning | §1, §4.1, §17 item 4 |
| 24 | NIT | Addressed. Replies go to the originating thread through `telegram.ui.send`, except the U3 Approve/Confirm edits | §4.1, §14.1 |
| 25 | NIT | Addressed. The `telegraph` category is rendered only while `telegraph.enabled`; always-denied entries stay unconditional | §6.8, AC 28 |
| 26 | NIT | Addressed. Toggles mute or batch, never drop; cards are always delivered | §7.2 |
| 27 | NIT | Addressed. `undefuse()` strips the marker after `/` before `make_visible`; §17 item 12 verifies WJ on every client | U8, §5.5, §17, AC 37 |
| 28 | NIT | Addressed. Mockup 02 L41; mockup 04 `[🧭 Explore] [🔀 Interdiff]`; mockup 06 matches §4.3 | mockups 02, 04, 06 |

**CR-004 presentation (user requirement relayed by the orchestrator, including Amendment B):**

| Requirement | Response | Where |
|---|---|---|
| Proposal card: problem, approach, likely files, size class and lines, validation plan, signals and rationale, confidence, risks; Start work / Adjust plan / Make it smaller / Skip / Never / Later; version diffs | Addressed: classic bound card, field bounds, two-tap Start work through `ProposalApprovals`, per-button behaviour, Details view, deterministic "Changed since vN" block | D18, §5.15, §5.16, AC 38, mockup 10 |
| Estimate vs actual on the preview card; `RESCOPE_REQUIRED` card | Addressed: the size line with the code/test split, the overrun highlight with the coder's labelled reason (default `flag`, not a block), files outside the plan; `RESCOPE_REQUIRED` only in `reapprove` mode; ceiling-hit card in every mode | §5.1, §5.17, §5.18, AC 39, mockups 01, 10 |
| `/size` and `/filters` pickers with per-repo overrides | Addressed: the `tune` class, the `/size` autonomous preference `xs`…`xl`, the read-only ceiling, per-repo overrides incl. `inherit`, `/filters` pages, Undo, `DiscoveryPrefs` | D19, U13, §8.1, §8.4, AC 41, mockup 11 |
| Digest merge rate by size class | Addressed: a table with `l+` merging and a `👤 yours` row | §7.4, AC 42 |
| Approvals topic order: proposals vs PR previews | Addressed: gate 2 previews (class 3) before autonomous proposals (class 4); `👤` items in class 1; promotion near expiry; autonomous proposals silent | §5.13, AC 43 |
| Amendment B: `/work`, the forwarded-link flow, the `👤 requested by you` badge, safety pre-checks | Addressed: the `request` class, the URL validation, link cards (routing rule 6), the badge on every card, the pre-check table | D22, §4.4 r6, §5.19, §14.6, AC 40, mockup 10 |

