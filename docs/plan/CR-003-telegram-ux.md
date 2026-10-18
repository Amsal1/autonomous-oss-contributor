# CR-003: Telegram user experience and the PR review flow

Status: Proposed, revision 6 (user request, 2026-10-18; revised after design review round 1, to present CR-004, after design review round 2, after design review round 3, after design review round 4, and after design review round 5, `docs/ux/cr003-review.md`). Applies on top of DESIGN.md, CR-001, CR-002 including Amendment A, and CR-004 (proposal gate and change-size budget). Where this CR conflicts with DESIGN §14 (Telegram design) or ADR-0004, this CR wins and both must be updated to match. It also amends CR-004's state machine with two human-only Undo edges (§5.15). The review-surface decision is **ADR-0019**; the Telegram client-library and Pillow decision is **ADR-0020** (formerly ADR-0019 Addendum A). Mockups, one per key screen, are in `docs/ux/` (index in §18). Responses to every review finding are in §20 (round 1), §21 (round 2), §22 (round 3), §23 (round 4) and §24 (round 5). Implementation-level details carried from the reviews are build acceptance criteria in §15.1.

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

The deterministic approval path keeps its rules: the same token format, binding and Confirm sentences. The nine bounded code deltas the UI needs in the token and approval layer are listed in §12.4 and are the only exceptions to §16:
1. non-approval tokens without an approval id;
2. a confirm-expiry disarm;
3. per-kind card purposes, with multi-use view buttons;
4. separate skill and proposal decision services;
5. `reissue()` as the one `/approve` semantics;
6. atomic token binding at send;
7. re-sends that keep the approval's expiry, carry each action's own part count and touch only eligible actions;
8. crash recovery for card delivery;
9. invalidation fan-out to every decision service. The default needs zero extra setup. Richer layers, the private GitHub review mirror (ADR-0019 option C) and phase-2 items, are opt-in.

## 1. What the research changed
Model knowledge was stale. Everything below was checked against the live documentation on 2026-10-18 (sources in §19). "PTB" means python-telegram-bot 22.8, the pinned version and the latest release (PyPI, 2026-06-12), which fully supports Bot API 10.0. The current Bot API is 10.3 (2026-08-24). PTB's native 10.1 work (issue #5261) is paused until its v23 refactor, so 10.1–10.3 methods and fields go through `Bot.do_api_request`, which the outbox already uses for every call (`telegram/bot.py: TelegramSender`), with typed pydantic payload models in `telegram/ui/blocks.py` and `telegram/rawapi.py`. The library was re-evaluated now that new dependencies are allowed: PTB stays, aiogram 3.31 and pyTelegramBotAPI 4.37 were rejected (ADR-0020).

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
- **D4 Review surface (ADR-0019).** Native Telegram by default: classic card plus rich read-only views with classic fallback. The private GitHub mirror is opt-in. The Mini App is phase-2. **Mini App: not in v1, requires no-exposure hosting or explicit user approval** (user decision, 2026-10-18): v1 has no internet-reachable component of any kind (no Cloudflare Tunnel, Tailscale Funnel, ngrok, public webhook or inbound port), enforced by a TRACEABILITY test (see `docs/ux/cr003-carried-findings.md`). Serverless is rejected.
- **D5 Approval surfaces.** The approval card and Confirm stay classic HTML messages whose reply-markup tokens are bound to the message ID. They are never rich, never drafts, never ephemeral, and never use in-body buttons (U3).
- **D6 Comments.** Review comments are reply/quote messages and hunk-picker prompts. They collect into a draft review and are sent as one change request.
- **D7 Interdiff.** Every revised candidate shows a gatekeeper-computed interdiff first. It is display-only.
- **D8 Assistant streaming.** `sendRichMessageDraft` (or `sendMessageDraft` when rich is unavailable) with `can_stop`. The final message is sent as a rich message built from Hermes Markdown by our own converter, never through Telegram's Markdown or HTML modes (U1). Every draft and the final message are redacted first, and a draft never shows a secret's prefix (U14).
- **D9 Follow-up buttons.** Allowed only as gatekeeper tokens whose single effect is to send the button's text to Hermes as the user's next message. The relayed text is **byte-for-byte the visible label** (≤ 64 characters), so nothing the user did not see is sent with his authority. This amends ADR-0004 ("never with buttons") (U2).
- **D10 Feature cards.** Hermes supplies typed data; the gatekeeper validates it and renders the card. Hermes never supplies markup or buttons.
- **D11 Notifications.** Four priorities (P0–P3), quiet hours, `/quiet` and `/notify`, coalescing and batching, plus a daily and weekly digest. P0 is never silenced (U7).
- **D12 Arguments.** Every command argument has an inline-keyboard picker. Typed arguments still work and are validated the same way.
- **D13 Confirmations.** Destructive and "go" actions use in-place confirm panels and toasts. Ephemeral messages are not used (§8.3).
- **D14 Kill.** Typed `/kill` stays immediate: it is handled within 2 s while a Hermes answer is streaming, because no update handler holds the update slot for long work (§12.5); during a Confirm live preflight it waits ≤ 8 s (§12.5 item 5), the one bounded inline exception. The dashboard 🛑 button needs a second tap within 10 s.
- **D15 Offline signal.** The pinned dashboard's relative `<tg-time>` keeps ageing on the client while the laptop is off. There are two named "down" states (CR-001), and only one of them is offline:
  - **`💤 DOWN (workers)`**: Telegram `/down`. Workers, Hermes and sandboxes stop; the gatekeeper keeps polling and answering. The short description stays "🟢 Online".
  - **`💤 DOWN (graceful)`**: full down (`down.ps1` / `ossctl down`), the gatekeeper itself stops. Only this state sets the "💤 Offline since…" short description.

  On reconnect, pending updates are processed with their age shown.
- **D16 Component kit.** All rendering goes through `ossbot.gatekeeper.telegram.ui`: card, list, pager, confirm, picker, progress, toast, doc, rich builder. Features register through an entry-point registry.
- **D17 Telegraph articles** (user suggestion, evaluated in §6.8). Off by default (`telegraph.enabled: false`). When enabled, `📖 Open as article` is offered **per message** only on eligible assistant answers, and publishing needs a tap plus a confirm tap. The gatekeeper publishes through a new egress category, after secret and personal-data scans. Dry-run does not govern Telegraph (it is not a GitHub write), so the confirm panel says so in a mode badge and states that it publishes even in dry-run mode; publishing and unpublishing are refused while paused or killed (S3, §6.8). Telegraph is never used for review content, digests, images, health or nutrition data, or anything else. There is **no automatic publishing**: a length threshold only decides whether the button is offered. This overrides the "automatic above a threshold" option, because a public, undeletable publication must be a deliberate act, and rich messages already show answers up to 32,768 characters in the chat.
- **D18 Proposal gate (CR-004 incl. Amendment B).** Gate 1 is a classic, message-bound **proposal card** in ✅ Reviews, with the same two-tap, single-use token mechanics as the PR card. `✅ Start work` is the only button that advances the pipeline; it binds the proposal hash, its version and the estimated class. Adjustments produce a new proposal version, sent as a reply to the old card with a deterministic version diff. The PR preview card (gate 2) shows the estimate against the gatekeeper-measured diff, with code and test lines split (`+142 / −30 code · +210 test`). An overrun is **highlighted on the preview card, not blocked** (`size.on_overrun: flag`, the default). A `RESCOPE_REQUIRED` card exists only in the opt-in `reapprove` mode, and a ceiling-hit card only when the hard `policy.diff` ceiling is reached (§5.15–§5.18).
- **D19 Size preference and filter tuning (CR-004 Amendment B).** Size classes are soft estimates and ranking preferences for **autonomous** discovery only. `/size` sets `size.autonomous_max_class` (`xs`…`xl`), `/filters` sets the discovery filters, and `/repos <repo>` sets per-repo overrides; all three affect autonomous discovery only. The hard ceiling (`policy.diff`: 80 files, 3,000 code lines, 3,000 test lines) is shown read-only and is changed only in config (§8.4).
- **D20 Telegram library (ADR-0020).** python-telegram-bot 22.8 stays the transport and dispatcher, with its default sequential update processing. PTB 22.8 doesn't support Bot API 10.1+, so 10.1–10.3 calls go through a thin typed adapter over `do_api_request`. aiogram and pyTelegramBotAPI were evaluated and rejected: aiogram's strict decoding has blocked long polling, and pyTelegramBotAPI is GPL-2.0; both would need a full migration. Pillow is **not** added to the gatekeeper: metadata stripping is a pure-Python segment walker that never decodes pixels (§6.5). Pillow is allowed only in `--network none` sandbox images.
- **D21 Unsolicited Hermes messages** (feature cards, notifications, reminders) go only to 💬 Assistant or user chat topics, always marked 🤖 in the header line, and their target thread is resolved from the gatekeeper's own `chat_turns` row, never from Hermes input (U12). **Answers to the user's own message** follow routing rules 7–8 (§4.4): in ✅ Reviews they carry the gatekeeper header `🤖 Assistant · answer to your message` with the body in a quote, and text typed in 🚦 Control or 📈 OSS feed is answered in 💬 Assistant. **In flat mode** (no topics, the zero-setup default) every Hermes message, answer or unsolicited, is rendered the same way as in ✅ Reviews: the gatekeeper header first, the body inside one quote (U6). Separation there comes from rendering, not from a topic.
- **D22 User-directed work (CR-004 Amendment B).** `/work <url> [notes]`, a forwarded or pasted GitHub link with a `🛠 Work on this` button, and `ossctl work <url>` create items marked `👤 requested by you` on every card. They skip ranking, daily caps, the size preference and the filters, and still pass both gates, forbidden paths, the AI-contribution ban, the CLA confirmation, the one-open-PR-per-repo check (overridable with a confirm) and the hard ceiling (§5.19).

## 3. Safety invariants
Unchanged and binding (DESIGN I1–I9, ADR-0004, ADR-0014):
- **S1** Approve, submit, resume, dry-run off, skill activation and proposal Start work (CR-004 gate 1) are deterministic gatekeeper actions. Each is two taps (Approve, then Confirm) on opaque `t:<22 chars>` tokens (24 bytes): single-use, expiring, bound to `tg_message_id`, and accepted only when `from.id` is the owner in the owner's private chat. No LLM output, chat text, Mini App, GitHub review or mirror event can approve.
- **S2** Every approval binds the exact commit SHA, the derived patch hash and the title/body hashes, plus the mode (`dry_run` recomputed at Approve, at Confirm and before every write). Any edit, dry-run toggle, reload or new rev invalidates it. A proposal approval (CR-004) binds the proposal hash, its version and the approved size class; a new proposal version, an expiry or a reload invalidates it.
- **S3** Nothing public happens before approval. Dry-run is on by default, and the mode is printed on every **public-action** card and in its Confirm sentence:
  - GitHub writes (`open_pr`, `push_update`, `comment`, `issue_claim`): the mode decides whether anything is published.
  - **Telegraph publish** (opt-in, §6.8): the one public write that is not a GitHub action. Dry-run does **not** govern it, so its confirm panel shows `🧪 DRY RUN (n/a: Telegraph is not GitHub)` or `🔴 LIVE (n/a: Telegraph is not GitHub)` and the sentence `This publishes now, even in dry-run mode.` Publish and Unpublish are refused while paused or killed ("Paused or killed: nothing was published.").

  Cards for local changes (skill activation, proposal Start work) also show the mode badge, marked "n/a: local change", and their Confirm says dry-run does not apply (§5.15, §9). So the badge is never missing, and it never implies a publish that won't happen or hides one that will.
- **S4** No inbound exposure by default. Any internet-reachable component (phase-2 Mini App) must meet all of these: optional, off by default, read-only, Ed25519 init-data verification, no secrets, no bot token, never able to approve.

New in this CR. Each invariant names the layer that enforces it:

| # | Invariant | Enforced by | Why there |
|---|---|---|---|
| U1 | **Untrusted text is data, never markup.** This covers Hermes output, issue and PR text, logs, skill text, mirror comments and feature payloads. Classic messages escape it (`escape.esc`, `make_visible`, `defuse_commands`). Rich messages carry it as plain strings in `blocks` with `skip_entity_detection: true`. Telegram `markdown`/`html` rich modes and MarkdownV2 are never used with interpolated content. Every leaf is redacted first (U14) | `telegram/ui/` renderers; an import-linter contract keeps `gatekeeper.chat` and `gatekeeper.features` from calling the outbox except through `telegram.ui` | One choke point that can be tested exhaustively |
| U2 | **Only the gatekeeper mints buttons.** Every `callback_data` is a gatekeeper token. Labels from untrusted sources are sanitized (§11.3) and never contain approval words. A follow-up token can only relay its stored text to Hermes; it never reaches the command registry | `telegram/ui/kit.py` `button()`, `chat/followups.py` | Hermes could otherwise forge a convincing "Approve" |
| U3 | **Approval and Confirm surfaces are classic messages with reply-markup tokens bound to their message ID.** They are never drafts, never rich, never ephemeral, never in-body buttons. Approve (and the proposal's `Start work`) appears only on the card | `telegram/cards.py`, `telegram/proposal_cards.py`, `CardPublisher` | Keeps the reviewed token paths; the only deltas are listed in §12.4. Ephemeral messages have `message_id` 0 |
| U4 | **Display artifacts are never bound or trusted.** Rich views, interdiff, HTML diff and mirror commits are all derived from `derived/<cid>/<rev>/`. Only `patch_sha256`, `head_sha` and the title/body hashes are bound | `review/` modules read only the derived directory | ADR-0014 |
| U5 | **The mirror can write only to the configured private repo.** The token is scoped by GitHub to that repo and proven by the enable-time probe. The writer refuses any other path. The repo must be private with Actions disabled before every push. Mirror content never goes upstream. GitHub reviews never approve | GitHub token scoping + `review_mirror_writer.py` (import-linter: only `review_mirror.py` imports it) + `review_mirror.py` pre-push checks | Defence in depth: token, code and runtime check |
| U6 | **Model-authored text is labelled.** Planner rationale, risk claims, self-review and "coder says" are prefixed 🤖 and shown in a quote. Gatekeeper-verified facts carry ✅ and no 🤖. **Hermes answers** depend on where they land: <br>• in 💬 Assistant and user chat topics (topic mode), everything is the assistant's, so answers render fully, without the header or the quote; <br>• in **flat mode** (topics off, the zero-setup default), approval cards and Hermes output share one chat, so every Hermes answer **and** every unsolicited Hermes message (feature card, notification, reminder) is rendered like rule 7: the gatekeeper header `🤖 <b>Assistant</b> · answer to your message` (unsolicited: `🤖 <b>Assistant</b> · <feature or "notice">`) comes first, and the whole body sits inside **one** quote. In rich mode, all body blocks are nested in a single `blockquote` block (`expandable_blockquote` when > 12 lines); in classic mode, `<blockquote expandable>`. Follow-up and feature buttons stay, because they are gatekeeper tokens. Separation is by rendering, not by topic; <br>• in ✅ Reviews, the gatekeeper header `🤖 <b>Assistant</b> · answer to your message` comes first and the whole body sits inside a quote (§4.4 rule 7); <br>• 🚦 Control and 📈 OSS feed never get an answer: text typed there is answered in 💬 Assistant (§4.4 rule 8) | `telegram/ui/kit.py` `claim()` vs `fact()`; `chat/answer_view.py` (header and quote by thread kind) | Prompt injection must not look like verification or like a card |
| U7 | **P0 alerts are never silenced** by quiet hours, `/quiet` or `/notify` | `telegram/notify.py` | Safety |
| U8 | **Bot-command links in untrusted classic text are defused.** U+2060 WORD JOINER goes after `/` when a letter follows. This is display only and is never applied to bytes that will be published (the `<pre>` parts and the PR title; the title is shown escaped in `<code>`, made visible but never defused or redacted, and `copy_text` carries the stored title unchanged). Because U+2060 is also in `escape._INVISIBLE`, any text that re-enters the gatekeeper (a quote copied into a comment, an echoed follow-up, a `Use as comment` source) first has the defuse marker removed by `sanitize.undefuse()`: every U+2060 that directly follows `/` is deleted **before** `make_visible`, so the user never sees `⟦U+2060⟧` from our own marker. Any other U+2060 is still shown | `escape.defuse_commands`, `ui/sanitize.undefuse` | A tap on a link like "/dryrun" inside a Hermes answer would send that command |
| U9 | **Opt-in surfaces stay off until confirmed.** The mirror, Telegraph articles and the phase-2 Mini App are enabled only through `config/telegram.yaml` plus a human-confirmed `/reload`, plus a passing doctor probe | config snapshot (DESIGN §7.9) | Security-relevant config never changes silently |
| U10 | **Topic routing never changes authorization.** Every update still needs `from.id == owner`, `chat.type == private` and `chat.id == owner`. `message_thread_id` decides presentation and routing only. **One exception:** `stopped_message_generation` (`MessageGenerationStopped`) has no `from` field, so it is checked by `authorized_chat_only()`: `chat.type == "private" and chat.id == owner`, read from `update.api_kwargs` (§6.1). Its only effect is to stop a stream that the owner's own message started, and it is accepted only for an active `draft_id` | `telegram/auth.py`: `authorized()` unchanged; new `authorized_chat_only()` used by that one handler | A stop update can't carry a user, and a private chat whose id equals the owner's id can only be the owner's |
| U11 | **Nothing is published to the web except approved GitHub actions and explicitly confirmed Telegraph articles.** An article can be created only by the gatekeeper, from an eligible `chat_answer` view (§6.8), after a publish tap and a confirm tap, and after the secret and personal-data scans pass. LLM components can't reach Telegraph at all (`telegra.ph`, `api.telegra.ph` and `edit.telegra.ph` are always denied for them) | `gatekeeper/telegraph.py` (the only module importing `telegraph_writer.py`, import-linter) + egress categories | The publish is public and irreversible, and its token stays with the deterministic component |
| U12 | **Unsolicited Hermes messages can't choose where or how loudly they land.** Feature cards, Hermes notifications and reminders go only to 💬 Assistant or user chat topics, never to ✅ Reviews, 🚦 Control or 📈 OSS feed. Each carries 🤖 in its header line (§6.7). **Flat mode has no topics**, so "never next to approval cards" can't hold by placement. There the guarantee is by rendering: every Hermes message carries the gatekeeper `🤖 Assistant` header with its body in one quote (U6), so it can't look like a card. `docs/SETUP.md` recommends turning topics on in @BotFather. The target thread comes only from the gatekeeper's own `chat_turns` row for the calling session (§6.7). Hermes P1 is capped at 5 per day, then sent as P2. Answers to the user's own message are not unsolicited: they follow §4.4 rules 7–8 and U6 (in ✅ Reviews: header plus quote, only `[📝 Use as comment]`) | `gatekeeper/api/notify.py`, `api/features.py`, `telegram/notify.py`, `telegram/routing.py` | A prompt-injected Hermes must not be able to place lookalike content next to approval cards or wake the user at night |
| U13 | **The size ceiling can't be changed from Telegram** (CR-004 Amendment B). `policy.diff.max_files / max_changed_lines / max_test_lines` have no picker, no token purpose, no typed argument and no `/reload` shortcut; they change only by editing `config/policy.yaml` plus the normal human-confirmed `/reload`. `/size`, `/filters` and `/repos … size` change only autonomous-discovery preferences (`xs`…`xl`; `xxl` is never selectable) and never touch an approved proposal, a user-directed item, a gate or the ceiling | `gatekeeper/discovery_prefs.py` (the only writer of preferences; rejects ceiling keys) + the strict config schema (CR-004) | The ceiling is a runaway-agent guard, not a preference; a prompt injection or a mis-tap must not move it |
| U14 | **Redaction before any send** (DESIGN §14.3). Every piece of untrusted or model-authored text passes one redactor before it reaches Telegram or a stored answer: `telegram/ui/redact.py`, which wraps `common.redact.Redactor` loaded with the policy secret patterns (`DEFAULT_SECRET_PATTERNS` + `policy.secrets.extra_patterns`), the gatekeeper's own secret values (`TELEGRAM_BOT_TOKEN`, `GITHUB_WRITE_TOKEN`, `HERMES_API_KEY`, `OSSBOT_TOKEN_PEPPER`, and when set `GITHUB_REVIEW_MIRROR_TOKEN` and `TELEGRAPH_ACCESS_TOKEN`) and the Kiro key hash check (each `policy.TOKENISH_RE` candidate whose sha256 is in `KIRO_KEY_SHA256` is replaced). It runs on: every untrusted text leaf in `classic.py` and `rich.py` (so every kit view, feature card and Hermes answer); every draft in `drafts.py`, which bypasses the outbox; `answer.md`; log tails and log documents; `chat_turns.answer_text` **before it is stored** (so Telegraph and `Use as comment` start from redacted text); follow-up items (an item with any hit is dropped, never shown as `[REDACTED]`). **Streaming:** a draft, a Stop partial and a truncated answer never show a secret's prefix: each is built with `draft_visible()` (§6.1 steps 3 and 5). Only a stream that completed normally is sent with `redact()` alone. The guarantee covers secret values and whitespace-free patterns (patterns that can match whitespace are refused at load, BA-16); the Kiro-key hash check is a final-text backstop, and the default `ksk_` pattern covers Kiro keys while streaming. **Verbatim publish bytes** (`<pre>` body, commit-message and comment parts, skill parts, `.patch`, `pr_body.md`, the HTML diff's code, **the PR title wherever it is shown** (the card's `PR title:` line, explorer section 9, `📋 Copy title`), and any text that will be published and is quoted back in a prompt (the current title and body in the S7 Title/Body ForceReply, the current comment text in FEAT-005's `edit_comment` prompt)) are never altered, because they must equal what is published or activated; they are only scanned. Packaging and every edit path already refuse them on a hit (`policy.check_text` with the gatekeeper's secret values, §12.1), so a render-time hit is a bug (§12.1) | `telegram/ui/redact.py` over the shared `gatekeeper/secret_scan.py` (§12.1), called by `classic.py`, `rich.py`, `drafts.py`, `chat/stream.py` (answer persistence, Stop and truncation paths) and the document builders; an AST test asserts each calls it. Publish bytes are gated earlier by `policy.check_text` in `packaging.py` and the edit paths, using the same scanner | Telegram is a cloud chat; one choke point for every new send path, including the draft lane that skips the outbox. It is a backstop against accidental echo (Hermes holds `HERMES_API_KEY` as its API-server key and its own Kiro key, and may repeat either), not a defence against deliberate encoding such as base64 |

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
| 🚦 Control | Pinned dashboard, command replies, P0 alerts, lifecycle summaries (CR-001), "local operator action" FYIs. Free text typed here is answered in 💬 Assistant (§4.4 rule 8) | P0 loud, rest silent |
| ✅ Reviews | Every approval card (proposal, PR, push_update, comment, claim, skill), proposal version diffs, `RESCOPE_REQUIRED` cards, explorer and file messages, draft reviews, Confirm panels, live PR status cards, drafted replies, merged/closed summaries, and answers to free text typed here (classic, under the `🤖 Assistant · answer to your message` header, body quoted; §4.4 rule 7). A candidate's whole lifecycle, from proposal to merge, stays in one topic because replies across topics are not supported (`ReplyParameters`) | P1 loud (rate-limited), updates silent |
| 📈 OSS feed | Discovery, vetting, stage progress, non-critical failures, digests, worker notices (§6.7). Free text typed here is answered in 💬 Assistant (§4.4 rule 8) | Silent (P2/P3) |
| 💬 Assistant | Default Hermes conversation (session `tg-<uid>-t<thread_id>`) | Normal (the reply to your own message) |
| 💬 user topics | One Hermes session each; renamed with `/title` | Normal |

Flat mode (topics off, or a topic call fails with "thread not found" after one re-create): every message is sent without a thread, with the section icon as its first character (CR-002 Amendment A, separation). Because cards and Hermes output then share one chat, every Hermes message is sent under the gatekeeper `🤖 Assistant` header with its body in one quote (U6, U12). Hermes sessions use `tg-<uid>-<n>` and `/new` increments `n`. The mode is re-checked at each start and on every "thread not found" error.

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
5. A reply to a gatekeeper view, resolved through the view's own `tg_views` row (never through the message text):
   - kind `file`, `explorer`, `interdiff`, `card`, `body_part` or `commit_msg`: the view's `action_id` (§12.2) is resolved to its current approval. What an open (`pending` or `armed`) approval means **depends on the action kind**:
     - `open_pr` or `push_update`: the reply is a **review comment** (§5.5). Replies to `body_part` and `commit_msg` messages are general comments, with the user's quote kept in `quote`.
     - `comment` or `issue_claim` (only `card` and `body_part` views exist for them): the reply is **never** a review comment. These kinds have no rev and no Request changes, and `CHANGES_REQUESTED` is invalid from the states they live in. Instead the gatekeeper answers with a confirm panel, a reply to the user's message (`tg_views` kind `confirm_prompt`): `Replace the comment text with your reply? · c_01JABC` + `[✅ Replace] [↩️ Cancel]`. Its tokens are the kit purposes `comment_replace` (single-use, 15 min, arg `{cid, action_id, text}`, where `text` is the user's reply after `undefuse` and normalization, ≤ 4,000 characters) and `comment_replace_cancel`. **Replace** runs FEAT-005's `edit_comment` path exactly as `📝 Edit text` would: normalization, `policy.check_text`, the new body stored, the approval invalidated `text_edited`, a new card sent, and the old card S12 `text_edited`. It is refused with the §12.2 outcomes when the action is no longer eligible (§12.4.3). **Cancel** edits the panel to `Not changed.` A refused policy check answers with the exact reason and stores nothing.
     - Either kind, when no approval is open: when the action is already `approved`, `executing`, `done` or `dry_run_done`, nothing is stored and the reply is answered `Submitted: comment on the PR instead.` (`Dry run done: this rev was not published.` for `dry_run_done`). When no approval is open and nothing was submitted (expired, invalidated), the reply gets the stale answer `This card is no longer current; review the new card.` and nothing is stored;
   - kind `proposal`: a proposal adjustment (§5.15);
   - kind `skill_card` or `skill_part`: relayed to Hermes in the 💬 Assistant session, with `Re: skill <name> v<version>` as quoted context (like `💬 Ask Hermes why`, §9), and answered in 💬 Assistant. It is never stored in `review_comments`.

   A document reply to a "Body as file" prompt is rule 3.
6. A non-reply message whose text or caption contains `github.com` issue, PR or repo URLs (in any topic, forwards included; at most 3 are carded): a **link card** per URL with `[🛠 Work on this]` (§5.19). When the message is nothing but the URL(s), routing stops here; otherwise it continues with rule 7, 8 or 9.
7. Free text, media, and replies to non-view bot messages **typed in ✅ Reviews**: relayed to Hermes in session `tg-<uid>-reviews`, and answered in ✅ Reviews as a **classic** message built by the gatekeeper (U6):
   - The first line is the gatekeeper header `🤖 <b>Assistant</b> · answer to your message`.
   - The whole Hermes body follows inside `<blockquote expandable>`, escaped and defused (U8). It is never rich, has no follow-up buttons and no feature cards.
   - An answer over 3,500 escaped characters is cut at a line boundary and ends `… full answer in 💬 Assistant`; the full answer is sent to 💬 Assistant with the first line `🤖 from your Reviews chat`.
   - The only button it may carry is one single-button row, `[📝 Use as comment]`. It is offered for a text message (not media) only when exactly one approval is `pending` or `armed` across all contributions **and its action is `open_pr` or `push_update`**. When the single open approval is a `comment` or `issue_claim` card, no button is offered, because those cards take no review comments (rule 5). The text names the target ("Tap to add your message above to c_01JABC r2's draft review"). The `use_as_comment` token converts **the user's own free-text message** (its stored text, after `undefuse`), never the Hermes answer.
8. Free text, media, and replies to non-view bot messages **typed in 🚦 Control or 📈 OSS feed**: relayed to Hermes in the 💬 Assistant session (`tg-<uid>-t<Assistant thread_id>`) and answered in 💬 Assistant. That answer starts with `↪️ from 🚦 Control: "<first 80 characters of your message>"` (or `📈 OSS feed`), so the context is visible. The originating topic gets one silent line, `🤖 Answered in 💬 Assistant`, as a reply to the user's message. Neither system topic has a Hermes session of its own.
9. Everything else goes to Hermes, in the session of its topic (💬 Assistant, a user topic, or the flat session): text, photos, albums, documents, voice, locations, and replies to any other bot message (the quoted context is passed, CR-002).

**Stale messages.** If `message.date` is older than 10 minutes when processed (the laptop was offline):
- read commands answer with "(sent <tg-time r>)";
- go and config commands still only produce their confirm button, and the prompt says "sent 2 h ago while I was offline";
- the pure stop commands `/pause`, `/kill`, `/drain` and `/dryrun on` run (stopping is always safe), and the reply notes the delay;
- a stale `/down` only produces its timeout picker, with the age shown ("sent 2 h ago while I was offline"), because `/down` is stop+confirm (§8.1) and is disruptive;
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
- **The PR title** is publish bytes: the exact string `open_pr` publishes and `title_sha256` binds. It is escaped and made visible inside `<code>`, but never defused or redacted (U8, U14). It is `scan()`ed at render; a hit invalidates the approval `render_failed` and sends the `Card withheld` stub instead of the card (§12.1). Packaging has already refused such a title (`Violation("gatekeeper_secret", "title")`), so that path is a bug.
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

  **Overrun formula** (`telegram/review/size_line.py`, unit-tested; numbers from CR-004's counting rules):
  - `code_changed = added_code + deleted_code`; test lines never count.
  - `overrun_pct = round(100 × (code_changed − est_code) / est_code)`, shown only when `est_code ≥ 1`. When `est_code = 0`, the line reads `⚠️ <b>est 0 code</b> · actual +a / −d code` instead of a percentage.
  - The ⚠️ highlight appears when `code_changed > est_code × (1 + size.overrun_tolerance)`, **or** when the measured class is larger than the estimated class, whichever comes first.
  - Examples: est 40, +142 / −30 → 172 → `+330%` (§5.1 table); est 48, the same diff → `+258%` (mockup 10).

Keyboard, in two phases. The minting rule is unchanged: Approve is minted only when `body_parts_delivered == body_parts_total`. The card's purposes now depend on the action kind and on whether an interdiff exists (§12.4 delta 3, `card_purposes(kind, has_interdiff)`):
- **Decision purposes** (`approve`, `reject`, `reject_reason`, `changes`) stay single-use.
- **View purposes** (`explore`, `files`, `interdiff`, `edit_text_pick`) are **multi-use**. They expire with the approval and die with it (`_kill_tokens`), so `🧭 Explore` works however often it is tapped.

While delivering, the card carries no tokens at all: only one `DisabledButton` `⏳ delivering 1/2…` (10.3; without 10.3 the card simply has no keyboard, as today). Delivery takes seconds, and minting nothing before it keeps `_maybe_arm` the only place card tokens appear.
```
while delivering:      [⏳ delivering 1/2…](disabled)
open_pr r1:            [✅ Approve & Submit](success) [❌ Reject]
                       [🧭 Explore] [🧾 Files (3)]
                       [✏️ Request changes] [📝 Edit text]
                       [🐙 Review on GitHub ↗](url, only when the mirror is on and pushed)
open_pr rN, interdiff: row 2 = [🧭 Explore] [🔀 Interdiff]                 (§5.7)
push_update:           [✅ Approve & Push](success) [❌ Reject fix]
                       [🧭 Explore] [🧾 Files (2)]                          (the incremental patch, DESIGN §14.4a)
                       [✏️ Request changes]
comment, issue_claim:  [✅ Approve & Post](success) [❌ Reject]
                       [📝 Edit text]                                       (opens the FEAT-005 edit_comment ForceReply directly)
```
`comment` and `issue_claim` cards have no Explore, Files or Request changes: their whole content is the verbatim body part under the card. `push_update` has no title or body, so it has no `📝 Edit text`. On `comment`/`issue_claim`, `📝 Edit text` skips the S7 picker (which offers title/body choices those kinds don't have) and opens FEAT-005's `edit_comment` ForceReply.
Changes from DESIGN §14.4:
- `View diff` becomes `Files`.
- `Test logs` moves into the explorer (section 7).
- `Edit title`, `Edit body` and `Clear my edits` move behind `Edit text` (S7).
- Labels stay ≤ 20 characters, with ≤ 2 buttons per row and ≤ 4 rows.

Silent or loud follows §5.13. The four action kinds keep their headers from DESIGN §14.4a. Their Confirm sentences are exactly what the frozen `ApprovalService.confirm_text` produces (§5.9); where DESIGN §14.4a words them differently, the code wins. Headers:
- `push_update`: 🔁 "Fix ready for PR #N".
- `comment`: 💬 "Comment ready".
- `issue_claim`: 🙋 "Issue claim ready".

### 5.2 S2: Explorer (`02-diff-explorer.svg`)
The explorer, the file view (§5.3) and the interdiff view (§5.7) exist only for `open_pr` and `push_update` actions, which have a patch. Every view token carries the view's `action_id` (§12.2), never just `(cid, rev)`.

`🧭 Explore` sends **one explorer message** as a reply to the card (`reply_parameters.message_id = card`), so the quoted header jumps back to the card. If an older explorer exists for this approval, it is edited to the stub "🧭 moved ↓". The explorer is a rich message when rich views are on (§11.3), else classic HTML. Navigation edits it in place. **Fallback during an edit:** when an in-place `editMessageText(rich_message=…)` of an explorer, file or interdiff view returns 400, the gatekeeper does not try to turn that message into a classic one. It sends the classic rendering of the same view as a **new reply to the card**, and records the new message in `tg_views`; the old row gets `stale = 1`, so the old message's buttons answer "Superseded…". The old message is then edited to the stub "🧭 moved ↓" (or "🧾 moved ↓"). If that edit fails too, it is deleted with `deleteMessage`, and if that also fails it is left as it is (INFO). Then it sets `rich_unavailable_until` (§14.5). Whether `editMessageText` can switch a message between rich and classic is a §17 item; this design doesn't depend on it.

Breadcrumb line: `🧭 <b>c_01JABC r2</b> · 3/9 Signals`. Sections:

| # | Section | Content (source) |
|---|---|---|
| 1 | Summary | The card facts, plus the mode badge and the approval expiry |
| 2 | Why this issue | Issue title and labels; the first 600 characters of the issue body (escaped, quoted, untrusted); maintainer confirmation facts (vetter); planner rationale 🤖 |
| 3 | Signals | Repo facts (stars, push recency, external PR merge rate, median first review, open PRs by us); policy checks ✅/❌ (CONTRIBUTING, AI policy, CLA, DCO, claim, competing PRs); CODEOWNERS for touched paths |
| 4 | Risk | Deterministic risk tags from derived numstat/raw: CI or workflow paths, dependency manifests or lockfiles, security-sensitive paths (policy list), binary, generated or vendored files, new files, deletions > 50 lines, files > 400 changed lines. Then planner risk and confidence 🤖 |
| 5 | Files | A numstat table (rich `table` or classic aligned `<pre>`): path · +/− · tags; 6 per page with a pager; each row has an `Open` button (button `i`) |
| 6 | Diff | Hunk index per file (`@@` headers and sizes); `Open file` buttons; `📄 .patch` and `🌐 HTML` document buttons |
| 7 | Tests | Each validation command: ✅/❌, duration, counts. Failing commands show the last 40 lines, redacted (U14), in `details` (rich) or an expandable blockquote (classic). `📄 Full logs` sends the `.txt`, redacted line by line before upload (zipped if > 45 MB, existing). Stack validation n/a. "Upstream CI runs after submit" |
| 8 | Self-review | Reviewer model ≠ coder model ✅ (fact); round i/N; verdict, blocking items and nits 🤖 (each ≤ 200 characters, at most 10) |
| 9 | PR text | Title in `<code>` (publish bytes: escaped and made visible, `scan()`ed, never defused or redacted; U8, U14); title/body "edited by you" flags; disclosure block present ✅; hidden-character count; "body parts are the messages under the card"; `📄 pr_body.md`; `📋 Copy title` (`copy_text`, ≤ 256 characters, the stored title unchanged) |

Explorer keyboard (multi-use `nav` tokens, 24 h, bound to the explorer message):
```
[◀] [3/9 ▾] [▶]
[🧾 Files] [🧪 Tests] [🤖 Review]
[💬 Comment] [⬇️ Show card]
```
- `3/9 ▾` swaps the keyboard for a section picker (3 per row, 9 sections).
- `⬇️ Show card` re-sends the card at the bottom with **`/approve` semantics**, which this CR fixes as the code's `reissue()`, always called through the one wrapper `AppContext.reissue(cid, actor, keep_expiry=True)` (§12.4 deltas 5 and 7, §12.4.3). In one transaction:
  - it is refused when any approval of the contribution is `confirmed` (`Submitting… try again in a moment`), so a re-send never kills a sibling mid-submission;
  - **every** open approval of an **eligible** action of the contribution is invalidated with reason `reissued`, and their tokens die (eligibility: §12.4.3; ineligible actions are skipped, and when none remain the toast is `Nothing to re-send (state IMPLEMENTING)`);
  - a fresh approval is created for each of those actions, with **that action's own** `body_parts_total` (`cards.part_count`), and each **inherits its predecessor's `expires_at`**, so neither the 72 h window nor the queue order changes;
  - after the commit, every re-created card is published (`on_approval_created`), and its body parts are re-sent (N4). Approve is minted on each card only when all of **its** parts are delivered.

  A contribution can have more than one open action: a live `issue_claim` beside the `open_pr` (`packaging.py`), or a `push_update` beside a drafted `comment` during watch (§5.11). In that case Show card re-sends **all** of them, and an armed sibling comes back at S1 on its new card. The toast says so: `Re-sent 2 cards` (or `Re-sent the card` for one). The old cards become "moved to a newer message" (S12). DESIGN §14.5 is amended to match.

  Explorer, file and draft-review tokens carry `approval_id = NULL` and `{cid, rev, action_id}` (§12.2). `action_id` is stable across `reissue()`, so they keep working against the new approval.
- Refusals: when the action has no open approval because it **expired**, Show card re-sends nothing. It answers `Expired: tap 🔁 Retry on the card for a fresh approval.` while the action is still eligible (§12.4.3), else `Nothing to re-send (state EXPIRED)`. When it was **submitted** (`approved|executing|done|dry_run_done`), it answers `Already submitted · PR #5678` (or `Dry run done · nothing was published`).
- **After Confirm**, the explorer and file views of that rev stay usable **read-only** while the action is `approved`, `executing`, `done` or `dry_run_done`: navigation works, and comment buttons answer `Submitted: comment on the PR instead.` (§12.2).
- **The explorer never carries Approve** (U3).

### 5.3 S3: File view and diff (`02-diff-explorer.svg`)
There is **one file-view message per approval** (per `(cid, rev)`), sent as a reply to the card and recorded in `tg_views(kind='file', cid, rev, path, page)`. It shows one file at a time:
- `Open` (explorer Files) or `Open file` (explorer Diff) **reuses** that message, editing it in place to the chosen file, when it exists and **fewer than 20 messages have appeared in its thread since it was sent**. This is counted from the gatekeeper's own records, never by message-ID distance (the ID counter may be shared with other chats, and strangers can message the bot):
  - `tg_thread_seq` (§14.2) holds one counter per thread key (topic name, user-topic `t<thread_id>`, or `flat`);
  - the counter is incremented by `Outbox.mark_sent` for every new message sent into the thread (`send*` methods, not edits), and by `routing.py` for every owner message received in it (after authorization, before classification);
  - the file view stores the counter value at its own send in `tg_views.state_json.seq`, and reuse applies when `seq_now − seq < 20`.

  Otherwise (older, or no file view yet) the file view is sent at the bottom and an older one is edited to the stub "🧾 moved ↓" (the explorer rule, §5.2). Either way there is at most one live file view per `(cid, rev)`.
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
- **Redaction (U14).** `.patch`, `pr_body.md` and the code in the HTML diff are verbatim publish bytes, so they are scanned (`redact.scan`) and never altered. The same holds for the PR title on the card, in explorer section 9 and in `📋 Copy title`, and for publish text quoted in the S7 and `edit_comment` prompts. Packaging already gates all of them with gitleaks, the Kiro-key check and the gatekeeper's own secret values (§12.1, `Violation("gatekeeper_secret", <surface>)`), so a hit is a bug and follows the §12.1 rule (ERROR, `render_failed`, stub, no document sent). Log documents and log tails are display text and are redacted. The HTML diff's card-facts header and every other non-code string pass the normal leaf redaction.

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

**Keeping the count current while armed.** Whenever a comment is saved while the approval is `armed`, the comment handler re-renders the Confirm panel text with the new count. The edit is a direct `editMessageText` of the card through the U3 edit path, with the **same** `reply_markup`: the unused `confirm` and `cancel` tokens are re-used and nothing is minted or rotated. Two races are closed:
- **Confirm tap.** It can't interleave with the save-and-edit. PTB runs one update at a time: the application is built without `concurrent_updates`, and the default update processor in the installed 22.8 has `max_concurrent_updates=1` (`telegram/ext/_applicationbuilder.py`). This holds because the comment save and the Confirm-panel refresh run **inline** in the update handler (one DB transaction and one `editMessageText`), within the update-handler budget (§12.5). Only long work leaves the update slot (Hermes turns, downloads, publishing, GitHub reads, document rendering, I/O-bound command effects). The `command` kind can invalidate PR approvals (`/reload`) while a comment handler awaits its edit; safety rests on the binding recheck (every binding carries `policy_hash`, recomputed at Approve, at Confirm and before the first write, and `invalidate_all_open` covers `confirmed`). The existing submit task is unchanged.
- **Housekeeping disarm and a concurrent `/reload`.** The disarm (§5.9) and a spawned `/reload` run in other tasks. After its edit, the comment handler re-reads the approval and, if it is no longer `armed`, re-renders the approval's **current** state: S1 when `pending` (`CardPublisher.render_s1(approval_id)`, dedupe key `s1:<aid>:<comment_id>`), S12 when invalidated or expired (`CardPublisher.render_stale(approval_id)`, dedupe key `stale:<aid>:<comment_id>`). Both keys are per occurrence (§12.2, §15.1 BA-3), so the edit that lands last always shows the approval's real state, and a dead Confirm panel never stays on a stale card.

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
- The Confirm sentence is exactly what `ApprovalService.confirm_text(binding)` returns, computed at that moment (DESIGN §12.4 for `open_pr` and DRY RUN). The other kinds are, in LIVE mode:
  - `push_update`: `Confirm: LIVE — push to PR #5678 on langfuse/langfuse as Amsal1 (author Amsal Khan). Expires in 5 min.`
  - `comment`: `Confirm: LIVE — comment on langfuse/langfuse#1234 as Amsal1 (author Amsal Khan). Expires in 5 min.`
  - `issue_claim`: `Confirm: LIVE — claim issue langfuse/langfuse#1234 as Amsal1 (author Amsal Khan). Expires in 5 min.`

  Where DESIGN §14.4a words these differently, the frozen code wins (§16), and DESIGN §14.4a is updated to match.
- The `⚠️ N unsent comments` line is kept current while armed (§5.5).
- The "Reviewed" coverage line is informational only, from `tg_views` opens; it never gates anything.
- Toasts (`answerCallbackQuery`, ≤ 180 characters) on refusal keep their current wording ("Paused or killed: nothing was approved. /resume first.", the lifecycle text, "Superseded: …").
- **Cancel** returns to S1 with rotated tokens (existing `cancel()` + `rotate_card_tokens`).
- **Confirm expiry** (no tap within `confirm_ttl_seconds`, 5 min) also returns to S1. Today nothing does this, so the housekeeping tick (≤ 10 s) runs the new `ApprovalService.disarm_expired()` (§12.4 delta 2): for every approval with `status='armed' AND armed_at <= now - confirm_ttl_seconds` it sets `status='pending', armed_at=NULL`, marks that approval's unused `confirm` and `cancel` tokens used, runs `rotate_card_tokens`, edits the card back to S1 with the fresh tokens (outbox dedupe key `disarm:<aid>:<armed_at>`, so a second time-out of the same approval is never dropped, §12.2), and audits `approval_disarmed {approval_id, reason:"confirm_expired"}`. The card shows one extra line, `↩️ Confirm timed out — tap Approve again`, until the next edit. A Confirm tap racing the disarm loses cleanly: the token is already used, so it gets the existing "token used" toast.

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
- **Drafted replies** (`comment` action) and fix rounds (`push_update`) arrive as normal approval cards, replying to the status card, and use the same S1/S8 flow with their per-kind keyboards (§12.4.1). Both can be open at once. Each card's views resolve their own action (§12.2), and `⬇️ Show card` from the `push_update` explorer (or `/queue → Open` on either) re-sends both (§5.2).
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
- keyboard `[⛔ Not approvable](disabled)` plus, per the table below, `[⬇️ Show current]` or `[🔁 Retry]`. **`⬇️ Show current` is rendered only when an open card exists for the stale card's contribution at render time** (the query below); otherwise the row falls back to the table's `🔁 Retry` (when the cause allows one and the action is eligible, §12.4.3) or to no second button.

**`⬇️ Show current` resolves by contribution, never by the stale card's action** (purpose `stale_show_current`, §12.2). The stale card's own action is usually cancelled (a new rev inserts a new action), so resolving by `action_id` would find nothing. The token's arg is `{cid}` for PR-side cards, `{cid, gate: 1}` for proposal cards and `{skill}` for skill cards. At tap time:
1. PR side: `SELECT 1 FROM approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE o.cid = :cid AND a.status IN ('pending','armed') LIMIT 1`. When a row exists → `AppContext.reissue(cid, actor, keep_expiry=True)` (§12.4.3; its eligibility and confirmed-sibling rules apply).
2. Otherwise, proposal side: the newest `proposal_approvals` row of the cid with status `pending` or `armed` → `AppContext.reissue_proposal(cid, actor, keep_expiry=True)` (§12.4.3). With `gate: 1` (a stale proposal card) this step runs first and step 1 second, so a superseded proposal shows the newest version, or the PR card once work has started.
3. Skill cards (`{skill}`): the newest `skill_approvals` row of that skill with status `pending` or `armed` → `AppContext.reissue_skill(skill, actor, keep_expiry=True)` (§12.4.3).
4. Nothing open → the toast `No open card for c_01JABC (state IMPLEMENTING)` (skills: `No open card for skill nutrition-log`), and nothing is sent.

So after r3 is packaged, Show current on the r2 card re-sends the r3 card. On proposal v1 it re-sends v2, and on a superseded skill version it re-sends the staged one.

| `approvals.invalidated_reason` (or status) | Label shown | Second button |
|---|---|---|
| status `expired` | `⌛ Expired` · "approval was valid 72 h" | `🔁 Retry` only while the action is eligible (§12.4.3). Once the contribution sweep moves it to `EXPIRED`, its `pending_approval` actions are cancelled (delta 7) and its stale cards are re-rendered without Retry; a tap that races the re-render answers `Nothing to re-send (state EXPIRED)` |
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
| `render_failed` (`CardPublisher`, §12.1; new) | "card couldn't be rendered (bug)" | none |
| `new_proposal_version` (`ProposalApprovals`; new) | `⚪ Superseded · v<N> is newer` | `⬇️ Show current` |
| `new_skill_version` (`SkillApprovals`; new) | `⚪ Superseded · v<N> staged` | `⬇️ Show current` |
| `tier_changed` (`SkillApprovals`, via the tier switch, §12.4 delta 9; new) | "Hermes tier changed" | `🔁 Retry` (a fresh skill card at the new tier, if the skill is still staged and within it) |
| any other code | "invalidated (<code ≤ 40 chars>)" | none |

The shared reasons (`reissued`, `binding_changed`, `binding_unavailable:*`, `config_snapshot_changed`, `killed`) apply to proposal and skill approvals with the same labels; proposal expiry is §5.15. These are the reason strings the code writes today (`approvals.py`, `control.py`, `config_snapshot.py`, `packaging.py`, `state_machine.py`, `handlers.py`) plus the four new ones above. The build pins them in `telegram/review/stale.py: REASON_LABELS`, with a unit test that greps every `invalidate_*(…, "<literal>")` call site in all three decision services and fails when a literal has no row.

`[🔁 Retry]` on S12 means **a fresh card via `/approve` semantics**: `AppContext.reissue(cid, actor, keep_expiry=True)` (§5.2, §12.4 delta 7), which creates a new approval for the same action. It runs only when the system is running and the action passes the eligibility predicate of §12.4.3: `pending_approval`, and the contribution is in a state where the action can still be submitted. A Retry during S5 (r3 being built) or after the `EXPIRED` sweep therefore re-sends nothing and answers `Nothing to re-send (state IMPLEMENTING)` (or `EXPIRED`).
- Expiry is decided per action: an action whose newest approval has **expired** (or has less than `confirm_ttl_seconds` left) gets a fresh `now + approval_ttl_hours`; every other action keeps its predecessor's `expires_at`.
- So Retry after `⌛ Expired` is the explicit act that restarts the 72 h window. A Retry after `live_check:*` or `binding_changed` keeps the window that was running, and so does any open sibling card that is re-sent with it.
- It is a kit token `stale_retry` (approval_id NULL, arg `{cid, rev, action_id}`). It is unrelated to the existing `retry` purpose, which re-runs a `failed` submission and is not used here.
- `⬇️ Show current` is the kit token `stale_show_current` (approval_id NULL, multi-use, 72 h, arg as above). It is the only kit purpose whose arg has no `action_id`, because it resolves by contribution (§12.2).

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
- **Loudness:** at most `telegram.loud_cards_per_hour` (default 2) new cards per hour notify with sound. Classes 0–1 always notify unless quiet hours apply, so a `👤` proposal you asked for arrives loud. Autonomous proposals (class 4) are **never loud**: each discovery cycle's ≤ `discovery.proposals_per_cycle` cards are sent silently and counted on the dashboard. The rest go out with `disable_notification: true` and are counted on the dashboard ("📥 4 pending, 2 new silently"). Cards re-sent at the user's request (Show card, Show current, `/queue → Open`, `/approve`, Retry) are always silent and don't count against `loud_cards_per_hour`.
- **`/queue`** shows the ranked list, 5 per page, with a section header per gate: `<b>Gate 2 · approval cards (3)</b>` (every gate-2-side card: PR previews, fixes, claims, drafted comments, rescope, ceiling and skill cards) then `<b>Gate 1 · proposals (4)</b>`. Lines look like `1. 🔁 PR #5678 fix · langfuse · expires <tg-time r>` and `4. 💡 Proposal v2 · langfuse #1234 · s · expires <tg-time r>`. Each line has an `[Open]` button that re-sends the card with `/approve` semantics and the expiry kept (`AppContext.reissue(cid, actor, keep_expiry=True)`, §5.2, §12.4.3; proposals: `AppContext.reissue_proposal(cid, actor, keep_expiry=True)`, §5.15). So opening an item never moves it in this list (`expires_at` is the second sort key), and a contribution with two open cards gets the toast `Re-sent 2 cards`. Sort toggles: `[⬆️ Oldest first] [🎯 By priority]`, with ✅ on the active one. `By priority` (the default) is the ranking tuple above. `Oldest first` sorts by `expires_at` ascending inside each gate section, which re-sends don't change either. Layout and limits (mockup `12-queue.svg`):
- 5 items per page, with numbered `[📥 Open 1]` … `[📥 Open 5]` buttons, 3 per row (labels ≤ 12 characters);
- then the sort row;
- then the pager row `[◀] [1/2 ▾] [▶]`;
- ≤ 4 rows in total;
- text ≤ 1,500 characters, each item line ≤ 120 escaped characters (repo names middle-ellipsized, untrusted text defused, U8). Numbering runs across both gate sections, and expired items aren't listed (their S12 card has `🔁 Retry` while the action is still eligible, §5.12).
- **Expiry warning:** a pending approval with < 12 h left gets one silent P2 reminder in the digest batch. One with < 1 h left gets a P1 reminder: a reply to the card, `[⬇️ Show card]`.

### 5.14 Review mirror integration (only when enabled; ADR-0019 option C)
When `review_mirror.enabled` is on and the push succeeded:
- the card's row 4 is `[🐙 Review on GitHub ↗]`;
- explorer section 1 shows `🐙 Mirror PR #12 · tree-equal ✅`.

**Late mirror push.** The mirror push is asynchronous and may land after the card was sent, or even while it is armed. When it lands, the gatekeeper adds row 4 with **`editMessageReplyMarkup`, re-using the same callback tokens** already on the card. They are read from `ui_tokens` for that `tg_message_id` and approval: the unused decision tokens, and the multi-use view tokens of `card_purposes(kind, has_interdiff)`, which are still valid however often they were tapped (§12.4 delta 3). Nothing is minted or rotated, and the URL button needs no token. If the card is armed, the Confirm panel is left alone and row 4 appears when the card next renders S1 (Cancel or confirm expiry). If the card is stale or consumed, nothing is edited.

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
- `⏭ Skip` → immediate. The card is finalized `⏭ Skipped` with `[↩️ Undo]` (10 min). Re-proposed only on a material change to the issue (CR-004 M6). Its effect is defined below under **Declining**.
- `🚫 Never…` → the keyboard swaps to `[🚫 This repo] [🚫 Kind: Bug]` / `[↩️ Back]`. The kind label is `🚫 Kind: <display name>`: 8 code points plus a fixed display name of ≤ 16 (§8.4), so ≤ 24. A choice edits the card into a confirm panel that names the target in full:
  - repo: `Never propose work from langfuse/langfuse again? Undo in /repos.` with `[🚫 Never this repo](danger) [↩️ Cancel]`;
  - kind: `Never propose "Small refactor" work (kind small_refactor) in any repo again? Undo in /filters → Kinds.` with `[🚫 Never this kind](danger) [↩️ Cancel]`.

  Then the card is finalized `🚫 Never: this repo` (or `🚫 Never: Small refactor`).
- `⏰ Later…` → `[1 d] [3 d] [7 d]` / `[14 d] [Custom…] [↩️ Back]`; `Custom…` is a ForceReply (`pending_input(kind='proposal_snooze_days')`, integer 1–90). The card is finalized `⏰ Snoozed until <tg-time>` with `[↩️ Undo]` (10 min).
- **Declining (Skip, Later, Never).** All three are decisions on the open `p_` approval, made through one call: `ProposalApprovals.reject(approval_id, reason, actor)`, with `reason ∈ {skipped, snoozed, never_repo, never_kind}`. Skip and Later call it on the tap; Never calls it on its confirm tap. In one transaction it:
  1. checks the approval is `pending` (an `armed` card is first returned to `pending` by Cancel; a `reject` on an armed card is refused with `Cancel the Start-work confirm first`);
  2. sets `status = 'rejected'` and `rejected_reason = reason` (§14.2);
  3. runs `_kill_tokens` for that approval, so its `✅ Start work`, `proposal_confirm` and `proposal_cancel` tokens die;
  4. transitions the contribution `PROPOSED → PROPOSAL_REJECTED` (`skipped`, `never_repo`, `never_kind`) or `PROPOSED → PROPOSAL_SNOOZED` (`snoozed`, with the snooze end), with actor human and an audit event `proposal_declined {approval_id, reason}`;
  5. for `never_repo`/`never_kind`, writes the never record through `DiscoveryPrefs` (the same record `/repos` and `/filters → Kinds` undo); for `snoozed`, writes CR-004's snooze record.
- **Undo** (`proposal_undo`) exists for Skip and Later only. It is a single-use kit token, valid 10 min, arg `{cid, version, approval_id}`. Never has no Undo: it is undone in `/repos` or `/filters → Kinds`, as its confirm text says. A tap, in one transaction:
  1. checks that the contribution is still `PROPOSAL_REJECTED` (reason `skipped`) or `PROPOSAL_SNOOZED`, and that the decision is ≤ 10 min old (from the `proposal_declined` audit time);
  2. removes the skip or snooze record;
  3. transitions the contribution back to `PROPOSED` over a **new human-only edge** (below);
  4. then calls `AppContext.reissue_proposal(cid, actor, keep_expiry=True)` (§12.4.3). That creates a new `p_` approval with the declined approval's `expires_at` (or a fresh TTL when it has less than `confirm_ttl_seconds` left, §12.4.3) and sends a fresh card as a reply to the declined one, which is edited to `↩️ Undone · see the card below`.

  A tap after 10 min, or after the state moved on, answers `Too late to undo; re-request with /work <issue url>`.
- **CR-004 amendment (state machine).** CR-003 adds two edges to CR-004's states: `PROPOSAL_REJECTED → PROPOSED` and `PROPOSAL_SNOOZED → PROPOSED`. Each is allowed for actor **human only**, guarded by `decided ≤ 10 min ago` and, for `PROPOSAL_REJECTED`, by reason `skipped`. They are audited and have a unit test for each edge, plus refusals for the gatekeeper actor, for a `never_*` reason and after 10 min. CR-004 says the Telegram presentation belongs in CR-003; these edges exist only to serve that presentation, so CR-004's state list and tests must be updated to match, like the DESIGN amendments in §14.8.
- `🧭 Details` → one reply message (kind `proposal_details`, rich when available, else classic) with: the full problem, approach and file list; tests to add or update; the validation plan; risks; alternatives; scout's codebase overview (layout, tooling, conventions, recent merged PR sizes, open PRs in the area) and the full version diff (§5.16). It carries `[◀] [i/N ▾] [▶]` section navigation and **no decision buttons**.
- `🙋 Ask maintainers first` → creates an `issue_claim` action, whose own approval card (S1/S8, mode-labelled, DRY RUN by default) follows as a reply. The proposal stays open.

**Delivery.** Autonomous proposals arrive silently (class 4, §5.13), at most `discovery.proposals_per_cycle` (3) per cycle and `proposal.max_per_day` (5) per day (CR-004). A `👤` proposal is P1 and loud (class 1). The v1 card of an autonomous proposal starts a new reply chain; a `👤` v1 card replies to the `Working on it` message when that is in ✅ Reviews (replies can't cross topics, so otherwise it starts a chain too). Later versions, Details, rescope, ceiling-hit and PR preview cards reply to the latest proposal card, so one candidate's history is one reply chain.

**Expiry** (`proposal.ttl_hours`, 72 h): the card is edited to `⌛ Expired · re-scouting if still relevant` with `[⛔ Not decidable](disabled)`. A tap on any of its tokens answers "This proposal expired; a fresh one will arrive if the issue is still open."

**Tokens and storage.** Proposal decisions run through a separate `ProposalApprovals` service (§12.4 delta 4). Its decision rows live in the `proposal_approvals` table that this CR's migration creates (§14.2), with ids `p_` + 10 Crockford characters (`ossbot.common.ids.new_id("p")`). The id is stored as `approval_id` in `ui_tokens`, so `_kill_tokens` on a PR approval never touches proposal tokens. CR-004 owns the proposal content (versions, fields, hashes); `proposal_approvals` references it by `(cid, version)`.
- **Decision purposes** (single-use): `proposal_start`, `proposal_confirm`, `proposal_cancel`. Start and Confirm are bound to the card's message ID and to the proposal binding `{v:1, kind:"proposal_start", cid, version, proposal_sha256, est_class, user_directed, policy_hash}`.
- **Kit purposes**: `proposal_skip`, `proposal_adjust`, `proposal_smaller`, `proposal_never`, `proposal_later`, `proposal_details`, `proposal_ask_first`, `proposal_undo`. They carry `approval_id = NULL` and `arg = {cid, version}` (`proposal_undo`: `{cid, version, approval_id}`, the declined approval), and resolve the current open proposal approval at tap time by `(cid, version)`. `proposal_undo` resolves no open approval; it checks the declined one, as described under Undo. That includes the `🧭 Details` section navigation (`nav` with `{cid, version, section}`), which has no PR approval or rev. A tap on a superseded version answers "Superseded: this proposal changed; review the new version."
- **Refusals.** Start work is refused at Start, at Confirm, and again immediately before its effect (enqueueing the plan stage) while the system is **paused, killed, draining or `💤 DOWN (workers)`**, with the existing toasts ("Paused or killed: nothing was approved. /resume first." and the lifecycle toast). The binding is recomputed at all three points; a mismatch invalidates with `binding_changed`.
- **Invalidation** reasons: `new_proposal_version`, plus the shared `config_snapshot_changed` (every open one), `killed` (armed and confirmed ones) and `binding_changed` (§5.12). Expiry is `proposal.ttl_hours`; there is no dry-run reason, because the binding has no mode (S3: n/a).
- **Re-sending** (`⬇️ Show current`, `/queue → Open`, Undo) is `AppContext.reissue_proposal(cid, actor, keep_expiry=True)`, which calls `ProposalApprovals.reissue` and publishes the new card after the commit (§12.4.3), with the same per-action expiry rule as §5.12. Eligibility: the contribution is `PROPOSED` and no proposal approval of it is `confirmed`. A proposal card has no parts, so the re-created approval is armable as soon as its card is sent (§12.4.3).

### 5.16 Proposal versions and the version diff
Each adjustment creates version `v<N+1>`. The new card is sent **as a reply to the previous card**, which is edited to `⚪ Superseded · v3 is newer` with `[⛔ Not decidable](disabled)` and `[⬇️ Show current]` (`stale_show_current` with `{cid, gate: 1}`, which re-sends the newest open version, §5.12), so the old Start work can't be used (the binding includes the version and the proposal hash). The new card has one block right after the header:
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
- `🧭 Explore` opens the normal explorer on the measured candidate in **read-only mode**. There is no PR approval yet, so its tokens carry `{cid, rev, action_id: null}` and navigation reads only that rev's derived artifacts. It has no `💬 Comment`, no `⬇️ Show card` and no Approve. The ceiling-hit card's `🧭 Explore` (§5.18) works the same way.
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
- `✂️ Split (adjust plan)` works like `📏 Make it smaller`: it sends the fixed feedback "Split this into parts that each stay under the ceiling; propose part 1 first" to scout right away, and the card's keyboard becomes `[⛔ Revising…](disabled)`. A ForceReply can't be pre-filled (its only hint is `input_field_placeholder`, 1–64 characters), so the gatekeeper then sends one **optional** ForceReply prompt, `✂️ Split requested.`, with the placeholder `Anything to add? (optional)` (27 characters) and `pending_input(kind='proposal_adjust')`. A reply within 15 min is handled as a normal `✏️ Adjust plan` on whatever proposal version is newest when it arrives. No reply changes nothing.
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
The title and state come from the gatekeeper's read-only GitHub client. The handler queues the request (DB only) and spawns the read as a tracked task (§12.5, kind `github_read`, 10 s timeout), which then enqueues this reply. On a read failure or timeout the line says `(details unavailable)`, and the request is still queued. `✖️ Cancel request` (kit token, valid until the proposal card is sent) cancels the scout.

**Forwarded or pasted links.** A non-command, non-reply message (text, caption or a forward) that contains one to three `github.com` issue, PR or repo URLs gets one **link card** per URL, as a reply to the message:
```
🔗 <b>langfuse/langfuse #1234</b> · issue · open · "Fix trace export when spans are empty"
Notes: "keep it minimal"          (only when the user typed text besides the URL; never for a forward)
[🛠 Work on this] [✖️ Dismiss]
```
- `🛠 Work on this` is exactly `/work <url> <notes>`: the card is edited to the `Working on it` text above. The facts on the card come from a `github_read` task too (§12.5).
- **Notes.** For a typed or pasted message, the notes are the rest of the message text with the URLs removed (≤ 1,000 characters, `make_visible`). The link card then shows them before the buttons, so nothing goes to scout with the owner's authority unseen: `Notes: "<first 80 characters, escaped and defused>"` (`…` when longer). **When the message is a forward** (`forward_origin` is set), the text is someone else's, so the notes are **empty**: no `Notes:` line is shown and nothing of the forwarded text goes to scout. To add notes to a forwarded link, the user types `/work <url> <notes>`.
- More than 3 URLs in one message is rule 6 too: link cards for the first 3, then one line "+N more links: use /work for each".
- If the message contains nothing but the URL(s) (and whitespace), it is **not** relayed to Hermes. Otherwise it continues to routing rule 7, 8 or 9 as well, so "what's this issue about? <url>" still gets an assistant answer.
- The link card is a gatekeeper message built only from the parsed URL, the GitHub API facts and, for a non-forwarded message, the `Notes:` line of the owner's own text (escaped, defused, ≤ 80 characters shown).

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
0. **Where a turn runs (normative, §12.5).** The update handler never awaits Hermes. It does only bounded local work:
   - it checks that the session is free: `Handlers` keeps a **session map** `session_key → Task` of running turns (§12.5 item 3); a session is busy while its entry exists;
   - it inserts the `chat_turns` row (`status='streaming'`);
   - it sets the 👀 reaction through the outbox;
   - it spawns the turn as a tracked task, `Handlers.spawn(run_turn(turn_id), kind="chat_turn", session_key=…, on_done=finalize_turn(turn_id))`, which registers the task in the session map before returning. There is no `await` between the check and the registration, so no other update can slip in.

   **Cleanup is owned by `spawn`, not by the turn's body** (§12.5 item 2). A task cancelled before its first step never runs its `finally`, which happens when a message and a `/kill` arrive in the same `getUpdates` batch (the offline catch-up case). So the task's done callback, which asyncio runs in every case, removes the session-map entry (only if it still points to this task) and calls `on_done`. `finalize_turn` closes the row in one transaction: `UPDATE chat_turns SET status = :s, finished_at = COALESCE(finished_at, :now) WHERE turn_id = :id AND status = 'streaming'`, with `:s` = `stopped` when the task was cancelled and `failed` when it raised. A turn that finished normally has already set `done`, so the `WHERE` makes the callback a no-op. At gatekeeper start, every row still `streaming` is set to `failed` (no task survives a restart), so U12 never accepts a session on the strength of a dead turn. **One turn per session at a time:** a message (or follow-up tap) for a busy session is not relayed. It is answered with `⏳ Still answering — tap ⏹ Stop or wait`, a reply to it (follow-up tap: the same text as a toast), and the user re-sends it after the answer. Other sessions (another topic) run their own turns concurrently.
1. Routing (§4.4) relays the message to `ChatBackend` from inside that task. The Hermes call is `POST /v1/responses` with `stream: true` and `X-Hermes-Session-Key` (topic or flat session). The stream is SSE: `response.output_text.delta`, `function_call` and `function_call_output` items, `reasoning` summaries and `response.completed|cancelled|failed` (Hermes API-server docs). The live contract comes from `GET /v1/capabilities` (existing check).
2. Immediately: `sendRichMessageDraft` with a `thinking` block ("Thinking…"), or an empty-text `sendMessageDraft` when rich is unavailable. Both use `draft_id = turn_id` (a positive int63) and `can_stop: true`.
3. **Draft lane** (`telegram/drafts.py`, in-memory, never persisted in `tg_outbox`):
   - Drafts are coalesced (the latest state wins) and sent at most once per 1.0 s per chat.
   - They yield whenever the outbox has a due row (cards and alerts first).
   - They are refreshed at least every 20 s even without new tokens, because a draft lives for 30 s: the heartbeat text is "⏳ 45 s · reading 2 pages".
   - Content is the partial answer converted by `md2blocks` (tolerant of unclosed fences), plus one status line from tool items (§6.3).
   - **Redaction and hold-back (U14).** Each draft is built from the whole accumulated text in this order: (1) `redact()` over the full text, so every complete secret is already replaced; (2) cut at `max(last whitespace boundary, len − 256)`, holding back the trailing incomplete token until the next delta or the final message; (3) `md2blocks`. A secret still arriving is therefore always inside the held-back tail, so no prefix of it is ever shown: secret values contain no whitespace and are shorter than 256 characters, and every pattern never matches whitespace and matches (and is redacted) as soon as its minimum length, also below 256, has arrived. All three properties are checked when the redactor loads (BA-16): a secret value with whitespace or ≥ 256 characters, or an `extra_patterns` entry that can match a whitespace character or has a minimum width ≥ 256, refuses the start or the `/reload`. So the no-prefix guarantee covers secret values and whitespace-free patterns; multi-line secrets such as PEM blocks are left to gitleaks on publish bytes. The Kiro-key hash check fires only on a complete token, so it is a final-text backstop; while streaming, Kiro keys are covered by the default `ksk_` pattern. The status line is built only from tool names and domains (§6.3) and is redacted too.
4. **Completion:** the final answer is redacted in full (U14), then sent with `sendRichMessage` (`blocks`, `skip_entity_detection: true`), or with `sendMessage` chunks (classic, escaped, §11.4). The same redacted text is what `chat_turns.answer_text` stores.
   - Answers over 30,000 characters (rich) or 3 classic parts are split at block boundaries.
   - Answers over 3 parts also get an `answer.md` document, built from the redacted text.
   - **Only a stream that completed normally** is sent with `redact()` alone. A stream that ends early (Stop, step 5; an answer cut at 200 KB or an event over 1 MB, §14.5) may end inside a secret, so its text is `draft_visible(text)` plus `…` and the footer `⏹ Stopped by you` or `…(truncated)`, and that is also what `chat_turns.answer_text` stores.
5. **Stop:** `stopped_message_generation` (`draft_id` must match an active turn) is handled in the update slot within 2 s even while the stream is open, because the stream runs in its own task (step 0). The handler sets the turn's stop event and returns. (A stop can't precede the task's start: the draft it targets is sent by the task.) The turn task then does three things, and the stream has ended within 2 s of the update:
   - It cancels Hermes. When `/v1/capabilities` advertises runs, the relay uses `POST /v1/runs` + `GET /v1/runs/{id}/events` and stops with `POST /v1/runs/{id}/stop`. Otherwise it closes the Responses SSE stream (**[VERIFY]** that closing cancels the turn at v0.22.2). The local SSE read is closed first in both cases, so our stream ends at once; the `/stop` call follows with a 5 s timeout, and its failure is the §14.5 "Hermes stop call" row.
   - It sends `draft_visible(partial)` (the whole text redacted, then the trailing incomplete token dropped, step 3) followed by `…`, as a final message with the footer `⏹ Stopped by you`. The same text is stored in `chat_turns.answer_text`, so Telegraph and `Use as comment` never start from a secret's prefix either (U14).
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
  - **drop** (never cut) items longer than 64 characters. The label **is** the text: there is no separate hidden text, so what the user taps is exactly what is sent;
  - drop items in which `redact()` finds anything (U14), so no label ever shows `[REDACTED]` or relays it.
- Each item becomes a `chat_followup` token (24 h, single-use, bound to the answer message, `arg = {"text": <label>, "turn_id": …}`). On a tap the gatekeeper:
  1. checks `arg.text` equals the label it rendered (both come from the same stored row; a mismatch is a bug and is refused at ERROR);
  2. **first** sends the echo `↪️ <text>` as a bot message in the same topic (so the user sees what is being sent before Hermes gets it);
  3. then relays the text to Hermes as the user's next turn, recording `chat_turns.origin = 'followup_tap'` (§14.2) so audits and the Telegraph eligibility check can tell it apart from typed input.

  It never reaches the command registry.
- One button per row (single-button rows allow ≤ 64 characters, §11.2), ≤ 3 rows.

### 6.5 Photos, albums, documents, voice and locations (CR-002 Amendment A)
- **Acknowledge:** `setMessageReaction(👀)` on the user's message as soon as it is accepted. It becomes `👌` on success (or `🤷` on unsupported input) when the answer is sent.
- **Albums:** messages with the same `media_group_id` arriving within 1.5 s of each other are one Hermes turn, capped at 10 images. Each album message's handler only appends the message to an in-memory buffer keyed by `media_group_id`. The first one also spawns a collector task (§12.5, kind `media`), whose `on_done` drops that buffer in every outcome, including a cancel before the collector starts. The collector waits until 1.5 s have passed since the last append, then downloads and strips the images and starts the turn (§6.1 step 0). No handler sleeps or waits for the album.
- **Downloads and stripping** (`getFile`, the download, the metadata walker) always run in the `media` task, never in the update handler.
- **Images:** sent as `input_image` data URLs. The vision model comes from `models.yaml roles.vision`. The footer shows `👁 vision: <model>`.
  - **Photos** (`message.photo`): the gatekeeper downloads the largest `PhotoSize` whose longer side is ≤ 1,600 px. Telegram already re-encodes photos as JPEG and supplies the smaller sizes, so no resizing is needed in ossbot.
  - **Image documents** (`message.document`): accepted only as JPEG, PNG or WebP, identified by **magic bytes** (`FF D8 FF`; `89 50 4E 47 0D 0A 1A 0A`; `RIFF….WEBP`), not by MIME type or filename, and ≤ 5 MB (the vision input cap). HEIC/HEIF, GIF, TIFF, SVG and anything else get "Send it as a photo instead (tap 🖼, not 📎)". Larger files get "Image is 12 MB; as a file the limit is 5 MB — send it as a photo".
  - **Metadata stripping** (CR-002 toggle, on by default) runs in the gatekeeper in `chat/imgmeta.py`, a pure-Python container walker that **never decodes pixels** (no image library in the TCB, D20):
    - JPEG: walk the marker segments up to SOS; drop `APP1`–`APP15` (EXIF, XMP, ICC is `APP2` and is dropped too) and `COM`; keep `APP0` (JFIF), `DQT`, `SOF*`, `DHT`, `DRI`, `SOS` and the entropy-coded data unchanged.
    - PNG: walk the chunks; drop `eXIf`, `tEXt`, `zTXt`, `iTXt` and `tIME`; keep every other chunk; verify each kept chunk's CRC.
    - WebP: walk the RIFF chunks; drop `EXIF` and `XMP ` and clear the matching flags in `VP8X`; fix the RIFF size.
    - Any structural error (bad length, truncated segment, CRC mismatch, more than 4,096 segments or chunks) rejects the image with "That image file looks damaged; send it as a photo". Never a partial strip.
    - Fuzzed with Hypothesis (random truncation, length fields and chunk orders, plus a corpus of real files with GPS tags) in `tests/unit/chat/test_imgmeta.py`: the walker either returns bytes with no metadata chunk/segment left or raises `ImageRejected`; it never raises anything else and never takes more than 50 ms per MB.
  - Pillow is not used (decision and alternatives in ADR-0020, "Pillow"). Image *tools* that do need decoding (phase 2, §13 item 12) run in a `--network none` sandbox job, where Pillow may live in the sandbox image.
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

**Where unsolicited Hermes messages go (U12).** This applies to feature cards, `POST /v1/notify` from Hermes (role `requester`) and reminders created by `ossbot_reminder_create`. Answers to the user's own message are not covered here; they follow §4.4 rules 7–9:
- The plugin sends the `X-Hermes-Session-Key` of the turn it is serving. The gatekeeper accepts it only if its **own** `chat_turns` table has a turn for that `session_key` with `(status='streaming' AND started_at >= now − 30 min) OR finished_at >= now − 10 min`. The 30-minute bound is far above the 120 s idle timeout of any live stream. A `streaming` row normally ends: the task's done callback closes it even when the turn is cancelled before it starts, and a restart resets leftovers to `failed` (§12.5). The `started_at` bound makes the check time-bounded even when that callback's `finalize_turn` fails (a DB error leaves the row `streaming` until the next start, §14.5). The target thread is that row's stored `thread_id`. An unknown or stale key gets 409 `no_active_turn` and nothing is sent (INFO log).
- The resolved thread must be 💬 Assistant or a user chat topic. In flat mode there is only the one chat, so the message is rendered under the gatekeeper header `🤖 <b>Assistant</b> · <feature or "notice">` with its whole body in one quote (U6); its buttons stay. A session whose thread is ✅ Reviews (`tg-<uid>-reviews`) gets its feature cards and notifications in 💬 Assistant instead, with a first line `🤖 from your Reviews chat`. No unsolicited Hermes message is ever sent to ✅ Reviews, 🚦 Control or 📈 OSS feed; the only Hermes text in ✅ Reviews is an answer to the user's own message there, under the 🤖 header and in a quote (rule 7).
- Every Hermes-originated message carries 🤖 in its header line: notifications and reminders start with `🤖`, and feature cards end their header with `· 🤖 assistant` (e.g. `🍽 Meal logged · lunch · 12:40 · 🤖 assistant`, keeping the one-leading-icon rule of §11.1). So none can look like a gatekeeper fact or an approval card.
- Priority: `api/notify.py` has no priority today. This CR adds an optional `priority` field to `NotifyRequest`, either `"P1"` or `"P2"` (default `"P2"`). P0 is gatekeeper-internal only: a request with any other value, `"P0"` included, gets 422 and nothing is sent. Hermes gets at most 5 P1 messages per local day; after that, its P1 requests are sent as P2 and counted in the digest ("Hermes asked for 3 more loud notices today").
- **Worker notices** (role `worker`, the `ℹ️` prefix today) are not Hermes and have no session. They go to 📈 OSS feed as P2 by default. A worker may request P1, which is still subject to the existing 10/h limit per caller and to quiet hours.
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
- the turn's thread is not ✅ Reviews (`chat_turns.thread_id` ≠ the Reviews thread; Reviews answers are classic quotes anyway);
- the turn produced no feature card: `chat_turns.feature_cards = 0`. `api/features.py` increments that counter whenever it accepts a `POST /v1/features/*/cards` that it resolved to this turn (§6.7).

All these facts, and the answer text itself, come from the `chat_turns` row written when the turn finished (`answer_text`, `tools_json`, `had_attachments`, §14.2), never from the callback.

**Eligibility is re-checked at every step.** A feature card can still be accepted for the turn up to 10 minutes after it finished (§6.7), after the `📖` button was rendered. So one function, `telegraph.eligible(turn_row)`, evaluates the whole list above from the **current** `chat_turns` row at four points: when the button is rendered, at the `📖` tap, in the Confirm handler, and immediately before `createPage`. At the tap it answers with that text as a toast and opens no panel; at Confirm or before `createPage`, a failure refuses with `Not published: this answer is no longer eligible`, edits the panel to that text, and nothing is sent to Telegraph (INFO).

The user's question is never included. Only the answer text is published, plus the source links.

**Flow** (two taps, like any outward action):
1. `📖 Open as article` (token `article_publish`, bound to the answer message, 24 h) sends a confirm panel as a reply to the answer, leaving the answer itself untouched. The panel is classic, ≤ 600 characters, `tg_views` kind `confirm_prompt`; its confirm token is bound to the panel's own message, and Cancel edits it to `Not published.`:
   ```
   📖 <b>Publish as a public Telegraph page?</b> · 🧪 DRY RUN (n/a: Telegraph is not GitHub)      (🔴 LIVE (n/a: …) in LIVE mode)
   This publishes now, even in dry-run mode.
   Anyone with the link can read it; Telegraph can't delete pages — Unpublish only blanks it.
   Only secrets, email, phone and name are checked — read the answer for other personal details first.
   [📖 Publish](danger) [↩️ Cancel]
   ```
   The confirm token is single-use, 5 min.
   The Confirm handler only checks the token and control state, edits the panel to `⏳ Publishing…`, and spawns the rest (step 3, including the scans and the retries) as a tracked task (§12.5, kind `telegraph`). Unpublish works the same way.
2. **Control-state refusal** (S3). While the system is **paused or killed**, the publish tap, the Confirm tap and the point just before `createPage` are all refused with the toast `Paused or killed: nothing was published.` The panel is edited to `Not published (paused or killed).` Unpublish (`editPage`) is refused too, because a kill stops every outbound write: `Paused or killed: the page was not changed and is still public. /resume first.` Draining and `💤 DOWN (workers)` don't block Telegraph, which is gatekeeper-only.
3. On Confirm, the gatekeeper:
   - re-evaluates `telegraph.eligible` on the current `chat_turns` row (above), and does so again right before `createPage`;
   - rebuilds the content from the stored answer (`chat_turns`, not from the callback; already redacted, U14);
   - runs the scans: policy secret patterns, its own secret values, the Kiro key hash check, `gitleaks stdin`, the confidentiality floor and denylist (ADR-0015), and the profile's personal values (email, phone, real name). There is no override; a hit gives "Not published: the answer contains <class>";
   - converts `md2blocks` output to Telegraph nodes. Only `p h3 h4 ul ol li pre code blockquote b i s u hr br` are used, and `a` with http(s) `href` only. No `img`, `iframe`, `video` or `figure`, so no remote media or tracking. Tables become `pre` text;
   - calls `createPage` with title = the first heading (or the first 60 characters) + ` · ` + 8 random base32 characters, which makes the path unguessable. `author_name` and `author_url` are empty by default (`telegraph.author_name` is optional), so no identity is attached.
4. A reply to the answer: `📖 <b>Published</b> · <title> · public link, not deletable (only blanked)`, with link preview on (Instant View) and `[📖 Open ↗](url) [📋 Copy link] [🗑 Unpublish]`. **After a `/kill`:** a task cancelled before its request started edits the panel to `Not published (stopped).` (its `on_done`, §12.5). A task cancelled while its request runs under `asyncio.shield` still finishes the request and the `telegraph_pages` write, and the shielded section then sends the announcement itself: this step's reply, or, when the cancel has already been recorded, the panel edit `📖 Published (after stop) · public link · see /articles` (for Unpublish: `🗑 Unpublished (after stop)`). So the panel never stays at `⏳ Publishing…` and the user always learns that a page exists.
5. **Unpublish** (confirm tap; refused while paused or killed, step 2): `editPage` with title "Removed" and one paragraph "This page was removed by its author." The UI says plainly that cached copies, Instant View caches and anyone who opened it may still have the content.
6. **`/articles`** lists the published pages from `telegraph_pages`, with Unpublish buttons.

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
| P2 FYI | stage done or failed (non-blocking), discovery found N, CI passed, dry-run done, mode changed (FYI), local operator action | silent | held until quiet hours end, then batched (§7.3) | 2-minute window |
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
- **Rows with buttons are never held or merged.** A row with `bind_tokens_json` or `approval_id` set (any kit view with buttons, any card or card part) is never written as `held` and never merged: a merged row's tokens would never be bound, and a card part counts towards Approve. Inside quiet hours or a batch window such a row is sent individually right away with `disable_notification: true`. `Outbox.enqueue` enforces this, and a unit test covers it (`test_outbox_hold.py`).

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

**Where a command's effect runs (§12.5).** A command or confirm handler whose effect awaits anything beyond SQLite and local reads does not run it inline: the `/hermes` surface probe, the root-helper and lifecycle requests of `/up`, `/down`, `/reload` and `/egress` (including the proxy re-render), and the `/model` writer. The handler checks and uses the token, edits the reply to `⏳ <verb>…`, and spawns the effect as a `command` task, which edits the reply to the outcome when done. `/kill` does not cancel `command` tasks: those effects keep their Phase 1 kill rules (each re-checks control state where Phase 1 does). Built-ins whose effect is local (`/status`, `/pause`, `/kill`, `/resume`, `/dryrun`, `/help`) stay inline.

| Command | Class | Picker / flow | Notes |
|---|---|---|---|
| `/start` | read | Welcome + `/dashboard` | Global command |
| `/help` | read | Generated from the registry, grouped Control / Reviews / Assistant / Settings | Global command |
| `/settings` | personal | `[🖼 Rich views: auto] [🔕 Notifications…] [🌙 Quiet hours…] [📰 Digest…] [🐙 Mirror: off ⓘ]` | Mirror enabling is config (U9); the button explains how |
| `/status` | read | Full status text (existing) + `[📌 Dashboard]` | |
| `/dashboard` | read | Re-send and re-pin | |
| `/queue` | read | Ranked pager, gate 2 then gate 1, `[📥 Open n]` keeps each item's expiry (§5.13, mockup 12) | |
| `/work <url> [notes]` | request | Typed only (the URL is the argument); without an argument: "Send /work <GitHub issue, PR or repo URL> [notes], or just paste a link" | CR-004 Amendment B; §5.19 |
| `/size` | tune | §8.4 picker | Autonomous discovery only |
| `/filters` | tune | §8.4 toggle pages | Autonomous discovery only |
| `/approve [cid]` | read | Picker of pending approvals → re-send the card (token rotation N4; `AppContext.reissue(cid, actor, keep_expiry=True)`, so an open approval keeps its expiry and an expired one gets a fresh 72 h). Only eligible actions are re-sent (§12.4.3): during S5 (`IMPLEMENTING`), after the `EXPIRED` sweep or while a sibling is `confirmed`, it answers `Nothing to re-send (state …)` or `Submitting… try again in a moment` and creates nothing | Never approves by itself (existing) |
| `/review [cid]` | read | Picker → explorer for that card | New |
| `/reject [cid] [reason]` | config | Picker → reasons `[Not valuable] [Wrong approach] [Repo unsuitable (7d)] [Other…]` → confirm | Existing behaviour |
| `/pause` | stop | Immediate | |
| `/resume` | go | Confirm: "Resume: scheduling restarts; approvals need Approve+Confirm again." | |
| `/kill [reason]` | stop | **Immediate** when typed (≤ 2 s while a Hermes answer streams; ≤ 8 s behind a Confirm live preflight, D14); the dashboard button is two-tap | Soft kill (existing). Also cancels every `chat_turn`, `media`, `telegraph` (before its request starts), `github_read` and `render` task (§12.5); `submit` and `command` tasks keep their Phase 1 kill rules |
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
| `/skills` | read | Staged and active skills; staged skills show `[Open card]` (§9), which re-sends the card with `AppContext.reissue_skill(skill, actor, keep_expiry=True)` (§12.4.3) | New (CR-002 self_improve) |
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

**`/filters`** (four pages, one message; the page name is in the text, the pager row navigates and wraps around):
```
🧰 <b>Discovery filters</b> · autonomous only · 1/4 Kinds
✅ = the bot may propose this kind. 👤 /work ignores filters.
[✅ Bug] [✅ Docs]
[✅ Tests] [✅ Small feature]
[✅ CI fix] [✅ Small refactor]
[⬜ Breaking change] [⬜ New dependency]
[⬜ API redesign] [⬜ Crypto/security]
[◀ PRs] [↩️ Undo] [Labels ▶]
```
- **Page 1, Kinds**: the union of CR-004's `allowed_kinds` and `excluded_kinds` defaults, one toggle each (✅ allowed / ⬜ not). Stored as `allowed_kinds`; `excluded_kinds` is its complement. Display names are fixed constants (≤ 16 characters), never repo or model text.
- **Page 2, Labels and tier**: `require_labels_any` toggles for `good first issue`, `help wanted`, `bug` and `documentation`, plus `[⬜ Any label (no label required)]`. Then the validation tier as a radio row, `[T0] [✅ T0–T1] [T0–T2]`. Pager `[◀ Kinds] [↩️ Undo] [Age ▶]`.
- **Page 3, Age and activity** (radio rows; every label names its filter, because an inline keyboard has no row headers):
  ```
  [Age 30 d] [Age 90 d] [✅ Age 180 d]
  [Age 365 d] [Age any]
  [Active any] [✅ Active 30 d]
  [Active 90 d]
  [◀ Labels] [↩️ Undo] [PRs ▶]
  ```
  `Age …` is `max_issue_age_days`; `Active …` is `min_maintainer_activity`.
  - Row layout is fixed and does not change with the selection.
  - Rows 1–2 use 3 per row: every label, with the `✅ ` prefix, is ≤ 12 characters (`✅ Age 365 d` = 11).
  - The `Active` rows use 2 per row, because `✅ Active 30 d` is 13 characters.
- **Page 4, Languages and PRs**:
  - languages from `profile.yaml` as toggles, 2 per row, ≤ 6 (≤ 3 rows), then the text line "+N more in config"; names are cut to 22 characters with a middle ellipsis;
  - `max_open_prs_by_me_per_repo` as the radio row `[✅ 1 open PR] [2 open PRs] [3 open PRs]` (each ≤ 12);
  - pager `[◀ Age] [↩️ Undo] [Kinds ▶]`.
- Each toggle applies immediately, edits the message and arms `[↩️ Undo]` for the last change (60 s). Every page has **≤ 5 toggle rows plus the 3-button pager row**: page 1 has 5, page 2 has 4, page 3 has 4 and page 4 has ≤ 4. Pager labels are ≤ 12 characters (`◀ Labels` = 8, `Labels ▶` = 8).

Both pickers fit the §11.4 budgets: ≤ 600 characters of text, ≤ 6 rows, multi-button labels ≤ 24 characters (the longest is `⬜ Breaking change`, 17), `callback_data` 24 bytes.

## 9. Skill approval card (CR-002 self_improve; `08-skill-approval.svg`)
A staged skill (new or changed) produces an approval card in ✅ Reviews, with the same machinery as a PR card: verbatim parts, delivery before Approve, two taps, single-use message-bound tokens.

Binding: `{v:1, kind:"skill_activate", skill, version, files_sha256 (sorted path→sha256), tools_requested, tier, policy_hash}`. A new staged version, a tier change or a reload invalidates it.

**Mode.** Skill activation is a **local** change (root copies files into Hermes's read-only skills dir); dry-run governs public GitHub writes only, so activation happens the same way in both modes. The card still carries the mode badge, marked as not applicable (S3), so a missing badge never has to be interpreted.

**Machinery.** Skill decisions run through `SkillApprovals` (§12.4 delta 4), with its own `confirm_text()`. `ApprovalService.confirm_text()` for the four outbound kinds is untouched.
- **Storage.** Decision rows live in the `skill_approvals` table that this CR's migration creates (§14.2), with ids `s_` + 10 Crockford characters (`new_id("s")`). Each row references FEAT-009's `skill_reviews` row by `skill_review_id`, whatever id format FEAT-009 ships. FEAT-009 keeps the content (staged files, hashes, activation result); CR-003 owns the decision lifecycle. So CR-003 depends on no FEAT-009 column beyond the row id.
- **Purposes.** Decisions (single-use): `skill_approve`, `skill_confirm`, `skill_cancel`, `skill_reject`. Views (multi-use, expiring and dying with the skill approval, like delta 3): `skill_diff`, `skill_ask`.
- **Views and replies.** The card and its verbatim parts are recorded as `tg_views` kinds `skill_card` and `skill_part`. A reply to either goes to Hermes in the 💬 Assistant session with `Re: skill <name> v<version>` as quoted context, and is never a review comment (§4.4 rule 5).
- **Refusals.** Approve and Confirm are refused while **paused or killed** (the existing toasts). The binding is recomputed at Approve, at Confirm and immediately before the root copy; a mismatch invalidates with `binding_changed`.
- **Invalidation.**
  - `new_skill_version`;
  - `tier_changed`, from the tier switch (§12.4 delta 9);
  - `config_snapshot_changed`, on every reload;
  - `killed`, for armed and confirmed approvals.

  A dry-run toggle doesn't invalidate a skill approval, because the binding has no mode (S3: n/a).
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
| Paused (and killed) | Control state | Refused with the existing toasts: Approve and Confirm on every card, proposal Start work and its Confirm, and skill Approve and Confirm. Telegraph Publish and Unpublish are refused with "Paused or killed: nothing was published." (§6.8). The dashboard shows `⏸ PAUSED · <reason>` |
| Draining or `💤 DOWN (workers)` (Telegram `/down`) | Lifecycle | The gatekeeper is **up** and answers commands. Approve, Confirm and proposal Start work (incl. its Confirm) are refused with the existing lifecycle toast ("ossbot is draining: nothing was approved. The card stays valid; retry after up.", and the same sentence with "down (workers)"). Skill activation and Telegraph are not blocked, because both are gatekeeper/root actions that need no worker. The dashboard shows `🟡 DRAINING` or `💤 DOWN (workers)` with `[⬆️ Up…]`, the short description stays "🟢 Online", and assistant messages get `🤖 Assistant is stopped (workers down) — /up` |
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
  - **Budget rule.** A card is built from blocks marked `required` or `optional` with a drop priority. When the rendered text would exceed the budget (§11.4), the kit removes optional blocks in priority order (lowest value first) and re-renders; it never cuts inside a required block and never cuts trailing lines. Required on every card: the header, the mode line, the subject/title (or issue) line, the size line and its ⚠️ highlight, and the expiry line.
  - **Each required block is bounded**, so the required set can't exceed the budget. Bounds in escaped characters:

    | Block | Bound |
    |---|---|
    | header including the mode badge | ≤ 200 |
    | subject/title or issue line | ≤ 300 |
    | size line plus its ⚠️ highlight | ≤ 400 |
    | expiry line | ≤ 100 |

    An untrusted part that is longer is cut with "…" inside its bound. The required set is therefore ≤ 1,000 characters, far below the 3,900 budget.
  - **`ViewTooLarge` is still checked, and means a bug.** The caller logs ERROR with the view kind and the block sizes (never the content). It invalidates the approval with reason `render_failed` (§5.12), so `issue_card_tokens` can never mint an Approve for it. In the card's place it enqueues a fixed classic stub **without `approval_id`**, so `on_sent` never treats it as a card: `⚠️ Card too large to render (bug) · <cid> r<rev> · nothing can be approved from it; see /logs <cid>`. Proposal and skill cards follow the same rule with their own services.
  - This replaces the current `cards.render` tail cut (`card[:MESSAGE_MAX-20].rsplit("\n",1)[0] + "\n…"`) for every card kind.
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
- `redact.py` (U14): the one redaction choke point.
  - `redact(text) -> (text, hits)` replaces every secret pattern, gatekeeper secret value and Kiro-key candidate with `[REDACTED]`. `classic.py` and `rich.py` call it on every untrusted text leaf **before** `make_visible`, `defuse_commands` and escaping; `drafts.py`, the answer persistence in `chat/stream.py` and the document builders (`answer.md`, log tails, log `.txt`) call it on their text.
  - `scan(text) -> hits` counts without changing anything. It is used for **verbatim publish bytes**: card body parts, commit-message and comment parts, skill parts, `.patch`, `pr_body.md`, the HTML diff's code, **the PR title wherever it is shown** (the card's `PR title:` line, explorer section 9 and the `📋 Copy title` `copy_text`), and publish text quoted back in a prompt (the current title and body in the S7 Title/Body ForceReply, the current comment in FEAT-005's `edit_comment` prompt). Those bytes must equal what is published or activated, so they are never altered or redacted. Packaging and the edit paths already gate them (next bullet), so a hit is a bug: ERROR (surface and count only, never the match), the approval is invalidated with `render_failed`, and a fixed stub like the `ViewTooLarge` one (`kit.py` above; text `⚠️ Card withheld: it matched a secret pattern (bug) · <cid> r<rev> · see /logs <cid>`, no `approval_id`, no buttons) is sent in place of the card, document or prompt.
  - **One scanner for publish bytes and display.** `gatekeeper/secret_scan.py: SecretScanner` holds the policy patterns, the gatekeeper's own secret values (the U14 list) and the Kiro-key check, and is built once per config snapshot (`AppContext.secret_scanner`). `redact.py` wraps it for display, and `policy.check_text` gains a keyword `secret_values` that every production caller fills with `ctx.secret_scanner.values`: besides its existing checks it reports a gatekeeper secret value as `Violation("gatekeeper_secret", <kind>)` (the surface, never the value). Packaging passes the values for the title, body, commit message and comment text (all `policy.check_text` kinds) and also scans the patch with the scanner (`Violation("gatekeeper_secret", "patch")`). So a title, body, commit message, comment or patch containing `HERMES_API_KEY`, `OSSBOT_TOKEN_PEPPER` or `TELEGRAPH_ACCESS_TOKEN` gets the normal policy-violation path at packaging (coder feedback or redirect, with the code as the reason), and every edit path that runs `policy.check_text` (S7 title and body, body as file, `edit_comment`, comment replace) refuses it with its existing refusal text. A render-time `scan()` hit then really is a bug.
  - A display hit (any `redact` call with `hits > 0`) logs WARNING `redacted {surface, hits}` once per turn or view, never the match, and the text is sent redacted.
  - The redactor is built once at start from the config snapshot and `gatekeeper.env`, and rebuilt on a confirmed `/reload`.

### 12.2 Tokens for kit components ("mint then bind")
Approval and Confirm tokens (PR card, proposal card, skill card) keep the reviewed code path: minted after delivery and attached by `editMessageReplyMarkup` (U3). Every other kit button uses "mint then bind", made crash-safe by the outbox:
1. `send.py` mints the token rows with `tg_message_id = NULL` and writes **one** outbox row carrying the payload, `bind_tokens_json` (the list of those tokens) and `view_json` (the `tg_views` row to create: kind, `cid`, `rev`, `action_id`, `path`, `page`, `state_json`), in one transaction.
2. The worker sends the message.
3. `Outbox.mark_sent(row, result)` runs **one** transaction that:
   - sets `status='sent'` and writes `result_message_id = result.message_id`;
   - runs `UPDATE ui_tokens SET tg_message_id = :mid WHERE token IN (:bind_tokens) AND tg_message_id IS NULL`;
   - inserts or updates the `tg_views` row from `view_json` with `message_id = :mid`. `view_json.kind` was already validated at **enqueue** against the `ViewKind` literal that mirrors the `tg_views.kind` CHECK (§14.2), so this insert can't fail on the CHECK after a successful send and leave the row `pending` to be re-sent every tick;
   - increments `tg_thread_seq` for `send*` methods (§5.3);
   - counts a delivered card part (existing). The UPDATE targets the table that the `approval_id` prefix names: `a_` → `approvals`, `s_` → `skill_approvals`. Proposal cards have no parts. **Part counting happens only here**, never in `on_sent`.

   The existing `on_sent` hook (card message id → enqueue parts → arm) still runs afterwards for card bookkeeping, but nothing the kit needs depends on it, so the crash window between `mark_sent` and `on_sent` can't lose a kit binding or a view. The card-bookkeeping part of that window is closed separately by **`CardPublisher.recover()`** (§12.4 delta 8). Its idempotency comes from the `part:<aid>:<i>` and `arm:<aid>` outbox dedupe keys and from single-transaction steps, **not** from `on_sent` or `set_card_message`/`mark_part_delivered`, which are not keyed by the outbox row.

An unbound token is never accepted, because the handler requires `tg_message_id == callback.message.message_id` and NULL never matches (verified in `check_token`). This halves the API calls per interactive message without weakening the binding.

**Dedupe keys are permanent.** `tg_outbox.dedupe_key` is `UNIQUE` for the life of the database, and `Outbox.enqueue` is `ON CONFLICT(dedupe_key) DO NOTHING`. So a key may be reused only for something that must happen **once ever** (a card, its parts, its first arm). Every edit or send that can repeat for the same approval or view must carry a per-occurrence component in its key: the disarm edit is `disarm:<aid>:<armed_at>`, and a re-rendered S1 carries the triggering comment id. The full list of key formats is build criterion BA-3 (§15.1).

**`approval_id` on kit tokens.** Every non-approval kit token is minted with **`approval_id = NULL`**, with `cid` set and with `arg` containing `{"cid": …, "rev": …, "action_id": …}` plus its own fields. The exceptions are proposal kit tokens (`{cid, version}`, §5.15) and `stale_show_current`, which resolves by contribution (§5.12).
- **Why `action_id`.** One contribution can have several open approvals at once: a live `issue_claim` beside the `open_pr`, or a `push_update` beside a drafted `comment` during watch. `comment` and `issue_claim` payloads have no `rev`, so `(cid, rev)` alone is ambiguous. `action_id` is stable across `reissue()`, which creates a new approval for the **same** outbound action, and it changes on a new rev, because `packaging.py` cancels the old `open_pr`/`push_update` actions and inserts new ones. `rev` stays in `arg` because drafts are keyed by `(cid, rev)` (§5.5). Views store the same `action_id` in `tg_views.action_id`, and routing rule 5 resolves through it.
- **Resolution at tap time** (`telegram/review/resolve.py`, one function for every kit handler):
  ```sql
  SELECT a.id, a.status FROM approvals a
   WHERE a.action_id = :action_id AND a.status IN ('pending','armed')
   ORDER BY a.created_at DESC LIMIT 1;
  -- when no row: SELECT status FROM outbound_actions WHERE id = :action_id;
  ```
- **Outcome by purpose class.** A view with `tg_views.stale = 1` always refuses with "Superseded…".

  | Purpose class | Open approval found | No open approval; action `approved`, `executing`, `done` or `dry_run_done` | No open approval; anything else |
  |---|---|---|---|
  | **Navigation** (`nav`, `file_pick`, `files`/`explore`/`interdiff` opened from views) | works | works **read-only** | "Superseded: content or mode changed since this card; review the new card." |
  | **Comment and send** (`comment_hunk`, `comment_line_pick`, `review_send`, `review_send_now`, `review_discard`, `comment_undo`, `use_as_comment`) | works | "Submitted: comment on the PR instead." (`dry_run_done`: "Dry run done: this rev was not published.") | "Superseded…" |
  | **Re-send** (`show_card`, `queue_open`; proposal items resolve by `(cid, version)` and use `AppContext.reissue_proposal`) | `AppContext.reissue(cid, actor, keep_expiry=True)`, subject to the eligibility predicate and the confirmed-sibling refusal (§12.4.3) | "Already submitted · PR #N" | expired and still eligible: "Expired: tap 🔁 Retry on the card for a fresh approval."; expired and ineligible: "Nothing to re-send (state X)"; otherwise "Superseded…" |
  | **`stale_retry`** | n/a (the card is stale) | n/a | runs `AppContext.reissue(cid, actor, keep_expiry=True)` when the system is running and the action is eligible (§12.4.3); else "Nothing to re-send (state X)" |
  | **`stale_show_current`** (arg `{cid}`, `{cid, gate: 1}` or `{skill}`; no `action_id`) | resolved **by contribution** (or skill), not by action: the §5.12 query finds any `pending`/`armed` approval of the cid → `AppContext.reissue`; else the newest open proposal approval → `AppContext.reissue_proposal`; skills → `AppContext.reissue_skill(skill, …)` | n/a | "No open card for c_… (state X)" |
  | **Comment replace** (`comment_replace`, `comment_replace_cancel`; §4.4 rule 5) | Replace runs FEAT-005's `edit_comment` path when the action is eligible; Cancel edits the panel | "Submitted: …" (as comment and send) | "Superseded…" |

- **Read-only explorers.** Tokens with `action_id: null` (the explorer from a rescope or ceiling-hit card, §5.17) resolve nothing. They navigate the `(cid, rev)` derived artifacts only and carry no comment or re-send buttons.
- **Survival.** `_kill_tokens(approval_ids)` only touches rows with that `approval_id`. So a Cancel, a `reissue()` or an invalidation never kills explorer navigation, file views, comment prompts, draft-review buttons or Undo. Only the card's own tokens (`card_purposes(...)`, `confirm`, `cancel`) carry `approval_id`.

Token purposes added (approval_id NULL unless noted):
- `nav`: multi-use, 24 h, arg `{cid, rev, action_id, view, section|page|path}` (proposal Details: `{cid, version, section}`).
- **Card view purposes** `explore`, `files`, `interdiff` and `edit_text_pick`, per kind (§12.4 delta 3). `issue_card_tokens` mints them with the card's `approval_id` and **`single_use=0`**. They expire with the approval (`expires_at` = the approval's) and die with it through `_kill_tokens`. Their handlers call `check_token` (never `use_token`) and only open views or the picker, so `🧭 Explore` works every time it is tapped, and the late mirror row 4 (§5.14) re-uses them.
- Skill card view purposes `skill_diff` and `skill_ask` follow the same rule, with the `s_…` id (§9).
- `comment_hunk`, `comment_line_pick`, `review_send`, `review_send_now`, `review_discard`, `comment_undo`, `file_pick`.
- `body_file`.
- `show_card`, `stale_retry`, `stale_show_current` (§5.12), `queue_open`, `queue_sort`, `picker`, `confirm_cmd` (existing pattern), `danger_second_tap` (10 s).
- `comment_replace` (single-use, 15 min, arg `{cid, action_id, text}`) and `comment_replace_cancel` (§4.4 rule 5).
- `chat_followup`, `chat_retry`, `chat_stop_fallback`, `use_as_comment`.
- `quiet_set`, `notify_toggle`, `digest_set`, `settings_set`.
- `size_set`, `size_pref_toggle`, `size_repo_pick`, `filters_toggle`, `filters_page`, `tune_undo` (§8.4).
- `work_on_link`, `work_cancel`, `link_dismiss` (§5.19).
- `proposal_skip`, `proposal_adjust`, `proposal_smaller`, `proposal_never`, `proposal_never_confirm`, `proposal_later`, `proposal_details`, `proposal_ask_first`, `proposal_undo`, `ceiling_recheck`, `item_drop`, `item_drop_confirm` (§5.15–§5.18).
- `feature_action`.
- `article_publish` (24 h), `article_confirm` (single-use, 5 min), `article_unpublish` (§6.8).
- Decision tokens of the separate services, each carrying its own approval id:
  - `proposal_start`, `proposal_confirm`, `proposal_cancel`: `ProposalApprovals`, ids `p_…` in `proposal_approvals`;
  - `skill_approve`, `skill_confirm`, `skill_cancel`, `skill_reject`: `SkillApprovals`, ids `s_…` in `skill_approvals`.

  All ids use `ossbot.common.ids.new_id(<one letter>)`, the existing `^[a-z]_[0-9A-HJKMNP-TV-Z]{10}$` format: `a_` approvals, `p_` proposal approvals, `s_` skill approvals. Because the prefixes differ, a token can never be resolved by the wrong service. `DecisionServices.for_id(approval_id)` dispatches on the prefix, and each service also checks the purpose set it accepts.
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
The review UI needs nine bounded changes in `approvals.py`, `outbox.py`, `CardPublisher`, `AppContext` (`context.py`: the `reissue`, `reissue_proposal` and `reissue_skill` wrappers and the `card_recover` hook), `state_machine.sweep_expired` and the existing invalidation call sites. Nothing else in the token or binding core changes. These stay as they are: the token format and checks (`TOKEN_RE`, `check_token`, `use_token`), `compute_binding`, `binding_hash`, `_recheck_binding`, `confirm_text` for the outbound kinds, `approve`, `confirm`, `consume` and `_kill_tokens`.

| # | Delta | Where | Why | Test |
|---|---|---|---|---|
| 1 | **Non-approval tokens have `approval_id = NULL`** and carry `{cid, rev, action_id}` in `arg`. Their handlers resolve the current approval at tap time by `action_id`, with the purpose-class outcomes of §12.2. No code change in `_kill_tokens`; the delta is a rule for every new mint site, enforced by a test | `telegram/ui/send.py`, `telegram/review/resolve.py`, `review/*` | Cancel, `reissue()` and invalidations kill every token with the approval's id, and review navigation must survive them. `(cid, rev)` is ambiguous when several actions are open | `test_kit_tokens_survive.py`: arm → Cancel → explorer `nav`, file `nav`, `review_send` and `comment_undo` still work; `reissue()` → same; invalidation (`new_package_rev`) → "Superseded" via `tg_views.stale`. `test_kit_token_resolution.py`: `issue_claim` + `open_pr`, and `push_update` + `comment`, open together → each view resolves its own action; after Confirm, navigation is read-only and comments answer "Submitted…" |
| 2 | **`disarm_expired()`** (new) on all three decision services, called by the housekeeping tick (≤ 10 s). For `armed` with `armed_at <= now − confirm_ttl_seconds`: set `pending` and `armed_at = NULL`, mark the unused `confirm`/`cancel` tokens of that approval used, `rotate_card_tokens`, edit the card back to S1, and audit `approval_disarmed {reason:"confirm_expired"}` | `approvals.py`, `proposal_approvals.py`, `skill_approvals.py`, `app.py: housekeeping_once`, `CardPublisher` | No path exists today: a timed-out Confirm leaves dead buttons and an approval nobody can re-arm (`issue_card_tokens` refuses non-`pending`) | AC 29 |
| 3 | **Card purposes per kind** (§12.4.1). `DECISION_PURPOSES` single-use; view purposes multi-use, expiring and dying with the approval; `card_purposes(kind, has_interdiff)` replaces the single `CARD_PURPOSES` tuple | `approvals.issue_card_tokens`, `keyboards.card_rows` | Single-use view buttons died after one tap, and one tuple did not fit `push_update`, `comment` or `issue_claim` | `test_card_purposes.py`: golden purpose set **and** `single_use` flag per kind and rev; AC 44 |
| 4 | **Separate decision services** (§12.4.2): `ProposalApprovals` (table `proposal_approvals`, ids `p_…`) and `SkillApprovals` (table `skill_approvals`, ids `s_…`), each with its own binding and `confirm_text`, plus the `DecisionService` protocol that all three implement. Both reuse `mint_token`/`use_token`/`check_token` and the U3 card path. Neither calls `ApprovalService.confirm_text`, whose dict lookup over the four outbound kinds would raise `KeyError` | `gatekeeper/decisions.py`, `proposal_approvals.py`, `skill_approvals.py`, `common/ids.py`, migration (§14.2) | Skills and proposals are not outbound actions (`approvals.action_id` references `outbound_actions`) | Each service's two-tap golden tests; `confirm_text` of `ApprovalService` unchanged (existing tests); AC 47 |
| 5 | **One `/approve` semantics: the code's `reissue()` wins.** `/approve <cid>`, `⬇️ Show card`, `⬇️ Show current`, `/queue → Open` and S12 `🔁 Retry` all call the one wrapper `AppContext.reissue(cid, actor, keep_expiry=True)` (§12.4.3): every open approval of an eligible action of the contribution is invalidated with `reissued`, a fresh approval is created per eligible action with **its own part count**, and every new card is published and its parts re-sent. So all open cards of the contribution are re-sent (toast `Re-sent N cards`), and an armed sibling comes back at S1. DESIGN §14.5 ("same approval, armed → pending") is amended to match. Draft comments are keyed by `(cid, rev)` (§5.5), so they survive | `approvals.reissue` (changed by delta 7 only), `context.AppContext.reissue` (new wrapper), DESIGN §14.5 text | `reissue()` is already reviewed (N4) and leaves no armed state behind; keeping the same approval would need new code to reset delivery counters | `test_reissue_keeps_draft.py`: 3 draft comments → Show card → the draft review still lists 3 and Send works. `test_reissue_siblings.py`: an armed `issue_claim` + a pending `open_pr` → Show card on the `open_pr` explorer → both re-sent, toast `Re-sent 2 cards`, the claim at S1 |
| 6 | **`Outbox.mark_sent` binds tokens, writes `tg_views`, increments `tg_thread_seq` and counts card parts in its transaction**; part counting picks the table by id prefix (`a_` approvals, `s_` skill approvals) (§12.2). Plus the new `tg_outbox` columns (§14.2) | `outbox.py` | Closes the kit's crash window between `mark_sent` and `on_sent`; skill parts must count too | `test_outbox_bind_atomic.py`: crash injected after `mark_sent` → tokens bound and view present after restart; a skill part advances `skill_approvals.body_parts_delivered` |
| 7 | **Re-sends keep the expiry, carry per-action part counts and touch only eligible actions** (§12.4.3): `create(…, expires_at=None)`; `reissue(…, part_count, keep_expiry)` with the eligibility predicate and the confirmed-sibling refusal; the `AppContext.reissue` wrapper that publishes; the `sweep_expired` reference becomes the newest approval's window start, and its move to `EXPIRED` cancels the contribution's `pending_approval` actions | `approvals.create`, `approvals.reissue`, `context.py`, `proposal_approvals.reissue`, `skill_approvals.reissue`, `state_machine.sweep_expired` | Navigation re-sends otherwise restart the 72 h TTL, reorder the queue and postpone the contribution-expiry sweep. One `body_parts_total=1` for every action let a re-sent `open_pr` card arm before its verbatim parts arrived (ADR-0004). Re-sends could revive cards that can't be submitted, or kill a sibling mid-submission | `test_reissue_keeps_expiry.py`, `test_reissue_part_counts.py`, `test_reissue_eligibility.py` (§12.4.3); AC 46, AC 57, AC 58 |
| 8 | **`CardPublisher.recover()`** and single-transaction `_card_sent`/`_maybe_arm` (§12.4.4) | `telegram/bot.py`, `TelegramFrontend._connect`, `app.py: housekeeping_once` | Nothing re-runs `on_sent` for rows already `sent`, so a crash after a card's `mark_sent` left a card with no parts and no Approve | `test_card_recover.py`; AC 31 |
| 9 | **Invalidation fan-out**: every existing invalidation and expiry call site calls all three decision services, and the tier switch invalidates skill approvals (§12.4.2) | `config_snapshot.py` (both reload branches), `control.py` (the four kill paths), `app.py: housekeeping_once`, the FEAT-009 tier switch | Today those call sites reach only `ApprovalService`, so a reload or kill would leave proposal and skill approvals valid | `test_decision_fanout.py`; AC 47 |

#### 12.4.1 Delta 3: card purposes per action kind

| Kind / rev | Single-use decision purposes | Multi-use view purposes (`single_use=0`, `expires_at` = the approval's, killed by `_kill_tokens` like today) | Keyboard (§5.1) |
|---|---|---|---|
| `open_pr` r1 (or no interdiff) | `approve`, `reject`, `reject_reason`, `changes` | `explore`, `files`, `edit_text_pick` | `[✅ Approve & Submit] [❌ Reject]` / `[🧭 Explore] [🧾 Files (n)]` / `[✏️ Request changes] [📝 Edit text]` |
| `open_pr` rN with an interdiff | same | `explore`, `interdiff`, `edit_text_pick` | row 2 `[🧭 Explore] [🔀 Interdiff]` |
| `push_update` | same | `explore`, `files` (the incremental patch, DESIGN §14.4a) | `[✅ Approve & Push] [❌ Reject fix]` / `[🧭 Explore] [🧾 Files (n)]` / `[✏️ Request changes]` |
| `comment`, `issue_claim` | `approve`, `reject`, `reject_reason` | `edit_text_pick` (opens FEAT-005's `edit_comment` ForceReply directly; no picker) | `[✅ Approve & Post] [❌ Reject]` / `[📝 Edit text]` |

```python
DECISION_PURPOSES = ("approve", "reject", "reject_reason", "changes")

def card_purposes(kind: OutboundKind, has_interdiff: bool) -> tuple[str, ...]:
    if kind == "open_pr":
        return (*DECISION_PURPOSES, "explore", "interdiff" if has_interdiff else "files", "edit_text_pick")
    if kind == "push_update":
        return (*DECISION_PURPOSES, "explore", "files")
    return ("approve", "reject", "reject_reason", "edit_text_pick")      # comment, issue_claim

# issue_card_tokens(approval_id): kind from outbound_actions; has_interdiff from
# derived/<cid>/<rev>/interdiff.json (present and not "unavailable"); every purpose p is minted with
# single_use = (p in DECISION_PURPOSES) and expires_at = the approval's expires_at.
```
- `edit_title`, `edit_body` and `clear_edits` move behind the S7 picker as kit tokens (`open_pr` only).
- `rotate_card_tokens` is unchanged: it kills every unused token of the approval, the view tokens included, and re-mints the set through `issue_card_tokens`.
- View handlers call `check_token`, never `use_token`.

#### 12.4.2 Deltas 4 and 9: decision services and the invalidation fan-out
`gatekeeper/decisions.py`:
```python
class DecisionService(Protocol):
    id_prefix: str                                    # "a" approvals, "p" proposal approvals, "s" skill approvals
    def invalidate_all_open(self, reason: str, actor: str = "system") -> list[Invalidated]: ...
    def invalidate_armed_confirmed(self, reason: str, actor: str = "system") -> list[Invalidated]: ...
    def expire_due(self) -> list[str]: ...
    def disarm_expired(self) -> list[str]: ...
    # card plumbing used by CardPublisher for a_, p_ and s_ cards alike (§12.4.4)
    def set_card_message(self, approval_id: str, message_id: int) -> None: ...
    def issue_card_tokens(self, approval_id: str) -> dict[str, str]: ...
    def card_view(self, approval_id: str) -> View: ...                 # the kit renderer for this kind
    def card_rows(self, approval_id: str, tokens: dict[str, str]) -> Rows: ...
    def part_count(self, approval_id: str) -> int: ...                 # proposals: 0

@dataclass(frozen=True)
class DecisionServices:                               # AppContext.decisions
    approvals: ApprovalService
    proposals: ProposalApprovals
    skills: SkillApprovals
    def all(self) -> tuple[DecisionService, DecisionService, DecisionService]: ...
    def for_id(self, approval_id: str) -> DecisionService: ...   # by prefix; unknown → ApprovalError("unknown_approval")
```
- **Existing methods.** `ApprovalService` already has the first three methods, unchanged, plus delta 2's `disarm_expired`; its card-plumbing methods are thin adapters over today's `set_card_message`, `issue_card_tokens`, `cards.view_for_approval`, `keyboards.card_rows` and `cards.part_count`. **`Invalidated` is widened** to `Invalidated(approval_id: str, action_id: str | None, cid: str | None)`: the proposal service returns `action_id=None`, and the skill service returns `action_id=None` and `cid=None` (skills have no contribution). This passes the strict mypy override for `ossbot.gatekeeper.*`. `AppContext.reissue_cards`, which re-creates outbound-action cards after a toggle or reload, skips rows whose `action_id` is `None` (BA-5). After a reload, `ProposalApprovals.reissue_invalidated(...)` and `SkillApprovals.reissue_invalidated(...)` re-create their cards the same way, with `keep_expiry=True` and only while the proposal is open or the skill is still staged and within the tier. They are called only from `AppContext.reissue_cards`, which publishes each returned id through `on_approval_created` after the commit, exactly as it does for PR cards (BA-17).
- **Each service** offers `create`, `issue_card_tokens`, `approve`, `confirm`, `cancel`, `reject`, `consume`, `reissue(…, keep_expiry)`, `confirm_text` and the protocol. For proposals, Skip, Later and Never (after its confirm) are `ProposalApprovals.reject(approval_id, reason, actor)` with `reason ∈ {skipped, snoozed, never_repo, never_kind}`; the kit buttons only collect the choice, and Undo is §5.15. Ids come from `new_id("p")` and `new_id("s")` (`common/ids.py` gains `new_proposal_approval_id()` and `new_skill_approval_id()`).
- **Refusals and recomputing**, at Approve, at Confirm and immediately before the effect:
  - proposal Start work is refused while paused, killed, draining or `💤 DOWN (workers)`; the effect is enqueueing the plan stage;
  - skill activation is refused while paused or killed; the effect is the root copy;
  - both use the existing toasts (§5.15, §9). The binding is recomputed at all three points.
- **Call sites (delta 9).** These are call-site-only changes. Each replaces `self.approvals.<method>(…)` with a loop over `decisions.all()`, inside the same transaction as today:

  | Call site | Today | After |
  |---|---|---|
  | `config_snapshot.py`, both branches (the startup reconcile and the human-confirmed reload) | `approvals.invalidate_all_open("config_snapshot_changed")` | all three services |
  | `control.py`: the kill path, the resume path that applies a root-written kill before reconcile, the resume-log path and the flag reconcile | `approvals.invalidate_armed_confirmed("killed", …)` | all three services |
  | `control.py` `set_dry_run` | `approvals.invalidate_all_open("dry_run_toggled")` | **unchanged, ApprovalService only**: proposal and skill bindings carry no mode |
  | `app.py: housekeeping_once` | `approvals.expire_due()` | `expire_due()` and `disarm_expired()` on all three, then `ctx.card_recover()`, which runs `CardPublisher.recover()` (delta 8, §12.4.4) |
  | Tier switch (FEAT-009: the `/hermes tier` confirm handler and the `ossctl hermes tier` API route, right after the tier flag is written, in the same transaction as its audit row) | none | `skills.invalidate_all_open("tier_changed")` |

- `REASON_LABELS` gains `tier_changed`, `new_proposal_version`, `new_skill_version` and `render_failed` (§5.12).

#### 12.4.3 Delta 7: re-sends keep the expiry, carry per-action part counts and touch only eligible actions
- `ApprovalService.create(action_id, *, tg_chat_id, body_parts_total, actor="system", expires_at: datetime | None = None)`. A given `expires_at` replaces `now + approval_ttl_hours`; it must be later than `now`, else `ApprovalError("bad_expiry")`.
- `ApprovalService.reissue(cid, *, tg_chat_id, actor, part_count: Callable[[str], int], keep_expiry: bool = False) -> list[str]`. The single `body_parts_total=1` integer is **removed**: today it is passed to `create()` for every action of the contribution, so a re-created `open_pr` card (≥ 2 parts: body parts plus the commit message) would get Approve after its first part, and any retry or backoff on a later part would put the arm edit ahead of it (ADR-0004, §5.1, AC 5). Now each action gets its own count: `create(action_id, …, body_parts_total=part_count(action_id), …)`. Inside its one transaction:
  1. **Confirmed-sibling refusal.** If any approval of the cid is `confirmed`, it raises `ApprovalError("submitting")` before changing anything. Callers answer `Submitting… try again in a moment`. A re-send never invalidates a `confirmed` approval.
  2. **Eligibility.** It selects the contribution's actions that pass `reissue_eligible(action, contribution_state)`. Ineligible actions are skipped, and their approvals are left as they are. If none is eligible, it raises `ApprovalError("nothing_to_resend")`, and callers answer `Nothing to re-send (state <state>)`.
  3. **Expiry.** **Before** invalidating, it reads the `expires_at` of each eligible action's newest approval.
     - With `keep_expiry=True`, the new approval inherits that value when it is later than `now + confirm_ttl_seconds`. Otherwise (expired, nearly expired, or no earlier approval) it gets `now + approval_ttl_hours`.
     - With `False`, the default and today's behaviour, every new approval gets `now + approval_ttl_hours`.
  4. It invalidates the `pending` and `armed` approvals of the eligible actions with `reissued` (never `confirmed` ones; step 1 already refused those), then creates one approval per eligible action and returns the new ids.
- **The eligibility predicate** (`approvals.reissue_eligible`, one function used by `reissue`, `stale_retry`, typed `/approve`, Show current and `comment_replace`):

  | Action kind | Eligible when |
  |---|---|
  | `open_pr`, `push_update` | `outbound_actions.status = 'pending_approval'` **and** the contribution state is `AWAITING_APPROVAL` |
  | `comment`, `issue_claim` | `status = 'pending_approval'` **and** the state is one the kind lives in: `VETTED`, `PACKAGING` (a claim, as in `handlers._reject`), `WATCHING` (a drafted reply), or `AWAITING_APPROVAL` (a claim or reply open beside an `open_pr`/`push_update` card, §5.2) |

  The `'approved'` value in today's `reissue()` selection is dropped: no code ever writes it. So a typed `/approve` during S5 (`IMPLEMENTING`, where the r2 action is still `pending_approval` until the next `/package`) and a Retry after the `EXPIRED` sweep create nothing.
- **`sweep_expired` cancels.** The transition to `EXPIRED` cancels the contribution's `pending_approval` actions in the same transaction, as it already does for `push_update` on the move to `WATCHING`. Their S12 cards are re-rendered without `🔁 Retry` (§5.12).
- **The wrapper every entry point uses** (`context.py`). It is the only re-send caller, used by Show card, Show current, `/queue → Open`, typed `/approve` and Retry:
  ```python
  def reissue(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]:
      ids = self.approvals.reissue(cid, tg_chat_id=self.telegram_chat_id, actor=actor, keep_expiry=keep_expiry,
                                   part_count=lambda aid: cards.part_count(self.conn, aid))
      for aid in ids:                      # after reissue()'s transaction has committed
          self.on_approval_created(aid)    # enqueues the card; parts follow; Approve after all parts
      return ids
  ```
  `ProposalApprovals.reissue(cid, *, keep_expiry)` follows the same rules with no parts: a proposal card is armable once the card itself is sent. Its eligibility is the contribution in `PROPOSED` with no `confirmed` proposal approval. `SkillApprovals.reissue(skill, *, keep_expiry)` passes the skill card's own part count (its files and diff parts) for every re-created approval. Its eligibility is the skill still staged and within the current tier, with no `confirmed` skill approval. **The services never publish.** Each has one `AppContext` wrapper, the only caller of its `reissue` (BA-17), which publishes after the commit exactly like `reissue` above:
  ```python
  def reissue_proposal(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]:
      ids = self.decisions.proposals.reissue(cid, actor=actor, keep_expiry=keep_expiry)
      for pid in ids:                      # after the service's transaction has committed
          self.on_approval_created(pid)    # proposal cards have no parts: armable once sent
      return ids

  def reissue_skill(self, skill: str, actor: str, *, keep_expiry: bool) -> list[str]:
      ids = self.decisions.skills.reissue(skill, actor=actor, keep_expiry=keep_expiry)
      for sid in ids:
          self.on_approval_created(sid)    # parts follow; Approve after all of them
      return ids
  ```
  Their callers: Show current (§5.12), `/queue → Open` (§5.13), proposal Undo (§5.15), `/skills → Open card` (§9). A failed `on_approval_created` after the commit is healed by `CardPublisher.recover()` step 0 within one tick, as for PR cards (§12.4.4).
- **Callers.** Show card, Show current, `/queue → Open`, typed `/approve <cid>` and S12 `🔁 Retry` all pass `keep_expiry=True`. Show card and `/queue → Open` never start from an expired action, because resolution refuses first (§12.2). So only Retry and a typed `/approve` revive the approval you tapped, and both are explicit requests. Because `reissue()` covers the whole contribution, an expired **sibling** of the tapped action is re-created too. It gets a fresh window (the per-action rule), binds the current state like any new approval, and is counted in the `Re-sent N cards` toast. That is stated, not silent. The dry-run-toggle and reload re-issue (`AppContext.reissue_cards`, which calls `create` directly) is unchanged and keeps today's fresh TTL, because the bound mode or policy changed. The proposal and skill re-sends (`reissue_proposal`, `reissue_skill`) follow the same `keep_expiry=True` rule as their callers above. Their reload re-issue (`reissue_invalidated`, §12.4.2) also keeps the expiry: a proposal or skill binding carries no mode, and a reload must not extend its window.
- **Sweep reference.** `state_machine.sweep_expired` uses the newest approval's **window start** (`expires_at − approval_ttl_hours`, newest by `expires_at`) instead of its `created_at`. For an approval created with a fresh TTL the two are equal. An inherited approval keeps its predecessor's window start, so navigation can't postpone the contribution-expiry sweep. A TTL change between two re-sends shifts the reference by the difference; that is accepted.
- **Queue order** (`expires_at` is its second key) therefore doesn't change on navigation.
- **`test_reissue_keeps_expiry.py`:**
  - approval created at T (expires at T+72 h); Show card at T+70 h → the new approval expires at T+72 h, `/queue` order is unchanged and the sweep reference stays T;
  - Show card at T+71 h 57 m (under 5 min left) → T+71 h 57 m + 72 h;
  - Retry after expiry → now + 72 h;
  - a dry-run toggle → now + 72 h (unchanged behaviour);
  - an open sibling re-sent by a Retry keeps its own expiry.
- **`test_reissue_part_counts.py`:**
  - an `issue_claim` (1 part) and an `open_pr` (3 parts) open together → Show card → the new approvals have `body_parts_total` 1 and 3;
  - the fake Bot API fails the `open_pr`'s part 2 once (429, then success) → no Approve token for the `open_pr` exists until part 2 is delivered, and the claim is armed after its one part;
  - each new id is published through `on_approval_created` exactly once, after the commit;
  - `SkillApprovals.reissue` uses the skill's part count, and `ProposalApprovals.reissue` arms with no parts;
  - `AppContext.reissue_proposal` and `AppContext.reissue_skill` publish each new id through `on_approval_created` exactly once, after the commit, and neither service calls the hook itself (a counting fake hook sees no call from inside the service's transaction).
- **`test_reissue_eligibility.py`:**
  - `IMPLEMENTING` with the r2 action still `pending_approval` → typed `/approve`, Retry and Show current create nothing and answer `Nothing to re-send (state IMPLEMENTING)`;
  - after `sweep_expired` → `EXPIRED`, the actions are `cancelled`, and Retry answers `Nothing to re-send (state EXPIRED)`;
  - a `confirmed` sibling (submit task paused between Confirm and `consume()`) → Show card on the other card is refused with `Submitting… try again in a moment`, and the confirmed approval is untouched and submits normally;
  - a claim in `VETTED`, a reply in `WATCHING`, and a claim beside an `open_pr` in `AWAITING_APPROVAL` are all eligible.

#### 12.4.4 Delta 8: card-delivery crash recovery
- `CardPublisher._card_sent(aid, mid)` runs `set_card_message` and every part enqueue in **one** `db.transaction` (nested use joins the outer transaction). `CardPublisher._maybe_arm(aid)` runs `issue_card_tokens` and the `arm:<aid>` enqueue in one transaction. A crash therefore leaves each step either fully done or not started.
- **One publisher for all three card families.** Today `CardPublisher` is PR-specific: it calls `ctx.approvals`, `cards.view_for_approval` and `keyboards.card_rows(kind)`. It now resolves everything through `svc = ctx.decisions.for_id(aid)`: `svc.set_card_message`, `svc.issue_card_tokens`, `svc.card_view` (the renderer) and `svc.card_rows` (the keyboard, §12.4.2). So `_card_sent`, `_maybe_arm`, `render_s1` and `recover()` work for `a_`, `p_` and `s_` ids alike.
- **Housekeeping reaches it through a hook.** `housekeeping_once(ctx)` receives only `AppContext`, while `CardPublisher` lives in `TelegramFrontend`. So `AppContext` gains `card_recover: Callable[[], None]`. It defaults to a no-op, and `TelegramFrontend.start` sets it to `publisher.recover`, the same way it sets `on_approval_created` today.
- **`CardPublisher.recover()`** runs once in `TelegramFrontend._connect`, right after `application.initialize()` and before polling starts, and then in every `housekeeping_once` tick (through `ctx.card_recover()`). It covers the `a_` and `s_` cards (with parts) and the `p_` cards (no parts):
  0. **Approval created, card never enqueued.** For every `pending` approval with `tg_message_id IS NULL` and **no** `tg_outbox` row `card:<aid>` (a failed `on_approval_created` after a re-send's commit), run `on_approval_created(aid)`. The `card:<aid>` dedupe key makes a repeat a no-op.
  1. **Card sent, message id lost.** For every `tg_outbox` row with `status='sent' AND dedupe_key LIKE 'card:%' AND result_message_id IS NOT NULL` whose approval is `pending` with `tg_message_id IS NULL`, run `_card_sent(aid, result_message_id)`. The parts' existing dedupe keys `part:<aid>:<i>` make a repeated enqueue a no-op.
  2. **Parts delivered, never armed.** For every `pending` approval with `tg_message_id IS NOT NULL` and `body_parts_delivered = body_parts_total` (proposals: no part condition), and with **no** `tg_outbox` row `arm:<aid>`, run `_maybe_arm(aid)`. Tokens and the arm row are written in one transaction, so "no arm row" means "no card tokens", and Approve is minted exactly once.
  - Card rows sent before this CR's migration have no `result_message_id`. Recover skips them with one WARNING each; those approvals are reachable through `/queue → Open`.
- **Part counting stays only in `Outbox.mark_sent`.** Recover never counts parts, and `mark_part_delivered` keeps having no production caller. So `body_parts_delivered` can't exceed the parts actually sent.
- **`test_card_recover.py`** injects a crash at four points: after a card row's `mark_sent`, after `_card_sent`, after the last part's `mark_sent`, and inside `_maybe_arm`. After restart, every part is sent once, Approve is minted once, and `body_parts_delivered ≤` the count of sent part rows. The same test runs for a `p_` and an `s_` card, and once through `housekeeping_once(ctx)` alone, to prove `ctx.card_recover` reaches the publisher.

### 12.5 Update handler budget (normative)
PTB processes **one update at a time** (`concurrent_updates` unset; `max_concurrent_updates=1` in the installed 22.8). This CR keeps that on purpose: §5.5's race argument and the reviewed Approve/Confirm path rely on it (ADR-0020). The cost is that a handler that awaits slow I/O holds the only update slot (design review round 4 checked the installed 22.8: `__update_fetcher` awaits each update inline when `max_concurrent_updates == 1`). A Hermes answer can stream for minutes (120 s idle timeout), and during that time `/kill`, `/pause`, Approve, Confirm and `stopped_message_generation` would all wait. Stop could never stop the stream it targets. So every handler follows this budget:

1. **What a handler may do inline.**
   - SQLite transactions and outbox enqueues. A kit reply through `telegram.ui.send` is an enqueue, not an API call.
   - Reading gatekeeper-local data: the config snapshot and `derived/<cid>/<rev>/` pages for explorer and file views.
   - **At most one direct Bot API call besides `answerCallbackQuery`**: the U3 card edit (Approve, Cancel, the Confirm-panel refresh of §5.5), or one direct `editMessageText` of the tapped view. PTB's request timeouts bound it.
2. **Everything else is spawned** as a tracked task with `Handlers.spawn(coro, kind, *, session_key=None, on_done=None) -> asyncio.Task`. It generalizes today's `_run_submit` pattern: `asyncio.create_task`, `self.tasks.add`, plus an index by kind and the session map (item 3). A task that raises is logged at ERROR with its kind and ids (never content), and the user gets the §14.5 text for that operation.

   **`spawn` owns cleanup.** Cancelling a task that has not started yet throws `CancelledError` into the coroutine before its body runs, so its `try/finally` never executes. That is the normal case when a message and a `/kill` arrive in one `getUpdates` batch, because PTB processes them back to back. So cleanup never lives only in the coroutine. `spawn` registers one `add_done_callback`, which asyncio runs for every outcome (result, exception, cancel before or after start). In order, it:
   - removes the task from `self.tasks` and from the kind index;
   - removes the session-map entry when `session_key` is set and the entry is **this** task (a newer turn's entry is never touched);
   - calls `on_done(task)` inside `try/except`, logging ERROR on failure.

   The callback is synchronous and idempotent: each step checks before it changes. A coroutine may keep its own `finally` for the normal path, but correctness never depends on it. `on_done` per kind:
   - `chat_turn`: `finalize_turn(turn_id)` closes the `chat_turns` row in one transaction (`stopped` on cancel, `failed` on exception, untouched when the body already set `done`; `finished_at` set if NULL; §6.1 step 0);
   - `media`: drops the album buffer of its `media_group_id`, so a later album with a new id starts clean;
   - `telegraph`: the task keeps a state object whose `request_started` flag it sets to `True` immediately before entering `asyncio.shield`. When the task was cancelled and `request_started` is false, `on_done` enqueues the edit of the confirm panel to `Not published (stopped).` (an outbox enqueue is a synchronous SQLite write, so it is allowed in the callback). When it is true, `on_done` only sets `outer_cancelled = True` on the state object. The shielded section always ends with the announcement, after its `telegraph_pages` write: the normal §6.8 step 4 reply, or, when `outer_cancelled` is already set, the panel edit `📖 Published (after stop) · public link · see /articles` (Unpublish: `🗑 Unpublished (after stop)`). Both are outbox enqueues, so they are safe while killed, and whichever way the cancel races the write, exactly one announcement is sent (§6.8 step 4);
   - the others: none.

   | Kind | Work | Cancelled by `/kill` |
   |---|---|---|
   | `chat_turn` | a Hermes turn, from relay to final message (§6.1) | yes: `chat_turns.status='stopped'` (set by `on_done`, also when cancelled before start), no partial sent |
   | `media` | album collection, `getFile`, download, metadata stripping (§6.5) | yes (the album buffer is dropped by `on_done`) |
   | `telegraph` | publish or unpublish, including the scans and the retries (§6.8) | only **before** the `createPage`/`editPage` request starts. From then on, the request, the `telegraph_pages` write and the announcement (`📖 Published` or `📖 Published (after stop)`) run under `asyncio.shield`, so a page that exists is always recorded and announced |
   | `github_read` | `/work` and link-card facts (§5.19) | yes |
   | `render` | the HTML diff (`review_html.py`) and `sendDocument` of `.patch`, `.html`, `pr_body.md` and log files (§5.4) | yes |
   | `submit` | the existing `_run_submit` | **no**: Phase 1 kill semantics are unchanged |
   | `command` | the effect of a registry command or confirm handler that awaits I/O beyond SQLite and local reads: the `/hermes` surface probe, root-helper and lifecycle requests (`/up`, `/down`, `/reload`, `/egress` proxy re-render), the `/model` writer (§8.1). It edits the command's reply to the outcome when done | **no**: those effects keep their Phase 1 kill rules |
3. **One turn per session.** `Handlers` keeps a **session map** `dict[str, asyncio.Task]` (`session_key` → the running `chat_turn` task). It replaces the per-session `asyncio.Lock` of revision 4: nothing ever waits on it, and an entry owned by a task can be removed by that task's done callback without the release-from-another-task and stale-release problems of a lock. A session is busy while it has an entry. The handler checks it and `spawn` registers the new task with no `await` in between, so two updates can't both pass the check. A message or follow-up tap for a busy session gets `⏳ Still answering — tap ⏹ Stop or wait` and is not relayed (§6.1 step 0).
4. **Kill, Stop and shutdown.**
   - `/kill`, typed or by the dashboard's second tap, first commits the existing kill transaction, then calls `Handlers.cancel(kinds={"chat_turn","media","telegraph","github_read","render"})`. `submit` and `command` are not cancelled.
   - `stopped_message_generation` sets the matching turn's stop event (§6.1 step 5).
   - `Handlers.drain()` (shutdown) cancels the same kinds, then awaits every task, `submit` included, as today.
5. **The one inline exception: the Confirm live preflight.** The existing Confirm handler awaits `Submitter.preflight` (GitHub live checks) **before** `confirm()`, so a confirmed approval never exists without passing checks. That order is reviewed Phase 1 behaviour, so it stays inline. It is now wrapped in `asyncio.wait_for(…, 8 s)`, and a timeout counts as the failed check `live_check:timeout`: the existing invalidation path, S12 with `🔁 Retry`. A `/kill` typed during a Confirm therefore waits ≤ 8 s, and then the Phase 1 kill semantics apply to that submission unchanged.
6. **Enforced by measurement.** `Handlers` wraps every handler in a timer with a per-handler threshold: `PREFLIGHT_TIMEOUT_S + 1` (9 s) for the Confirm handler, whose inline preflight item 5 allows, and `HANDLER_SLOW_S` (1 s) for every other handler. Going over logs WARNING `handler_slow {handler, ms, threshold_ms}` (no content), and the fake-Bot-API suite fails on any `handler_slow` event. So a slow GitHub preflight in production is not reported as a budget bug, and any other slow handler still is. AC 59 and AC 67 test the budget end to end (`tests/telegram/test_handlers_nonblocking.py`).
7. **Startup.** Before polling starts, every `chat_turns` row still `streaming` is set to `failed` with `finished_at = now` (no task survives a restart), and the session map starts empty.

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

Python stack, locked: Python 3.12, **python-telegram-bot 22.8** (transport, long polling, dispatch and typed objects up to Bot API 10.0; Bot API 10.1–10.3 through `Bot.do_api_request` with typed pydantic payload models), FastAPI over UDS, httpx, pydantic 2, Jinja2 (HTML diff) and SQLite. The "no new runtime dependency" constraint is lifted, and the library choice was re-made on the merits in ADR-0020: PTB stays (lenient decoding of unknown fields, LGPL-3.0, zero migration, existing fake-Bot-API tests), aiogram and pyTelegramBotAPI are rejected, and **no new runtime dependency is added by this CR** (Pillow included, §6.5). When PTB ships typed 10.1+ support (v23), `rawapi.py` call sites switch to typed methods behind the same kit API; that upgrade is a separate change with its own review.

| Path | Change |
|---|---|
| `src/ossbot/gatekeeper/telegram/ui/{__init__,kit,blocks,classic,rich,sanitize,limits,icons,send,redact}.py` | **New** component kit (§12); `redact.py` is the U14 choke point over `gatekeeper/secret_scan.py` |
| `src/ossbot/gatekeeper/secret_scan.py` | **New**: `SecretScanner` over `common.redact.Redactor` (policy patterns, the gatekeeper's own secret values) plus the Kiro-key hash check (`policy.TOKENISH_RE`, `policy.kiro_key_hashes`) and the load checks of BA-16; one instance per config snapshot on `AppContext.secret_scanner`, used by `redact.py`, `policy.check_text` and packaging (§12.1) |
| `src/ossbot/gatekeeper/policy.py` | `check_text(…, *, secret_values)` also reports gatekeeper secret values as `Violation("gatekeeper_secret", <kind>)`; every production caller passes `ctx.secret_scanner.values` (§12.1, BA-21); nothing else |
| `src/ossbot/gatekeeper/telegram/review/{explorer,files,comments,interdiff_view,status_card,queue,stale,edit_text}.py` | **New** review flow (§5) |
| `src/ossbot/gatekeeper/telegram/{topics,routing,dashboard,notify,digest,drafts,pickers,rawapi}.py` | **New** (§4, §6.1, §7, §8). `rawapi.py`: typed wrappers (pydantic request/response models) for every Bot API 10.1–10.3 method and field the kit uses, over `do_api_request` (ADR-0020) |
| `src/ossbot/gatekeeper/telegram/{proposal_cards,work_links,size_pickers}.py` | **New** (CR-004 presentation: §5.15–§5.19, §8.4) |
| `src/ossbot/gatekeeper/{decisions,proposal_approvals,skill_approvals,discovery_prefs}.py` | **New**: the `DecisionService` protocol and `DecisionServices` (§12.4.2); `ProposalApprovals` and `SkillApprovals` (§12.4 delta 4) over the new `proposal_approvals` and `skill_approvals` tables; `DiscoveryPrefs` (§8.4). The proposal and work-request content model itself is CR-004's; the staged-skill content is FEAT-009's `skill_reviews` |
| `src/ossbot/gatekeeper/approvals.py` | §12.4 deltas 2 (`disarm_expired`), 3 (`DECISION_PURPOSES`, `card_purposes()`, multi-use view tokens in `issue_card_tokens`) and 7 (`create(expires_at=)`; `reissue(…, part_count, keep_expiry)` with `reissue_eligible()` and the confirmed-sibling refusal); `Invalidated` widened to optional `action_id`/`cid` (§12.4.2); nothing else |
| `src/ossbot/gatekeeper/context.py` (`AppContext`) | `reissue(cid, actor, *, keep_expiry)`, the only PR-side re-send entry point, which passes `cards.part_count` and publishes after the commit, and `reissue_proposal(cid, actor, *, keep_expiry)` and `reissue_skill(skill, actor, *, keep_expiry)`, the only callers of the proposal and skill services' `reissue`, which publish the same way (§12.4.3); `card_recover: Callable[[], None]` (no-op default, set by `TelegramFrontend.start`, §12.4.4); `reissue_cards` skips `Invalidated` rows with `action_id=None` |
| `src/ossbot/gatekeeper/app.py` (`housekeeping_once`) | Calls `expire_due()` and `disarm_expired()` on every decision service (delta 9), queues the S1 card edits, then calls `ctx.card_recover()` (delta 8) |
| `src/ossbot/gatekeeper/control.py` | **Call sites only** (delta 9): the four `invalidate_armed_confirmed("killed", …)` sites (kill, resume-before-reconcile, resume-log, flag reconcile) loop over `ctx.decisions.all()`; `set_dry_run` is unchanged |
| `src/ossbot/gatekeeper/config_snapshot.py` | **Call sites only** (delta 9): both `invalidate_all_open("config_snapshot_changed")` branches loop over the decision services; the proposal and skill re-issue after a reload (§12.4.2) |
| `src/ossbot/gatekeeper/state_machine.py` | `sweep_expired`: reference = the newest approval's window start, and the move to `EXPIRED` cancels the contribution's `pending_approval` actions (delta 7); the two human-only CR-004 Undo edges `PROPOSAL_REJECTED → PROPOSED` and `PROPOSAL_SNOOZED → PROPOSED` (§5.15); nothing else |
| `src/ossbot/common/ids.py` | `new_proposal_approval_id()` (`p_`) and `new_skill_approval_id()` (`s_`) over the existing `new_id` |
| `src/ossbot/gatekeeper/telegram/review/{resolve,size_line}.py` | **New**: kit-token resolution by `action_id` with the purpose-class outcomes (§12.2); the size line and overrun formula (§5.1) |
| `src/ossbot/gatekeeper/chat/answer_view.py` | **New**: renders a Hermes answer or unsolicited Hermes message for its thread kind: full in Assistant and user topics; in flat mode and in Reviews, the gatekeeper `🤖 Assistant` header plus the body in one quote (rich: one `blockquote`/`expandable_blockquote` block holding every body block; classic: `<blockquote expandable>`), with flat mode keeping follow-up and feature buttons; never in Control or the feed (U6, §4.4 rules 7–9) |
| `src/ossbot/gatekeeper/telegram/auth.py` | Adds `authorized_chat_only()` (U10 exception); `authorized()` unchanged |
| `src/ossbot/gatekeeper/chat/imgmeta.py` | **New** metadata walker (§6.5) |
| `src/ossbot/gatekeeper/telegram/cards.py` | Decorated S1 layout via the kit (FEAT-005 item 22); skill card; stale rendering |
| `src/ossbot/gatekeeper/telegram/keyboards.py` | `style`, `disabled`, URL/copy buttons in `to_json`; the regrouped card rows |
| `src/ossbot/gatekeeper/telegram/escape.py` | `defuse_commands`; the `tg-time` helper |
| `src/ossbot/gatekeeper/telegram/handlers.py` | Delegates classification to `routing.py`; new token purposes; `stopped_message_generation` `TypeHandler`; `_send_reply`/`_edit` rewritten to send through `telegram.ui.send` in the originating thread, except the U3 Approve/Confirm edits (§4.1); the update-handler budget (§12.5): `spawn(coro, kind, *, session_key, on_done)` with the done-callback cleanup, `cancel(kinds)`, the session map, the `command` kind for I/O-bound command and confirm effects (§8.1), the per-handler timer thresholds, `drain()` cancelling the chat-side kinds, the startup reset of `streaming` turns, and the 8 s bound on the Confirm preflight |
| `src/ossbot/gatekeeper/telegram/bot.py` | `ALLOWED_UPDATES += ["stopped_message_generation"]`; topic bootstrap; command scopes and menu button; draft lane task; short-description updates on up/down; `CardPublisher`: dispatch through `ctx.decisions.for_id(aid)` for `a_`/`p_`/`s_` cards (§12.4.4), per-kind `card_rows`, single-transaction `_card_sent`/`_maybe_arm`, `recover()` (run in `_connect` before polling, and wired to `ctx.card_recover` in `TelegramFrontend.start`), `render_s1()` and `render_stale()` (§5.5), the `ViewTooLarge` stub (§12.1); `concurrent_updates` stays unset (§12.5) |
| `src/ossbot/gatekeeper/telegram/commands/{builtin,registry}.py` | New commands (§8.1); safety class `personal`; picker integration; `menu()` per control state |
| `src/ossbot/gatekeeper/outbox.py` | Columns `priority`, `thread_key`, `coalesce_key`, `result_message_id`, `bind_tokens_json`, `view_json`, `merged_into`; statuses `held`, `merged`; `mark_sent(row, result)` binds tokens, writes `tg_views`, increments `tg_thread_seq` and counts parts by id prefix in its transaction (§12.2, delta 6); quiet-hours hold and batching, never for rows with buttons or an `approval_id` (§7.3); "thread not found" recovery |
| `src/ossbot/gatekeeper/chat/{backend,hermes_api,stream,md2blocks,followups,media,turns}.py` | FEAT-005 item 23 plus streaming, rendering, follow-ups and media (§6); `turns.py` holds `finalize_turn` and `reset_streaming` (§12.5, BA-15); `stream.py` redacts the answer before it is sent or stored in `chat_turns.answer_text`, and builds Stop partials and truncated answers with `draft_visible()`; `drafts.py` applies the redact-then-hold-back rule (U14, §6.1 steps 3–5) |
| `src/ossbot/gatekeeper/review_html.py` + `templates/review.html.j2` | **New** HTML diff (§5.4) |
| `src/ossbot/gatekeeper/interdiff.py` | **New**: runs `INTERDIFF`/`RANGE_DIFF` at `/package` (§5.7) |
| `src/ossbot/gatekeeper/packaging.py`, `derive.py` | Call `interdiff.py`; retention amendment (§5.7); packaging passes `ctx.secret_scanner.values` to `policy.check_text` (title, body, commit message, comment) and scans the patch with `ctx.secret_scanner`, recording `Violation("gatekeeper_secret", "patch")` (§12.1) |
| `src/ossbot/common/gitcmd.py` | `INTERDIFF`, `RANGE_DIFF`, mirror plumbing argv constants |
| `src/ossbot/gatekeeper/review_mirror.py`, `review_mirror_writer.py` | **New**, opt-in (ADR-0019 C); import-linter contract "only review_mirror imports review_mirror_writer" |
| `src/ossbot/gatekeeper/telegraph.py`, `telegraph_writer.py` | **New**, opt-in (§6.8). The confirm panel carries the n/a mode badge; Publish and Unpublish check `system_running()` at the tap, at Confirm and right before the API call (refused while paused or killed); `eligible(turn_row)` is re-evaluated from the current `chat_turns` row at render, at the tap, at Confirm and before `createPage`. Import-linter: only `telegraph` imports `telegraph_writer`; egress category `telegraph` (gatekeeper only); `config/egress.yaml` `always_denied` += `telegra.ph`, `api.telegra.ph`, `edit.telegra.ph` for every other profile |
| `src/ossbot/gatekeeper/features/{__init__,registry}.py` | **New** registry (§12.3); `api/features.py` route `POST /v1/features/{feature}/cards` (role hermes), which increments `chat_turns.feature_cards` of the turn it resolved (§6.8 eligibility) |
| `src/ossbot/gatekeeper/api/notify.py`, `api/features.py`, `common/schemas.py` (`NotifyRequest`) | New optional `priority: Literal["P1","P2"] = "P2"` (anything else, `P0` included, → 422; P0 is gatekeeper-internal only). Requester (Hermes): target thread resolved from `chat_turns` only, Assistant/user topics only, 🤖 prefix, P1 ≤ 5/day (U12, §6.7). Worker: 📈 OSS feed, P2 default, P1 allowed within the existing 10/h |
| `hermes/plugins/ossbot/` | Tools `ossbot_feature_card`, `ossbot_reminder_create`; follow-up instruction text |
| `config/telegram.yaml` (+ schema) | **New** tracked config loaded through the snapshot (§14.3) |
| `config/profile.example.yaml` | `telegram.timezone`, `telegram.quiet_hours`, `review_mirror.repo` |
| `scripts/setup-secrets.ps1` | Optional `GITHUB_REVIEW_MIRROR_TOKEN` (and later integration keys), set by key name only |
| `src/ossbot/ctl/` | `ossctl review-mirror enable|disable|status|purge`; `doctor`: topics state, rich fallback flag, mirror probes |
| `pyproject.toml` | Import-linter contracts: features/chat cannot import `approvals`, `submit`, `github_writer`, `review_mirror_writer`, `state_machine`, `control`; only `telegram.ui.send` writes Telegram rows for chat/features |
| `README.md`, `AGENTS.md`, `docs/SETUP.md` | Telegram tour (topics, dashboard, review flow), the §12.3 guide, the BotFather topic setting (SETUP **recommends enabling topics**: flat mode works, but there separation from cards is by rendering only, U12), mirror setup (optional) |

### 14.2 Data model (`migrations/00NN_telegram_ux.sql`, next free number)
```sql
CREATE TABLE tg_topics (name TEXT PRIMARY KEY, thread_id INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE tg_views (
  message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, thread_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('dashboard','card','explorer','file','interdiff','draft_review','status',
                                      'digest','feature','picker','progress','chat_answer',
                                      'body_part','commit_msg','fyi','confirm_prompt',
                                      'proposal','proposal_details','rescope','ceiling','work_request','link_card',
                                      'skill_card','skill_part')),
  approval_id TEXT, cid TEXT, rev INTEGER,
  action_id TEXT,                                -- outbound action the view belongs to (§12.2); NULL for read-only explorers
  path TEXT, page INTEGER,
  state_json TEXT NOT NULL DEFAULT '{}', content_sha256 TEXT, updated_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE tg_thread_seq (thread_key TEXT PRIMARY KEY, n INTEGER NOT NULL);   -- §5.3: messages seen per thread
CREATE TABLE proposal_approvals (               -- §12.4.2; CR-004 owns the proposal content, referenced by (cid, version)
  id TEXT PRIMARY KEY,                          -- 'p_' + 10 Crockford chars (new_id("p"))
  cid TEXT NOT NULL REFERENCES contributions(id), version INTEGER NOT NULL,
  binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','armed','confirmed','consumed','invalidated','expired','rejected')),
  tg_chat_id INTEGER NOT NULL, tg_message_id INTEGER,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, armed_at TEXT, confirmed_at TEXT, consumed_at TEXT,
  invalidated_reason TEXT, decided_by_tg_user INTEGER,
  rejected_reason TEXT CHECK (rejected_reason IN ('skipped','snoozed','never_repo','never_kind')));   -- §5.15
CREATE INDEX proposal_approvals_open ON proposal_approvals(cid, version, status);
CREATE TABLE skill_approvals (                  -- §9, §12.4.2; FEAT-009's skill_reviews keeps the staged content
  id TEXT PRIMARY KEY,                          -- 's_' + 10 Crockford chars (new_id("s"))
  skill_review_id TEXT NOT NULL,                -- FEAT-009 skill_reviews.id (any format FEAT-009 ships)
  skill TEXT NOT NULL, version INTEGER NOT NULL,
  binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','armed','confirmed','consumed','invalidated','expired','rejected')),
  tg_chat_id INTEGER NOT NULL, tg_message_id INTEGER,
  body_parts_total INTEGER NOT NULL, body_parts_delivered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, armed_at TEXT, confirmed_at TEXT, consumed_at TEXT,
  invalidated_reason TEXT, decided_by_tg_user INTEGER);
CREATE INDEX skill_approvals_open ON skill_approvals(skill, status);
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
  had_attachments INTEGER NOT NULL DEFAULT 0,
  feature_cards INTEGER NOT NULL DEFAULT 0);   -- feature cards accepted for this turn (§6.8 eligibility)
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
**Every view has a listed `tg_views.kind`.** `telegram/ui/send.py` defines `ViewKind = Literal[…]` with exactly the CHECK's values and validates `view_json.kind` with it at **enqueue**, so a bad kind fails before anything is sent (§12.2 step 3). Views without a dedicated kind map like this:

| View | `kind` |
|---|---|
| `/queue`, `/settings`, `/size`, `/filters`, `/notify`, `/quiet`, `/digest`, `/memory`, `/articles`, `/repos`, `/model`, `/egress`, `/hermes`, `/approve` and `/review` pickers, and every other paged list or toggle grid | `picker` |
| Command confirms (`Reply.confirm`), the comment-replace panel (§4.4 rule 5), the Telegraph publish panel | `confirm_prompt` |
| Reminders when they fire, the Telegraph `Published` reply, comment acknowledgements, `Still answering`, `/status`, `/help`, `/start` and other plain replies, toasts sent as messages, P0/P1/P2 notices | `fyi` |
| Assistant answers, including Reviews-topic and flat-mode answers | `chat_answer` |
| Feature cards | `feature` |

`ui_tokens.purpose` is free text (the comment in `0001_init.sql` lists the purposes), so no CHECK constraint changes. The new purposes are listed in §12.2. `ui_tokens.approval_id` has no foreign key, so it can hold `a_`, `p_` and `s_` ids alike.

Expiry: proposal approvals use `proposal.ttl_hours` (CR-004, 72 h by default); skill approvals use `limits.approval_ttl_hours`; both use `limits.confirm_ttl_seconds` for Confirm.

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

Why lenient decoding matters here: the gatekeeper is the only poller, and the same `getUpdates` stream carries Approve, Confirm, `/kill` and `/pause`. One unknown field must never stop it. PTB 22.8 keeps unknown keys in `api_kwargs`; aiogram's strict pydantic decoding blocked long polling on the Bot API 10.3 `RichTextButton` (aiogram #1890, fixed by upgrading to 3.31) and still can on a missing required field (aiogram #1840 open, its fix PR #1848 unmerged). The comparison is in ADR-0020.

### 14.5 Error handling (per operation)

| Operation | Failure | Recoverable? | User sees | Logged |
|---|---|---|---|---|
| `sendRichMessage` | 400 (unsupported, block error) | Yes: classic fallback; `rich_unavailable_until = +24 h` | The same view in classic form | WARNING once/day + payload kind |
| `editMessageText(rich_message)` of an existing view | 400 | Yes: the classic view is **sent as a new reply to the card**, `tg_views` moves to the new message (old row `stale = 1`), and the old message is stubbed (or deleted if the stub edit fails too) (§5.2); `rich_unavailable_until = +24 h` | The view continues below; the old one says "moved ↓" | WARNING once/day + payload kind |
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
| Hermes stream | Event > 1 MB or answer > 200 KB | Truncated: the stream is ended early, and the text sent and stored is `draft_visible(text)` + `…(truncated)` (U14, §6.1 step 4) | The partial answer with the "…(truncated)" note; never a secret's prefix | WARNING |
| Hermes stop call | Error | Yes: close the stream | The partial `draft_visible(text)` + `…` and `⏹ Stopped by you` (§6.1 step 5) | WARNING |
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
| Kit token tap with no open approval for its `action_id` | Approval invalidated or expired; or the action was submitted | Navigation: yes, read-only after submit. Others: no (by design) | Per the §12.2 purpose-class table: "Superseded…", "Submitted: comment on the PR instead.", "Expired: tap 🔁 Retry…" or "Already submitted · PR #N" | DEBUG |
| `CardPublisher.recover()` | A sent card row without `result_message_id` (sent before the migration) | No for that row; the approval stays reachable via `/queue → Open` | Nothing | WARNING once per row |
| `CardPublisher.recover()` | DB error | Yes: next housekeeping tick | Nothing (card waits) | ERROR |
| Card render | `ViewTooLarge` (a bug: required blocks are bounded, §12.1) | No for that approval: invalidated `render_failed` | The fixed stub "⚠️ Card too large to render (bug) · …" with no buttons | ERROR (view kind and block sizes only) |
| `reissue(keep_expiry=True)` | An inherited `expires_at` within `confirm_ttl_seconds` of now | Yes: a fresh `now + approval_ttl_hours` is used | Card shows the new expiry | INFO |
| `AppContext.reissue` (Show card, Show current, `/queue → Open`, `/approve`, Retry) | An approval of the cid is `confirmed` (`ApprovalError("submitting")`) | Yes: retry after the submission finishes | Toast `Submitting… try again in a moment`; nothing changes | INFO |
| `AppContext.reissue` | No eligible action (`nothing_to_resend`: `IMPLEMENTING`, `EXPIRED`, cancelled) | No | `Nothing to re-send (state <state>)` | INFO |
| `AppContext.reissue` | `on_approval_created` fails for one new id after the commit | Yes: the approval exists with no card row; `CardPublisher.recover()` step 0 (§12.4.4) enqueues it on the next tick | The card arrives within one tick | ERROR |
| `stale_show_current` | No open card for the contribution | No | `No open card for c_… (state <state>)` | DEBUG |
| Comment replace (§4.4 rule 5) | `policy.check_text` refusal, too long, or the action no longer eligible | No: nothing stored | The exact reason, or the §12.2 refusal text | INFO |
| Proposal Undo | Over 10 min, or the state moved on | No | `Too late to undo; re-request with /work <issue url>` | INFO |
| `ProposalApprovals.reject` | The approval is `armed` | Yes: Cancel first | `Cancel the Start-work confirm first` | INFO |
| Update handler | Took longer than its threshold (1 s; the Confirm handler 9 s) — a budget breach, a bug | Yes | Nothing | WARNING `handler_slow` (handler name, ms, threshold) |
| Spawned task | Cancelled before its first step (e.g. a message and `/kill` in one `getUpdates` batch) | Yes: the done callback frees the session and runs `on_done` (`chat_turns` → `stopped`, album buffer dropped) | Nothing beyond the `/kill` reply; the next message in that session starts a new turn | DEBUG |
| Spawned task `on_done` | Raises (e.g. DB error) | Partly: the session-map entry and task index are already cleared; a `chat_turns` row left `streaming` is reset at the next start, and U12 stops accepting its session 30 min after `started_at` even before that (§6.7) | Nothing | ERROR (kind, ids) |
| `command` task | The effect fails (probe error, root-helper refusal, timeout) | Per the command's Phase 1 rules | The reply edited to the command's existing failure text | As the command logs today |
| Redaction, display text (U14) | `redact()` replaced a secret in a draft, answer, view leaf, log tail or document | Yes: sent redacted | `[REDACTED]` in place of the value | WARNING `redacted {surface, hits}` once per turn or view, never the match |
| Redaction, verbatim publish bytes (U14) | `scan()` finds a secret in a card part, commit message, comment, skill part, `.patch`, `pr_body.md`, HTML-diff code, the PR title (card, explorer section 9, `Copy title`) or publish text quoted in an S7/`edit_comment` prompt (a bug: packaging and the edit paths gate these with the same scanner, §12.1) | No for that approval: invalidated `render_failed` | The fixed `⚠️ Card withheld … (bug)` stub (§12.1) in place of the card, document or prompt; the title is never shown redacted | ERROR (surface and count only) |
| Packaging or an edit path (`policy.check_text`) | A gatekeeper secret value (`HERMES_API_KEY`, `OSSBOT_TOKEN_PEPPER`, `TELEGRAPH_ACCESS_TOKEN`, …) in the title, body, commit message, comment text or patch | Packaging: the normal policy-violation path (coder feedback or redirect). Edit: no, nothing stored | Packaging: the usual violation note, reason `gatekeeper_secret (<surface>)`. Edit: the edit path's existing refusal text | WARNING (code and surface, never the value) |
| Telegraph publish | The turn is no longer eligible at the tap, Confirm or before `createPage` (e.g. a feature card arrived after the answer) | No | `Not published: this answer is no longer eligible` | INFO |
| Spawned task (`Handlers.spawn`) | Unhandled exception | No for that task | The operation's own failure text from this table (e.g. "Assistant unavailable", "Publish failed") | ERROR (kind and ids, never content) |
| Chat message | The session is busy (an entry in the session map) | Yes: re-send after the answer | `⏳ Still answering — tap ⏹ Stop or wait` | DEBUG |
| Confirm live preflight | No result within 8 s | Yes: Retry | The existing live-check failure text with `timeout`; card S12 `live_check:timeout` with `🔁 Retry` | WARNING |
| `/kill` task cancellation | A `telegraph` task inside its shielded request (`request_started` true) | Yes: it finishes, records the page and announces it | The panel reads `📖 Published (after stop) · public link · see /articles` (or the normal Published reply if the cancel came after it); the page is in `/articles` | INFO |
| `/kill` task cancellation | A `telegraph` task before its request (`request_started` false) | No: nothing was sent to Telegraph | The panel reads `Not published (stopped).` | INFO |
| Decision fan-out at a **kill** | One service's `invalidate_armed_confirmed` raises | Yes. The kill itself always commits: each service call is wrapped, so a failure can't undo the kill. Every service refuses Confirm and its effect while killed anyway, and the flag-reconcile path retries the invalidation on the next tick | P0 alert "kill: <service> invalidation failed — retrying" | ERROR |
| Decision fan-out at a **reload** | One service's `invalidate_all_open` raises | No: the whole reload transaction rolls back and the reload is refused (the existing reload error path), so no new snapshot runs with stale approvals | The existing reload refusal | ERROR |
| Proposal Start work / skill activation | Paused, killed (and for Start work: draining, `💤 DOWN (workers)`) | Yes: retry after resume or up | The existing toasts | INFO |
| Telegraph publish / unpublish | Paused or killed | Yes: after `/resume` | "Paused or killed: nothing was published." / "…the page was not changed and is still public. /resume first." | INFO |
| `POST /v1/notify` | `priority` not `P1`/`P2` (incl. `P0`) | No | Nothing | INFO; HTTP 422 to the caller |
| Confirm-panel refresh (§5.5) | "message is not modified" / "not found" / network | Ignored: the count only informs | Count may lag by one | DEBUG |
| `mark_sent` token bind | A token in `bind_tokens_json` already bound (should not happen) | Fatal for that row's buttons: the row is marked sent, the stray tokens are killed | Buttons answer "This button expired; reopen the view" | ERROR (bug) |
| Hermes notify / feature card | `session_key` with no turn that is `streaming` and started ≤ 30 min ago, or finished ≤ 10 min ago | No | Nothing | INFO; HTTP 409 to the plugin |
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
| Picker / nav token `arg` | yes | JSON validated by the purpose's pydantic model (`extra="forbid"`). Review kit tokens require `cid` (`ID_RE`), `rev` (int ≥ 1) and `action_id` (`ID_RE` with prefix `x_`, or `null` only for read-only explorers); proposal kit tokens require `cid` and `version` (int ≥ 1). Exceptions: `stale_show_current` takes exactly `{cid}`, `{cid, gate: 1}` or `{skill}` (a skill name, 1–64 `[a-z0-9-]`); `comment_replace` takes `{cid, action_id, text}` with `text` 1–4,000 characters | Treated as an invalid token |
| `NotifyRequest.priority` | optional | `"P1"` or `"P2"` (default `"P2"`); anything else, `"P0"` included, is rejected | 422; nothing sent |
| Review comment text | yes | Non-empty after strip; ≤ 4,000 characters; UTF-8; `undefuse` applied; ≤ 30 open comments per `(cid, rev)` | Reply with the limit ("Draft review is full (30); send or discard first"); nothing stored |
| Draft review at Send | n/a | Deterministic `change_requests.text` ≤ 60,000 characters | Send refused with the size; nothing changes |
| S6 "You asked" block (render) | n/a | ≤ 5 items; text ≤ 80, claim ≤ 60 characters; optional block (§12.1) | Cut with "…" / "+N more"; dropped first under budget pressure |
| Proposal adjust feedback | yes | Non-empty; ≤ 2,000 characters; `make_visible`; sent to scout as untrusted user text | Reply with the limit |
| Proposal snooze (Custom) | yes | Integer 1–90 (days) | Usage hint |
| Proposal fields from scout (render) | yes | Per the §5.15 table (problem ≤ 300, ≤ 6 bullets × 100, ≤ 4 paths × 60, rationale ≤ 160, confidence 0–1); scout output validated by CR-004's pydantic model first | Field cut with "…" (display only); a schema failure means no card and a scout retry (CR-004) |
| `/work <url> [notes]` | url yes | `^(?:https://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})(?:/(?:issues\|pull)/(?P<n>[1-9][0-9]{0,9}))?/?$` after stripping `?query` and `#fragment` (a missing scheme is allowed, `http://` is not); notes ≤ 1,000 characters, `make_visible` | Usage hint with the accepted forms; nothing queued |
| Link cards (routing rule 6) | n/a | Same URL regex, found anywhere in text, caption or `entities` of type `url`/`text_link`; ≤ 3 carded. Notes: the message text minus the URLs, ≤ 1,000 characters, `make_visible`; **empty whenever `forward_origin` is set**; shown on the card as `Notes: "<≤ 80 chars>"` | Non-matching links are ignored (normal Hermes routing) |
| `/size` and per-repo class | yes | One of `xs s m l xl`, or `inherit` for a repo; repo must be a configured target | Toast with the allowed values |
| `/filters` toggles | yes | Token `arg` names a key and value from the fixed allowlist (kinds, labels, tier, age, activity, languages from profile, max open PRs 1–3) | Treated as an invalid token |
| Hermes `session_key` (notify, feature card, reminder) | yes | Must match a `chat_turns` row with `(status='streaming' AND started_at ≥ now − 30 min) OR finished_at ≥ now − 10 min`; resolved thread must be Assistant or a user topic | 409 `no_active_turn` / redirected to 💬 Assistant |
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
- `test_kit_tokens_survive.py`, `test_kit_token_resolution.py`, `test_disarm_expired.py`, `test_card_purposes.py`, `test_reissue_keeps_draft.py`, `test_reissue_siblings.py`, `test_outbox_bind_atomic.py`, `test_reissue_keeps_expiry.py`, `test_reissue_part_counts.py`, `test_reissue_eligibility.py`, `test_card_recover.py`, `test_decision_fanout.py` (§12.4 deltas 1–9, as described in each row and in §12.4.3).
- `test_stale_show_current.py`: after r3 is packaged, Show current on the r2 card re-sends r3; on proposal v1 it re-sends v2; on a superseded skill version it re-sends the staged one; with nothing open it answers `No open card…`, and S12 renders no Show current button.
- `test_comment_replace.py`: a reply to a `comment` or `issue_claim` card opens the replace panel and stores no `review_comments` row; Replace runs the `edit_comment` path (policy refusal → nothing stored); `use_as_comment` is not offered when the single open approval is a `comment` or `issue_claim`.
- `test_proposal_decline.py`: Skip, Later and Never call `ProposalApprovals.reject` with their reasons, kill the Start-work tokens and move the contribution; Undo within 10 min re-creates a `p_` approval with the old expiry; Undo after 10 min, or after Never, is refused; the CR-004 edges refuse the gatekeeper actor.
- `test_view_kinds.py`: the `ViewKind` literal equals the `tg_views.kind` CHECK list parsed from the migration, and every View builder's kind is in it; an invalid kind fails at enqueue, never in `mark_sent`.
- `test_dedupe_keys.py`: two Confirm time-outs of the same approval produce two disarm edits; every repeatable edit's key carries its per-occurrence component (BA-3). A comment saved while armed whose Confirm-panel refresh lands after a concurrent `/reload`'s S12 edit is followed by a `stale:<aid>:<comment_id>` re-render, so the card ends at S12 (not S1, and no dead Confirm panel); with a disarm instead, it ends at S1 via `s1:<aid>:<comment_id>`.
- `test_decision_services.py`: `p_`/`s_` ids pass `ID_RE`, and `for_id` dispatches by prefix; refusals (Start work while paused, killed, draining or down; skill activation while paused or killed); the binding is recomputed before the effect; a reload re-issues open proposal and skill cards with their expiry kept.
- `test_answer_view.py` (U6, rules 7–9): an answer in Reviews is classic, with the 🤖 header outside a `<blockquote expandable>`, and is cut at 3,500 with the pointer; text typed in Control or OSS feed is answered in Assistant with the `↪️ from …` line and a pointer in the origin; Assistant answers are rich and unquoted; **in flat mode** every answer, feature card and notification has the gatekeeper header first and its whole body in one quote (rich: a single top-level `blockquote`/`expandable_blockquote` block; classic: `<blockquote expandable>`), and follow-up and feature buttons are kept.
- `test_outbox_hold.py`: rows with `bind_tokens_json` or `approval_id` are never `held` or `merged`.
- `test_view_too_large.py`: maximal untrusted input never exceeds the required-block bounds; a forced `ViewTooLarge` sends the stub without `approval_id`, invalidates `render_failed` and never mints Approve.
- `test_thread_seq.py`: file-view reuse below 20, a re-send at 20; messages in other threads don't count.
- `test_notify_priority.py`: P0 → 422; worker → OSS feed P2; worker P1 allowed; Hermes P1 cap.
- `test_telegraph_control.py`: publish and unpublish are refused while paused or killed (at the tap, at Confirm and before `createPage`); the confirm panel carries the n/a badge and both sentences.
- `test_stale_reasons.py`: every `invalidate_*(…, "<literal>")` call site has a `REASON_LABELS` row (§5.12).
- `test_followups.py` (extended): label == relayed text; items > 64 dropped; the echo is sent before the relay; `origin='followup_tap'` recorded.
- `test_use_as_comment.py`: converts the user's stored message, never the Hermes answer; `undefuse` applied.
- `test_hermes_routing.py` (U12): unknown/stale session key → 409; a row left `streaming` (a failing `finalize_turn`) is accepted at `started_at` + 29 min and refused with 409 at + 31 min; a row finished 9 min ago is accepted and 11 min ago refused; Reviews session → redirected to Assistant; unsolicited messages never in Reviews/Control/Feed; 🤖 prefix; sixth P1 of the day → P2.
- `test_imgmeta.py`: Hypothesis fuzzing and a GPS-tagged corpus (§6.5).
- `test_undefuse.py`: a defused `/kill` quoted back into a comment shows `/kill`, not `⟦U+2060⟧`; a foreign U+2060 elsewhere is still made visible.
- `test_proposal_cards.py`: goldens for v1, v2 with version diff, `👤` variant, CLA and open-PR warnings, forbidden-path variant, expired and superseded stubs; the field bounds of §5.15; Start work only on the card.
- `test_size_line.py`: every row of the §5.1 size table, including code/test split and the coder reason, in `flag` and `reapprove` modes; ceiling hit → no preview; the overrun formula (est 40 → `+330%`, est 48 → `+258%`, est 0 → `est 0 code`, flag by tolerance or by class).
- `test_size_filters_pickers.py`: `/size` never offers `xxl`; per-repo `inherit`; `/filters` has 4 pages, each with ≤ 5 toggle rows + the pager, a fixed row layout whatever the selection, and self-describing age/activity labels; Undo restores the previous value; a ceiling key is refused by `DiscoveryPrefs`; the `🚫 Kind: <name>` label is ≤ 24 for every kind display name.
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
- `test_telegraph.py`: eligibility, scans, node allowlist, random title suffix, unpublish; a feature card accepted for the turn after the answer was sent → the `📖` tap, Confirm and the pre-`createPage` check each refuse with `Not published: this answer is no longer eligible`, and the fake API receives no call. `/kill` cases (§12.5, BA-20): cancelled before the request (`request_started` false) → the panel reads `Not published (stopped).` and the fake API got no call; cancelled while the fake `createPage` is held under the shield → the page is in `telegraph_pages` and `/articles`, the panel reads `📖 Published (after stop) · public link · see /articles`, and exactly one announcement is enqueued; the same for Unpublish (`🗑 Unpublished (after stop)`).
- `test_redaction_paths.py` (U14, AC 66): a fake Hermes stream carries `HERMES_API_KEY`, a `ghp_` token and a Kiro key (`ksk_…`, so the default pattern covers it while streaming; hash in `KIRO_KEY_SHA256`), each split across 3 deltas at random offsets, with and without surrounding whitespace. No draft, final message (classic and rich), Stop partial, truncated answer, `answer.md` or `chat_turns.answer_text` contains the secret or any ≥ 8-character prefix of it. Stop is also tapped at every delta boundary, including after the second of three deltas of `HERMES_API_KEY`, and the stream is also cut by the 200 KB limit in the middle of a `ghp_` token. A follow-up item containing a secret is dropped. A log tail and a log `.txt` with a secret are redacted. A secret planted in a card body part, `.patch` or the PR title is never altered: the approval is invalidated `render_failed`, the stub is sent, no Approve exists, and the title is never shown as `[REDACTED]` on the card, in explorer section 9 or in `copy_text`. An AST test asserts that `classic.py`, `rich.py`, `drafts.py`, `chat/stream.py` and the document builders call `redact` or `draft_visible`, and that `chat/stream.py` calls `draft_visible` on its Stop and truncation paths.
- `test_secret_scan.py` (§12.1, BA-16, BA-21): the load refusals (a secret value with whitespace or ≥ 256 characters; an `extra_patterns` entry containing `\s`, `.`, `[\s\S]`, `\W`, `\D`, a negated class or a literal space, or with a minimum width ≥ 256; the four default patterns and `[A-Za-z0-9_]{20,}` accepted); `policy.check_text` with a title, body or commit message holding `HERMES_API_KEY`, `OSSBOT_TOKEN_PEPPER` or `TELEGRAPH_ACCESS_TOKEN` returns `Violation("gatekeeper_secret", <kind>)` without the value in `detail`; packaging on a patch or comment holding one records the violation and takes the normal violation path (no card, no `render_failed`); the S7 title edit with one is refused and nothing is stored.

**Telegram** (fake Bot API, `-m telegram`):
- golden cards S1/S6/S8/S12 in LIVE and DRY RUN for every action kind, plus the skill card;
- explorer navigation edits in place;
- file view paging (rich and classic);
- the rich 400 → classic fallback, for a send and for an in-place edit (new classic reply to the card, old message stubbed, `tg_views` moved);
- `test_handlers_nonblocking.py` (§12.5, AC 59): with a fake Hermes stream held open for 60 s, a typed `/kill` is handled within 2 s and cancels the turn, `stopped_message_generation` ends the stream within 2 s, an Approve tap on another card is answered within 2 s, a 3-photo album in another session is collected and answered, and no `handler_slow` event is logged. Plus (AC 67): a message and a `/kill` returned in one fake `getUpdates` batch → the turn is cancelled before its first step, the session map has no entry, the row is `stopped` with `finished_at` set, and after `/resume` the next message in that session starts a new turn; the same for an album message followed by `/kill` (buffer dropped); a `/reload` confirm whose effect is held by a fake root helper for 5 s doesn't hold the slot (a `/status` sent meanwhile is answered within 2 s) and is not cancelled by `/kill`; a Confirm whose fake preflight takes 6 s logs no `handler_slow`, and any other handler taking 1.5 s does; a `streaming` row left from a simulated crash is `failed` after restart;
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
- the late mirror row 4 re-uses the card's tokens (no new `ui_tokens` rows), including an Explore already tapped several times;
- per-kind card keyboards for `open_pr` r1/rN, `push_update`, `comment` and `issue_claim`, and `📝 Edit text` on a comment opening the `edit_comment` ForceReply directly;
- a reply to a skill card or part reaching Hermes in Assistant and storing no `review_comments` row;
- a stale `/down` producing only the timeout picker with its age;
- the `/queue` pager and `[📥 Open n]` buttons (mockup 12);
- comments → one change request with `comments_json`;
- the two-tap Kill button with its 10-s expiry;
- `/approve` rotating tokens (existing N4).

**Integration:** mirror end to end against a fake GitHub API (respx) and a local bare "remote": push, PR, comments → change request; repo-public → disable + P0; token-too-broad probe → refuse; a GitHub Approve is ignored.

**Live** (manual, recorded in VERIFICATION.md on Amsal's phone and desktop): rich explorer and file rendering; quote-reply on `<pre>` and on rich messages; `details` and `diff` highlighting; HTML document opening; topics; drafts with Stop; `tg-time` relative rendering; disabled buttons; the food-photo estimate (CR-002); the merged effect.

### 14.8 Milestones and mapping to PLAN.md
- **M1** (extends FEAT-005 item 22):
  - §12.4 token-core deltas 1–9 first, before any new view ships, together with the update-handler budget (§12.5: `spawn` with done-callback cleanup, `cancel`, the session map, the `command` kind, the handler timer, the preflight bound) and the U14 redactor (`telegram/ui/redact.py` over the shared `secret_scan.py`, with the `policy.check_text` `secret_values` change in packaging and the edit paths);
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
  - CR-004 presentation: proposal card and versions, size line, rescope and ceiling cards, `/work` and link cards, `/size`, `/filters`, per-repo size (ships with CR-004's pipeline work).
- **M2** (extends FEAT-005 item 23, FEAT-007): chat streaming via drafts and Stop, md2blocks, sources and footer, follow-ups, media UX, `/new` `/title` `/memory`.
- **M3:** rich views for explorer, file, interdiff, tests and assistant (behind `auto`); digest; status card S10/S11; reminders (§13 item 3).
- **M4** (opt-in): review mirror (ADR-0019 C) + `ossctl review-mirror`.
- **v1.x:** Telegraph articles (§6.8; off by default, with `ossctl telegraph setup|rotate`).
- **Phase 2:** feature registry consumers (nutrition log first), §13 v1.x and phase-2 items, Mini App (only if needed).

Docs and AGENTS.md updates ship with M1. DESIGN §14 is updated to point here. ADR-0004 gains an amendment note: follow-up buttons (D9), rich rendering of answers (D8, U1), `allowed_updates += stopped_message_generation` with `authorized_chat_only()` (U10), the Approve-after-parts rule restated for re-sends (delta 7), and the token-use rule: "View and navigation tokens are multi-use until the approval ends (or 24/72 h for kit views), still message-bound, owner-only and opaque; every **decision** token (Approve, Confirm, Cancel, Reject, Start work, skill activation, Publish) stays single-use (CR-003 §12.4 delta 3)." DESIGN §8.4 T5 ("single-use 128-bit tokens") gains the same sentence as an amendment. DESIGN §14.3's "redaction before any send" gains a pointer to U14, the hold-back rule for drafts, Stop partials and truncated answers, and the PR title's status as publish bytes. DESIGN §14.5 `/approve` semantics are amended to the code's `reissue()` with the expiry kept (§12.4 deltas 5 and 7). DESIGN §14.4a's Confirm sentences are aligned with `confirm_text` (§5.9). DESIGN §12.5 retention is amended. CR-004 §5's state list and tests gain the two human-only Undo edges (§5.15).

## 15. Acceptance criteria
1. Every message the gatekeeper sends passes the §11.4 budgets: Hypothesis tests over all View builders, in classic and rich modes, with random untrusted input. Every `callback_data` is exactly 24 bytes, `t:` plus 22 characters.
2. No untrusted string ever appears in a rich `html` or `markdown` field, and every rich payload has `skip_entity_detection: true`. A test injects `<tg-button type="callback_data" data="x">`, `**`, `/dryrun off` and `tg://` into Hermes output, issue titles and logs, and asserts they render as literal text.
3. In classic mode, `/kill`-style text inside a Hermes answer is not a clickable command: a word joiner follows the slash. Verbatim `<pre>` bytes of PR bodies, comments and commit messages are byte-identical to the stored values.
4. The approval card and Confirm are classic messages. Approve exists only on the card; the explorer, file, interdiff, draft-review and status messages contain no approve or confirm token. A test enumerates the purposes on every kind of view.
5. Approve is minted only after all body parts are delivered (existing AC 14), with the per-kind keyboard of §12.4.1. Golden S1 cards exist in LIVE and DRY RUN for `open_pr` (r1 and rN with interdiff), `push_update`, `comment`, `issue_claim` and `skill_activate`. Each card's minted purposes and their `single_use` flags equal the §12.4.1 table. `comment` and `issue_claim` cards have no Explore, Files or Request changes, and their `📝 Edit text` opens the `edit_comment` ForceReply.
6. The Confirm text equals `ApprovalService.confirm_text` for the kind and the current mode (the DESIGN §12.4 sentence for `open_pr` and DRY RUN; the §5.9 strings for the other kinds). A dry-run toggle between Approve and Confirm leaves the old Confirm token invalid (existing behaviour, re-tested through the new UI).
7. A reply to a file message with a quote that matches one line range stores a `review_comments` row with that `path` and range. An ambiguous quote shows a line picker. A non-matching quote stores a file-level comment.
8. "Send N comments" creates exactly one `change_requests` row whose `comments_json` has N entries. The contribution moves to `CHANGES_REQUESTED` and the old card shows S5 with no approvable button.
9. A revised card (rev N) shows the interdiff from `INTERDIFF` when the base is unchanged, or from `RANGE_DIFF` when it moved. With the same base, "touched" flags use the ±3-line window. With a moved base, `files[]` comes from the normalized per-file comparison of the two `FORMAT_PATCH` outputs, "touched" is file-level, the card says "line-level touch check unavailable", and a quote-reply on the interdiff view is stored as a file-level comment. The card states that the full patch is what gets approved. With the rev N−1 mirror missing, the card still arrives with "Interdiff unavailable".
10. Every invalidation reason edits the old card to S12 within one housekeeping tick (≤ 10 s) and stubs its body parts. A stale token tap answers "Superseded…". After r3 is packaged, `⬇️ Show current` on the r2 card re-sends the r3 card. On proposal v1 it re-sends v2. When the contribution has no open card, S12 shows no Show current button, and a tap that races that answers `No open card for c_… (state X)`.
11. `/queue` orders pending approvals by `(class_rank, expires_at, -target_priority, created_at)`. `[📥 Open n]`, `⬇️ Show card`, `⬇️ Show current` and typed `/approve` keep each re-sent approval's `expires_at` (delta 7), so opening an item doesn't move it. At most `loud_cards_per_hour` cards per hour go out without `disable_notification`.
12. With topics enabled in the fake `getMe`, the four system topics are created once and reused across restarts. Approval cards go to Reviews. A "thread not found" error re-creates the topic once and then falls back to flat mode, and the card is still delivered.
13. In the owner scope, the command menu matches the registry; in the default scope it is empty. In killed mode the owner's menu is `/status /resume /logs /help /start`, and `/settings` is absent.
14. The dashboard is edited at most once per 30 s and never when its content hash is unchanged. Its footer is a relative `tg-time`. A deleted dashboard is re-sent and pinned.
15. Typed `/kill` is immediate, including while a Hermes answer is streaming (AC 59). The dashboard Kill needs a second tap within 10 s, and a second tap after 11 s is refused.
16. A go or config command sent more than 10 minutes before processing produces only its confirm prompt, with the age shown. `/pause`, `/kill`, `/drain` and `/dryrun on` run and note the delay. A stale `/down` produces only its timeout picker, with the age shown.
17. P0 messages are loud during quiet hours and while `/quiet` is active. P1 becomes silent and P2 is held and later batched into one message.
18. An assistant turn sends a draft within 1 s, then at most one draft per second, a heartbeat at least every 20 s, and a final persisted message. `stopped_message_generation` stops the Hermes run within 2 s and persists the partial answer, built with `draft_visible()` and ending in `…`, with "Stopped by you". A second message to the same session while it streams gets `Still answering — tap ⏹ Stop or wait` and is not relayed.
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
    - the confirm panel shows `🧪 DRY RUN (n/a: Telegraph is not GitHub)` (or the LIVE variant), `This publishes now, even in dry-run mode.` and `Only secrets, email, phone and name are checked — read the answer for other personal details first.`;
    - while paused or killed, the publish tap, the Confirm tap and Unpublish are refused ("Paused or killed: nothing was published.") and the fake API receives no call, including when the pause starts between Confirm and `createPage`;
    - an answer containing a secret pattern, the Kiro key, a confidentiality-floor string or the profile email is refused with nothing sent;
    - the published nodes use only the allowlisted tags, and the title ends with the random suffix;
    - Unpublish calls `editPage` with the removal text.
28. Egress: `telegra.ph`, `api.telegra.ph` and `edit.telegra.ph` get 403 from every profile except the gatekeeper's `telegraph` category, in every egress mode, including `open`. With `telegraph.enabled: false` the gatekeeper's `telegraph` category is absent from the rendered squid config too.
29. A Confirm panel left untouched for 5 min 10 s is back to S1 (card edited, `approval_disarmed {reason:"confirm_expired"}` audited) and its fresh Approve token arms it again. The same holds for the proposal and skill Confirm panels.
30. After arm → Cancel, after `⬇️ Show card` (`reissue()`), and after `/approve <cid>`, the explorer, the file view, a pending comment prompt and the draft review's `📨 Send` all still work, and the draft review keeps its comments. A comment saved while the card is armed re-renders the Confirm panel with the new count (`⚠️ 2 unsent comments…`) using the same `confirm`/`cancel` tokens (no new `ui_tokens` rows). A Confirm tap after that refresh works, a disarm racing the refresh still ends at S1, and a `/reload` racing the refresh ends at S12.
31. A crash injected between `mark_sent` and `on_sent` leaves every kit token of that message bound and its `tg_views` row present after restart. A crash after a **card** row's `mark_sent` (and at the other three points of `test_card_recover.py`) is healed by `CardPublisher.recover()` at the next start or tick: parts are sent once, Approve is minted once, and `body_parts_delivered` never exceeds the parts actually sent.
32. The S6 "You asked" block shows at most 5 items of ≤ 80 characters plus "+N more". With 30 long comments the card is ≤ 3,900 characters and still contains the header, mode, PR title and expiry lines. The 31st comment is refused; a draft whose `change_requests.text` would exceed 60,000 characters can't be sent.
33. `/down` (workers) leaves the short description "🟢 Online", shows `💤 DOWN (workers)` on the dashboard, and the assistant answers "Assistant is stopped (workers down) — /up". Only a full down sets "💤 Offline since…".
34. The skill card and its Confirm carry the mode badge with "n/a: local change", the Confirm sentence says "dry-run does not apply", the button is styled `danger`, and activation behaves the same in DRY RUN and LIVE.
35. A Hermes feature card or notification with an unknown or stale session key is refused (409). One for the Reviews session lands in 💬 Assistant. No **unsolicited** Hermes message (feature card, notification, reminder) ever lands in Reviews, Control or OSS feed. Each carries 🤖 in its header line, and the sixth Hermes P1 of a day is sent silent. A notify request with `priority: "P0"` gets 422. A worker notice lands in OSS feed as P2.
36. An image document with EXIF GPS (JPEG), `eXIf`/`tEXt` (PNG) or `EXIF`/`XMP ` (WebP) reaches Hermes with those removed and the pixel data byte-identical; a HEIC document gets "send it as a photo"; the fuzz test never raises anything but `ImageRejected`.
37. A defused command quoted back into a comment or echoed as a follow-up shows `/kill`, never `⟦U+2060⟧`.
38. **Proposal card (CR-004):** it shows problem, approach (≤ 6 bullets), likely files, `est <class> · ~code · ~test · files`, validation plan, signals, rationale, confidence, risk and alternative counts, the mode badge "n/a: local change" and the expiry, within 3,900 characters, with the keyboard of §5.15 (Start work only on the card). Start work needs two taps and binds version + proposal hash; a tap on the v1 Start work after v2 exists answers "Superseded…". Adjust plan and Make it smaller produce v2 as a reply with the deterministic "Changed since v1" block.
39. **Size on the preview card:** the size line splits code and test lines; with `on_overrun: flag` an overrun shows `⚠️` with the percentage and the coder's labelled reason and still offers Approve; with `reapprove` a larger class produces a `RESCOPE_REQUIRED` card and no preview; a diff above `policy.diff` produces the ceiling-hit card in both modes and for `👤` work, and no button raises the ceiling.
40. **User-directed work:** `/work <url>` and `🛠 Work on this` on a link card queue a scout and reply "Working on it · 👤 requested by you"; the proposal and preview cards carry `👤 requested by you`; the size preference and filters are not applied; an AI-ban repo gets the refusal card; a link-only message is not relayed to Hermes.
41. **`/size` and `/filters`:** `/size` offers `xs`…`xl` (never `xxl`) and per-repo overrides including `inherit`; a change is immediate, audited, FYI'd and undoable for 60 s; an already-approved proposal keeps its class; the ceiling is shown read-only. `/filters` toggles apply to autonomous discovery only; every page has ≤ 5 toggle rows plus the pager row; every label fits §11.2.
42. **Digest:** the weekly digest shows the merge-rate-by-size table with `l+` merging, the `👤 yours` row and rates only from ≥ 3 PRs.
43. **Approvals topic order:** `/queue` lists gate 2 items before autonomous proposals; a `👤` proposal sorts in class 1 and arrives loud; autonomous proposals arrive silent; an autonomous proposal within 6 h of expiry is promoted to class 2.
44. **View buttons are reusable:** tapping `🧭 Explore` on a card twice, 25 h apart, opens (or reuses) the explorer both times. A card's `🧾 Files` and `📝 Edit text` work after repeated taps. After Cancel, the rotated card's view buttons work and the old ones answer "This button is no longer valid."
45. **Several open actions:** with an `issue_claim` and an `open_pr` open for one contribution (and, separately, a `push_update` and a `comment` during watch), every explorer, file and draft-review token resolves its own action. `⬇️ Show card` re-sends both cards with the toast `Re-sent 2 cards`, and an armed sibling comes back at S1. The explorer, file and interdiff views exist only for `open_pr`/`push_update`. Proposal Details navigation resolves by `(cid, version)`.
46. **Expiry kept on re-send:** an approval created at T and re-sent by Show card, Show current, `/queue → Open` or `/approve` at T+70 h still expires at T+72 h, keeps its `/queue` position, and leaves the `sweep_expired` reference at T. With under 5 min left, or after expiry via `🔁 Retry`, the new approval gets now + 72 h. Show card on an expired action re-sends nothing and answers "Expired: tap 🔁 Retry…".
47. **Decision services:**
    - Proposal approval ids match `^p_…` and skill approval ids `^s_…`, and both pass `ID_RE`.
    - A confirmed `/reload` invalidates open PR, proposal and skill approvals and re-issues their cards. A kill invalidates the armed and confirmed approvals of all three services. A tier switch invalidates skill approvals with `tier_changed`. A dry-run toggle invalidates only PR approvals.
    - Proposal Start work is refused at Start, at Confirm and before the plan is enqueued while paused, killed, draining or `💤 DOWN (workers)`. Skill activation is refused while paused or killed.
    - Each service recomputes its binding before its effect.
    - `REASON_LABELS` covers every new reason.
48. **Hermes answers next to cards:** a Hermes answer containing `🧩 Ready for review … ✅ CI passed` to free text typed in ✅ Reviews arrives as a classic message whose first line is `🤖 Assistant · answer to your message` and whose whole body is inside a quote, with no button other than `[📝 Use as comment]`. The same text typed in 🚦 Control is answered in 💬 Assistant (`↪️ from 🚦 Control: …`), and Control gets only `🤖 Answered in 💬 Assistant`. **Flat mode** (topics off): the same lookalike answer, a Hermes feature card and a Hermes notification each arrive with the gatekeeper `🤖 Assistant · …` header as the first line and the whole body inside one quote (rich: one `blockquote`/`expandable_blockquote` block; classic: `<blockquote expandable>`). Follow-up and feature buttons are still present.
49. **Batching never eats buttons:** during quiet hours, a P2 kit view with buttons and a card part are sent immediately and silently, never `held` or `merged`, and their tokens are bound.
50. **`/filters`:** 4 pages (Kinds; Labels and tier; Age and activity; Languages and PRs). Every page has ≤ 5 toggle rows plus the pager. Age and activity labels name their filter (`Age 30 d`, `Active 30 d`, `Age any`, `Active any`), and every multi-button label is ≤ 12 characters where 3 share a row.
51. **Labels:** `🚫 Kind: <name>` is ≤ 24 code points for every kind display name, and the confirm text names the kind in full.
52. **File-view reuse:** with 19 messages in the thread since the file view was sent, `Open` edits it in place. At 20 it sends a new one and stubs the old one. Messages in other threads don't count.
53. **Card too large:** a forced `ViewTooLarge` sends the fixed stub with no buttons, invalidates the approval `render_failed` and logs ERROR, and no Approve token exists for it. Maximal untrusted input never reaches `ViewTooLarge`.
54. **Overrun formula:** the size line's percentage is `round(100 × (code_changed − est_code) / est_code)` with `code_changed = added_code + deleted_code` (est 40 → `+330%`, est 48 → `+258%`). `est_code = 0` shows `est 0 code`, and the ⚠️ appears on tolerance or on a larger class.
55. **Skill-card replies:** a reply to a skill card or one of its parts reaches Hermes in 💬 Assistant with `Re: skill <name> v<version>` and creates no `review_comments` row.
56. **After submit:** after Confirm (LIVE and dry run), the rev's explorer and file views still navigate. Their comment buttons, and replies to them, answer "Submitted: comment on the PR instead." (`dry_run_done`: "Dry run done: this rev was not published."), and Show card answers "Already submitted".
57. **Re-sent cards arm only after their own parts:** with an `issue_claim` (1 part) and an `open_pr` (3 parts) open, Show card creates approvals with `body_parts_total` 1 and 3. With the `open_pr`'s part 2 failing once, no Approve token exists for the `open_pr` until part 2 is delivered. Every entry point (Show card, Show current, `/queue → Open`, `/approve`, Retry) goes through `AppContext.reissue`, which publishes each new card once. Proposal and skill re-sends (Show current, `/queue → Open`, Undo, `/skills → Open card`) go through `AppContext.reissue_proposal` and `AppContext.reissue_skill`, which also publish each new card once after the commit, so the card arrives without waiting for a recover tick (`test_reissue_part_counts.py`).
58. **Re-send eligibility:** during `IMPLEMENTING` (the r2 action still `pending_approval`), a typed `/approve`, Retry and Show current create no approval and answer `Nothing to re-send (state IMPLEMENTING)`. After the `EXPIRED` sweep the contribution's actions are `cancelled`, and S12 has no Retry. While a sibling approval is `confirmed`, every re-send is refused with `Submitting… try again in a moment`, and the confirmed approval submits normally (`test_reissue_eligibility.py`).
59. **Update handler budget:** with a fake Hermes stream held open for 60 s, a typed `/kill` is handled within 2 s and cancels the turn (`chat_turns.status='stopped'`), and `stopped_message_generation` ends the stream within 2 s. An Approve tap on another card is answered within 2 s, and no handler logs `handler_slow` (`tests/telegram/test_handlers_nonblocking.py`). The Confirm handler's threshold is `PREFLIGHT_TIMEOUT_S + 1` (a 6 s preflight logs nothing); every other handler's is 1 s. A `command` effect (a `/reload` held 5 s by a fake root helper) doesn't hold the slot and is not cancelled by `/kill`.
60. **Replies to comment and claim cards:** a reply to a `comment` or `issue_claim` card or its body part stores no `review_comments` row and opens `Replace the comment text with your reply?`. Replace produces a new card with the reply as its text (after normalization and `policy.check_text`), and the old card goes S12 `text_edited`. Cancel changes nothing. `[📝 Use as comment]` is offered in ✅ Reviews only when the single open approval is an `open_pr` or `push_update`.
61. **Proposal decline and Undo:** Skip, Later and Never (after its confirm) set the `p_` approval `rejected` with reason `skipped`, `snoozed`, `never_repo` or `never_kind`, kill its Start-work tokens, and move the contribution to `PROPOSAL_REJECTED` or `PROPOSAL_SNOOZED` (actor human, audited). Undo within 10 min (Skip and Later only) removes the record, returns the contribution to `PROPOSED` and sends a fresh card with a new `p_` approval and the old expiry, whose Start work works. Undo after 10 min answers `Too late to undo…`.
62. **Split on the ceiling card** sends the fixed split feedback at once and then an optional ForceReply whose placeholder is `Anything to add? (optional)` (≤ 64 characters). No reply leaves the split request unchanged.
63. **Link-card notes:** a typed message `<url> keep it minimal` gets a link card with `Notes: "keep it minimal"`, and `🛠 Work on this` passes those notes. A forwarded message with a URL and text gets a link card with no `Notes:` line, and `🛠 Work on this` passes empty notes.
64. **Rich edit fallback:** when an in-place rich edit of a file view returns 400, the classic file view arrives as a new reply to the card, the old message is stubbed "moved ↓", and its buttons answer "Superseded…".
65. **Telegraph and feature cards:** a turn for which `api/features.py` accepted a feature card has no `📖 Open as article` button; the same answer without a feature card does. When the feature card is accepted **after** the answer (and its button) was sent, the `📖` tap, Confirm and the check right before `createPage` each refuse with `Not published: this answer is no longer eligible`, and the fake Telegraph API receives no call.
66. **Redaction before any send (U14):** a fake Hermes stream carries `HERMES_API_KEY`, a `ghp_` token and a Kiro key, each split across 3 deltas. No draft, final message (classic and rich), Stop partial, truncated answer, `answer.md` or `chat_turns.answer_text` contains the secret or any prefix of it of 8 or more characters; the final answer shows `[REDACTED]` in its place. This includes Stop tapped while the second of three deltas of `HERMES_API_KEY` has arrived (the stopped message and the stored answer end before the key's first character, then `…`), and a stream cut by the 200 KB limit inside a `ghp_` token. Log tails and log documents are redacted; a follow-up item containing a secret is not offered. Verbatim publish bytes are never altered: a secret planted in a card body part or `.patch` invalidates the approval `render_failed`, sends the withheld stub and leaves no Approve. A secret value planted in the PR title invalidates the approval `render_failed` and sends the withheld stub, and the card, explorer section 9 and `📋 Copy title` never show the title redacted. At packaging, the same value in the title, body, commit message, comment or patch is a `gatekeeper_secret` policy violation, so in production that card is never rendered (`test_redaction_paths.py`, `test_secret_scan.py`).
67. **Cancel before start:** a message and `/kill` returned in one `getUpdates` batch leave no busy session and a `chat_turns` row `stopped` with `finished_at` set; after `/resume`, the next message in that session starts a new turn and is answered (no `Still answering`). A `streaming` row left by a crash is `failed` after restart, so U12 refuses that session's Hermes notifications with 409 (`tests/telegram/test_handlers_nonblocking.py`). A row left `streaming` because `finalize_turn` failed is refused by U12 once `started_at` is more than 30 min old, without a restart (`test_hermes_routing.py`).
68. **Telegraph after `/kill`:** a publish cancelled before its request leaves the panel at `Not published (stopped).` and sends nothing to Telegraph; a publish cancelled during the shielded `createPage` records the page, lists it in `/articles` and edits the panel to `📖 Published (after stop) · public link · see /articles`, with exactly one announcement (`test_telegraph.py`).

### 15.1 Build acceptance criteria carried from review
These are implementation-level details from design review rounds 3 and 4: exact signatures, key formats, type shapes, constants and literal lists. The design decisions they implement are in the sections named. Each item is a build acceptance criterion with its test, checked in the build review, and none of them changes a design decision. BA-1 to BA-14 come from round 3 (column "From" names the round-3 finding); BA-15 to BA-19 come from round 4 ("R4 finding n"); BA-20 to BA-22 come from round 5 ("R5 finding n"). BA-1 and BA-2 were extended in round 4; BA-3 and BA-16 were extended in round 5.

| # | From | Criterion | Test |
|---|---|---|---|
| BA-1 | Finding 1 | Exact signatures (strict mypy): `ApprovalService.reissue(self, cid: str, *, tg_chat_id: int, actor: str, part_count: Callable[[str], int], keep_expiry: bool = False) -> list[str]`; `AppContext.reissue(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]`; `ProposalApprovals.reissue(self, cid: str, *, actor: str, keep_expiry: bool = False) -> list[str]`; `SkillApprovals.reissue(self, skill: str, *, actor: str, keep_expiry: bool = False) -> list[str]`; `approvals.reissue_eligible(kind: OutboundKind, action_status: str, state: State) -> bool`. The old `body_parts_total` parameter of `reissue` no longer exists. No production module except `context.py` calls `ApprovalService.reissue`, `ProposalApprovals.reissue` or `SkillApprovals.reissue` (R4 finding 6; wrappers in BA-17), and none of the three services calls `on_approval_created` | `test_reissue_part_counts.py`; `test_reissue_eligibility.py` (a table test over every kind × state); an AST test that finds every `.reissue(` call on any of the three services outside `context.py`, and every `on_approval_created` reference inside `approvals.py`, `proposal_approvals.py` and `skill_approvals.py`, and fails |
| BA-2 | Finding 2 (+ R4 findings 2–4) | `TaskKind = Literal["chat_turn", "media", "telegraph", "github_read", "render", "submit", "command"]`; `Handlers.spawn(self, coro: Coroutine[Any, Any, Any], kind: TaskKind, *, session_key: str \| None = None, on_done: Callable[[asyncio.Task[Any]], None] \| None = None) -> asyncio.Task[Any]`; `Handlers.cancel(self, kinds: set[TaskKind]) -> int` (returns the number cancelled); `KILL_CANCELS: frozenset[TaskKind] = {"chat_turn", "media", "telegraph", "github_read", "render"}`; `PREFLIGHT_TIMEOUT_S = 8.0`; `HANDLER_SLOW_S = 1.0`; the Confirm handler's timer threshold is `PREFLIGHT_TIMEOUT_S + 1.0`, every other handler's is `HANDLER_SLOW_S`. `_run_submit` is started through `spawn(…, "submit")`; every I/O-bound command or confirm effect through `spawn(…, "command")`. No handler calls `httpx`, the Hermes client, `getFile` or the GitHub read client directly | `tests/telegram/test_handlers_nonblocking.py`; an import and AST test: `telegram/handlers.py` and `routing.py` call those clients only inside coroutines passed to `spawn` |
| BA-3 | Finding 8 | Outbox dedupe key formats. Once ever: `card:<aid>`, `part:<aid>:<i>`, `arm:<aid>`, `stale:<aid>`, `article:<page_path>:announce` and `article:<page_path>:unpublish_announce` (BA-20). Per occurrence: `disarm:<aid>:<armed_at>` (ISO, the value being cleared), `s1:<aid>:<comment_id>`, `stale:<aid>:<comment_id>` (R5 finding 5: the S12 re-render after a comment-save refresh landed on an invalidated approval), `stale:<aid>:swept` (the post-`EXPIRED` re-render without Retry), `stub:<message_id>:<reason>`. Every kit view without a natural key uses `view:<ulid>`, which is new per send | `test_dedupe_keys.py`: Approve → time-out → Approve → time-out yields two disarm edits that both land; the comment-refresh vs `/reload` race ends at S12 via `stale:<aid>:<comment_id>`; a grep test that every `enqueue(` call site uses one of these formats |
| BA-4 | Finding 10 | `CardPublisher` takes no `ApprovalService` argument and reaches every card family through `ctx.decisions.for_id(aid)` (§12.4.2 protocol). `AppContext.card_recover: Callable[[], None]` defaults to a no-op and is set in `TelegramFrontend.start` next to `on_approval_created`. `housekeeping_once` calls it exactly once per tick | `test_card_recover.py` for `a_`, `p_` and `s_` ids; a housekeeping test with a counting fake `card_recover` |
| BA-5 | Finding 11 | `@dataclass(frozen=True) class Invalidated: approval_id: str; action_id: str \| None; cid: str \| None`. `AppContext.reissue_cards` skips rows with `action_id is None` and never raises `KeyError` on them. `mypy --strict` passes for `ossbot.gatekeeper.*` | `test_decision_fanout.py`: a reload with an open PR, proposal and skill approval re-issues all three through their own services, without error |
| BA-6 | Finding 12 | `telegram/ui/send.py: ViewKind = Literal[...]` lists exactly the `tg_views.kind` CHECK values of §14.2, and `enqueue` validates `view_json` with a pydantic model using it. Every view in the §14.2 mapping table uses its mapped kind | `test_view_kinds.py` (the literal equals the CHECK parsed from the migration; every View builder's kind is valid; an invalid kind fails at enqueue and the row is never written) |
| BA-7 | Finding 13 | Every ForceReply `input_field_placeholder` constant is 1–60 characters (§11.4). No prompt claims pre-filled text. The split prompt's placeholder is exactly `Anything to add? (optional)` | `test_kit_limits.py` (all placeholder constants); `test_proposal_cards.py` (split flow) |
| BA-8 | Finding 14 | `work_links.notes_for(message) -> str` returns `""` when `message.forward_origin` is set; otherwise it returns the text minus URLs, ≤ 1,000 characters after `make_visible`. The card's `Notes:` line shows ≤ 80 characters, escaped and defused, and is omitted when the notes are empty | `test_work_links.py`: typed, pasted, forwarded-with-text, forwarded-link-only, > 80 characters |
| BA-9 | Finding 15 | The edit-fallback path is `rich.edit_or_resend(view_row, view) -> int` (returns the message id now holding the view). It never calls `editMessageText` without `rich_message` on a message that was sent rich. §17 item 15 records whether Telegram allows that switch | Telegram test "rich 400 → classic fallback (edit)" |
| BA-10 | Finding 16 | `api/features.py` increments `chat_turns.feature_cards` in the same transaction that stores the feature card. The §6.8 eligibility reads only `chat_turns` | `test_telegraph.py` (eligibility rows for a feature-card turn and a Reviews turn) |
| BA-11 | Finding 3 | `class StaleShowCurrentArg(BaseModel, extra="forbid")` with the fields `cid: str \| None`, `gate: Literal[1] \| None`, `skill: str \| None`, and a validator that accepts exactly `{cid}`, `{cid, gate}` or `{skill}`. S12 renders the button only when `resolve.open_card_exists(arg)` is true | `test_stale_show_current.py` |
| BA-12 | Finding 6 | `ProposalApprovals.reject(self, approval_id: str, *, reason: Literal["skipped", "snoozed", "never_repo", "never_kind"], actor: str, snooze_until: datetime \| None = None) -> None` (`snooze_until` is required iff `reason == "snoozed"`). The CR-004 Undo edges carry the guard name `undo_window` (≤ 600 s since `proposal_declined`, reason `skipped` or `snoozed`, actor human) | `test_proposal_decline.py`; a state-machine unit test per new edge |
| BA-13 | Finding 4 | `class CommentReplaceArg(BaseModel, extra="forbid")` with the fields `cid: str`, `action_id: str` (`x_` id) and `text: str` (1–4,000 characters, stored after `undefuse` and normalization). Replace calls FEAT-005's `edit_comment` service function, never a copy of it | `test_comment_replace.py` |
| BA-14 | Finding 7 | `answer_view.render(answer, thread_kind, mode) -> View` has a `flat` thread kind. For `flat` and `reviews`, the first block is the gatekeeper header, and **all** remaining blocks are children of one quote block. Feature cards in flat mode pass through the same wrapper | `test_answer_view.py` (flat-mode cases) |
| BA-15 | R4 finding 2 | `Handlers._sessions: dict[str, asyncio.Task[Any]]`; `Handlers.session_busy(self, session_key: str) -> bool`. `spawn` inserts into `_sessions` before returning and registers exactly one done callback, `_on_task_done(task, kind, session_key, on_done)`, which: discards the task from `self.tasks` and the kind index; deletes `_sessions[session_key]` only when it `is` this task; then calls `on_done(task)` inside `try/except Exception` (ERROR on failure). `chat/turns.py: finalize_turn(conn, turn_id: int) -> Callable[[asyncio.Task[Any]], None]` runs `UPDATE chat_turns SET status = :s, finished_at = COALESCE(finished_at, :now) WHERE turn_id = :id AND status = 'streaming'` with `:s = 'stopped' if task.cancelled() else 'failed' if task.exception() else 'done'`, in one `db.transaction`. `chat/turns.py: reset_streaming(conn) -> int` runs at start before polling. The media collector's `on_done` pops its `media_group_id` from the album buffer | `tests/telegram/test_handlers_nonblocking.py`: message + `/kill` in one batch (cancel before start) → `_sessions` empty, row `stopped`, next turn starts after `/resume`; the callback runs once per task and is a no-op on a `done` row; a newer turn's `_sessions` entry survives an older task's callback; album + `/kill` → buffer empty; `reset_streaming` after a simulated crash |
| BA-16 | R4 finding 1 | `telegram/ui/redact.py`: `redact(text: str) -> tuple[str, int]` (redacted text, number of replacements); `scan(text: str \| bytes) -> int` (never alters its input); `draft_visible(text: str) -> str` (redact the whole text, then cut at `max(last whitespace index, len − HOLD_BACK)`); `HOLD_BACK = 256`; `build(snapshot, env) -> Redactor` loads `DEFAULT_SECRET_PATTERNS` + `policy.secrets.extra_patterns`, the values of `TELEGRAM_BOT_TOKEN`, `GITHUB_WRITE_TOKEN`, `HERMES_API_KEY`, `OSSBOT_TOKEN_PEPPER`, `GITHUB_REVIEW_MIRROR_TOKEN` and `TELEGRAPH_ACCESS_TOKEN` when set, and the Kiro-key check (`policy.TOKENISH_RE` candidates whose sha256 is in `policy.kiro_key_hashes(KIRO_KEY_SHA256)`, active when `policy.secrets.kiro_key_hash_check`). At load it refuses to start (or a `/reload` is refused) when a secret value contains whitespace or is ≥ `HOLD_BACK` characters, when a pattern's minimum match width is ≥ `HOLD_BACK`, or (R5 finding 3) when an `extra_patterns` entry can match a whitespace character: `secret_scan.can_match_whitespace(pattern) -> bool` walks `re._parser.parse(pattern)` (Python 3.12) and flags `ANY` (`.`), the categories `SPACE`, `NOT_WORD`, `NOT_DIGIT` (`\s`, `\W`, `\D`), `NOT_LITERAL`, negated classes, and any literal or class member that is in `WHITESPACE = " \t\n\r\f\v\u00a0\u2028\u2029"`, recursing into groups, alternations and repeats; the refusal message names the entry and says multi-line secrets are covered by gitleaks on publish bytes. Replacement text is `common.redact.REDACTED`. **`draft_visible` is the only builder** for drafts, Stop partials and truncated answers (R5 finding 1): `chat/stream.py` builds the Stop partial as `draft_visible(text) + "…"` and the truncated answer as `draft_visible(text) + "…(truncated)"`, and stores the same string in `chat_turns.answer_text`. The PR title (card line, explorer section 9, `copy_text`) and prompt-quoted publish text are passed to `scan`, never to `redact` (R5 finding 2). The display WARNING is `redacted {surface, hits}`; the verbatim ERROR is `redaction_verbatim_hit {surface, count}`; neither logs the match | `test_redaction_paths.py` (AC 66, including Stop after the 2nd of 3 deltas of `HERMES_API_KEY` and the 200 KB cut); `test_secret_scan.py` (a unit test per load refusal, the whitespace table); the AST test of U14 (each listed module calls `redact` or `draft_visible`; `chat/stream.py` calls `draft_visible` on the Stop and truncation paths; the verbatim builders and the title renderers call `scan` and never `redact`) |
| BA-17 | R4 finding 6 | `AppContext.reissue_proposal(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]` and `AppContext.reissue_skill(self, skill: str, actor: str, *, keep_expiry: bool) -> list[str]` call the service and then `on_approval_created(id)` for each returned id after the service's transaction has committed. `AppContext.reissue_cards` does the same for the ids returned by `ProposalApprovals.reissue_invalidated` and `SkillApprovals.reissue_invalidated` | `test_reissue_part_counts.py` (wrapper cases); the BA-1 AST test |
| BA-18 | R4 findings 3–4 | Every registry command and `CONFIRMS[purpose]` handler is tagged `effect: Literal["local", "io"]` in the registry. `io` handlers (at least the `/hermes` surface probe, `/up`, `/down`, `/reload`, `/egress`, `/model`) consume the token and edit the reply to `⏳ …` inline, then spawn the effect with kind `command`; `local` handlers run inline as today | A registry test: every handler whose effect calls the root helper, the lifecycle client, the Hermes client or the proxy renderer is tagged `io` (AST check of the effect functions); the `/reload` case of `test_handlers_nonblocking.py` |
| BA-19 | R4 finding 5 | `telegraph.eligible(turn: ChatTurnRow, cfg: TelegraphConfig, reviews_thread_id: int \| None) -> str \| None` (`None` = eligible, else the failing rule's name for the INFO log). It is the only eligibility check and is called at render, at the `📖` tap, in the Confirm handler and in the `telegraph` task immediately before `createPage`, each time on a freshly read `chat_turns` row | `test_telegraph.py` (late feature card at each of the three refusal points; a grep test that no other module re-implements a rule) |
| BA-20 | R5 finding 6 | `telegraph.py: @dataclass class PublishTaskState: request_started: bool = False; outer_cancelled: bool = False`, one per `telegraph` task, shared by the task and its `on_done`. The task sets `request_started = True` immediately before `await asyncio.shield(inner)`. `on_done(task)`: when `task.cancelled()` and not `request_started`, enqueue the panel edit `Not published (stopped).`; when `task.cancelled()` and `request_started`, set `outer_cancelled = True` and enqueue nothing. `inner` writes `telegraph_pages`, then enqueues exactly one announcement: the §6.8 step 4 reply when `outer_cancelled` is false, else the panel edit `📖 Published (after stop) · public link · see /articles` (Unpublish: `🗑 Unpublished (after stop)`), with dedupe key `article:<page_path>:announce` (Unpublish: `article:<page_path>:unpublish_announce`; a page is unpublished at most once) | `test_telegraph.py` (`/kill` before the request; `/kill` during a held `createPage`; `/kill` during a held `editPage`; one announcement in each case) |
| BA-21 | R5 findings 2 and 4 | `gatekeeper/secret_scan.py: class SecretScanner` with `values: frozenset[str]` (the U14 gatekeeper secret values that are set, each ≥ 8 characters), `scan(text: str \| bytes) -> int`, `redact(text: str) -> tuple[str, int]`, built by `secret_scan.build(snapshot, env) -> SecretScanner` (the load checks of BA-16 live here) and held on `AppContext.secret_scanner`, rebuilt on a confirmed `/reload`. `policy.check_text(kind, text, bundle, confidentiality, kiro_key_sha256=None, *, secret_values: Collection[str] = ())` appends `Violation("gatekeeper_secret", kind)` when any value occurs in `text` (never the value in `detail`). Packaging adds `Violation("gatekeeper_secret", "patch")` when `ctx.secret_scanner.scan(derived.patch)` finds a value. `telegram/ui/redact.py` wraps `ctx.secret_scanner` and adds `draft_visible` | `test_secret_scan.py`; an AST test that every production call of `policy.check_text` passes `secret_values=` |
| BA-22 | R5 finding 7 | The U12 query (`api/notify.py`, `api/features.py`, reminders): `SELECT thread_id FROM chat_turns WHERE session_key = :k AND ((status = 'streaming' AND started_at >= :now_minus_30m) OR finished_at >= :now_minus_10m) ORDER BY turn_id DESC LIMIT 1`; the constants `U12_STREAMING_MAX = timedelta(minutes=30)` and `U12_FINISHED_GRACE = timedelta(minutes=10)` live in one module and are the only bounds used | `test_hermes_routing.py` (stuck `streaming` row at + 29 / + 31 min; finished row at 9 / 11 min) |

## 16. Out of scope
- Building the Mini App (ADR-0019 B), Telegram Serverless, webhooks and any inbound component.
- Inline mode, guest mode, business features, payments and Stars.
- Ephemeral messages (§8.3).
- Approving anything outside the Telegram card: GitHub reviews, Mini App, reactions, reply keyboards and voice are all excluded.
- A persistent reply keyboard. The menu button and dashboard cover it. It could be added later with fixed label → read-only command aliases.
- Multi-user support, groups and channels.
- Implementing a specific reverse-image-search provider (needs a user decision, CR-002), calendar, and the other phase-2 items in §13.
- Changing the approval/token core, the binding, or the Confirm sentences of the four outbound kinds (S1, S2). **Explicit exceptions:** the nine token-core deltas of §12.4, each listed with its test and AC:
  1. non-approval tokens with `approval_id NULL` and `{cid, rev, action_id}`;
  2. `disarm_expired`;
  3. per-kind `card_purposes()` with multi-use view tokens;
  4. the separate `SkillApprovals`/`ProposalApprovals` services and the `DecisionService` protocol;
  5. `reissue()` as the single `/approve` semantics;
  6. atomic bind (and prefix-aware part counting) in `mark_sent`;
  7. `reissue(…, part_count, keep_expiry)` with the eligibility predicate and the confirmed-sibling refusal, the `AppContext.reissue`, `reissue_proposal` and `reissue_skill` wrappers, `create(expires_at)`, and the `sweep_expired` reference and action cancel;
  8. `CardPublisher.recover()`;
  9. the invalidation fan-out at the existing call sites in `config_snapshot.py`, `control.py`, `housekeeping_once` and the tier switch.
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
15. Whether `editMessageText` can switch an existing message between rich and classic (either direction). The design doesn't depend on it: a failed rich edit always re-sends the view as a new classic message (§5.2, §14.5).
16. Which rich block types may be nested inside a rich `blockquote`/`expandable_blockquote` (tables, `pre`, `details`, lists), for the flat-mode and Reviews quote wrapper (U6). Where one can't be nested, `answer_view` sends that answer in classic form, which quotes everything.

## 18. Mockups (`docs/ux/`)
SVG phone screens (390 × 844 frame) with a constraints panel on each. Labels, rows, lengths and token sizes follow §11.2 and §11.4. Revision 2 updated 00–04 and 06–09 for the review findings and added 10 and 11 for CR-004. Revision 3 updated 00, 02, 03, 04, 05, 07, 08, 10 and 11 for review round 2 and added 12. Revision 4 updated 00, 04, 05, 07, 10, 11 and 12 for review round 3: flat-mode quoting, Show current resolution and Retry eligibility, the replace-text reply, the turn task model and Still answering, Skip/Later decline and Undo, Split without a pre-filled reply, link-card notes, and part counts on re-send. Revision 5 updated 07 (redaction and the draft hold-back, U14; turn cleanup when cancelled before start) and 12 (the `Gate 2 · approval cards` section name; proposal re-sends through `AppContext.reissue_proposal`) for review round 4. Revision 6 updated 01 (the PR title is publish bytes: escaped, scanned, never redacted; `📋 Copy title` copies it unchanged) and 07 (Stop partials and truncated answers use the same hold-back as drafts) for review round 5; the other mockups show nothing that round 5 changed.

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
| `11-size-filters.svg` | `/size` picker with per-repo overrides and the read-only ceiling; `/filters` pages 1–2 drawn, pages 3–4 summarized (4 pages); a pasted GitHub link → link card → `Working on it · 👤 requested by you`, and the AI-ban refusal card |
| `12-queue.svg` | `/queue`: Gate 2 (approval cards) and Gate 1 (proposals) sections, numbered `[📥 Open n]` buttons, sort toggles and pager; the `Re-sent 2 cards` toast and the kept expiry |

## 19. References (accessed 2026-10-18)
- Telegram Bot API reference: https://core.telegram.org/bots/api (rich messages and formatting options; `sendMessageDraft`; `EphemeralMessageParameters`; `InlineKeyboardButton.style`/`disabled`; `InlineKeyboardMarkup.force_reply`; `TextQuote`; `LinkPreviewOptions`; `setMessageReaction`; `createForumTopic`; `setMyCommands`; `setChatMenuButton`; `pinChatMessage`; `getFile` 20 MB; `getUpdates` and webhook exclusivity and 24-h retention)
- Bot API changelog (9.3–10.3): https://core.telegram.org/bots/api-changelog
- Bot features (streaming replies, topics in private chats, ephemeral messages, command scopes, menu button, AI agents): https://core.telegram.org/bots/features
- Bots FAQ (rate limits, file sizes): https://core.telegram.org/bots/faq
- Telegram Mini Apps (init data, Ed25519 third-party validation and public keys, `DeviceStorage`, `SecureStorage`, `SecondaryButton`, fullscreen, `themeParams`, `Serverless.call`): https://core.telegram.org/bots/webapps
- Telegram Serverless (handlers, endpoints, DB, Mini App hosting, platform-managed webhook): https://core.telegram.org/bots/serverless
- Telegram FAQ (cloud chats use server-client encryption; secret chats are end-to-end): https://telegram.org/faq
- python-telegram-bot 22.8 changelog (full Bot API 10.0 support): https://docs.python-telegram-bot.org/en/v22.8/changelog.html ; releases on PyPI: https://pypi.org/project/python-telegram-bot/
- python-telegram-bot 22.8 `ApplicationBuilder.concurrent_updates` (default: updates are processed one at a time). Checked on 2026-10-18 in the installed 22.8 source, `/opt/ossbot/app/.venv/.../telegram/ext/_applicationbuilder.py`, where the default processor is built with `max_concurrent_updates=1`; documentation: https://docs.python-telegram-bot.org/en/v22.8/telegram.ext.applicationbuilder.html#telegram.ext.ApplicationBuilder.concurrent_updates (§5.5)
- Hermes Agent API server (streaming events, tool progress, `input_image`, runs, stop, capabilities): https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
- Prior art: https://github.com/SanNw/opencode-telegram (README; `docs/en/security.md`) and https://github.com/RichardAtCT/claude-code-telegram
- GitHub fine-grained personal access tokens (resource owner, "only select repositories", only classic tokens write to public repos the user doesn't own, read access to public repos): https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- GitHub Mobile (review pull requests, edit files): https://docs.github.com/en/get-started/using-github/github-mobile
- Cloudflare Quick Tunnels (temporary, testing only, no SSE): https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
- Tailscale Funnel (`ts.net` names, ports 443/8443/10000, relays don't decrypt): https://tailscale.com/docs/features/tailscale-funnel
- Telegraph API (methods, no delete, `access_token`, 64 KB content, tag and attribute allowlist, Instant View): https://telegra.ph/api
- Telegram Mini Apps `sendData` ("only available for Mini Apps launched via a Keyboard button", ≤ 4,096 bytes, closes the app): https://core.telegram.org/bots/webapps
- Bot API `MessageGenerationStopped` (no `from`), `TextQuote.is_manual`, `ReactionTypeEmoji` list, `User.allows_users_to_create_topics`, `sendRichMessage` parameters: https://core.telegram.org/bots/api

Telegram library evaluation (revision 2; accessed 2026-10-18; details in ADR-0020, formerly ADR-0019 Addendum A):
- python-telegram-bot on PyPI (latest 22.8, 2026-06-12, LGPL-3.0-only): https://pypi.org/project/python-telegram-bot/ ; Bot API 10.1 tracking issue #5261 (open; native work paused 2026-06-18 "until we complete major refactors"; related PRs #5263, #5275, #5356, #5359): https://github.com/python-telegram-bot/python-telegram-bot/issues/5261 ; unknown-key handling in v22.8 `TelegramObject._de_json`: https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/_telegramobject.py
- aiogram on PyPI (latest 3.31.0, 2026-08-26, MIT; Bot API 10.1 in 3.29.0, 10.2 in 3.30.0, 10.3 in 3.31.0): https://pypi.org/project/aiogram/ and https://github.com/aiogram/aiogram/releases ; issue #1890 (10.3 `RichTextButton` → `ClientDecodeError`, polling blocked; closed "Update to v3.31"): https://github.com/aiogram/aiogram/issues/1890 ; issue #1840 (missing required field blocks polling; open) and PR #1848 (tolerant `getUpdates`; open): https://github.com/aiogram/aiogram/issues/1840 , https://github.com/aiogram/aiogram/pull/1848
- pyTelegramBotAPI on PyPI (latest 4.37.1, 2026-10-17, GPLv2; 4.35.0 = Bot API 10.1, 4.36.0 = 10.2, 4.37.0 = 10.3): https://pypi.org/project/pyTelegramBotAPI/ and https://github.com/eternnoir/pyTelegramBotAPI/releases
- Bot API changelog (10.3 = 2026-08-24, the latest): https://core.telegram.org/bots/api-changelog
- Pillow on PyPI (13.0.0, 2026-10-16; HEIC needs a plugin): https://pypi.org/project/Pillow/

## 20. Responses to design review round 1 (then in `docs/ux/cr003-review.md`, now replaced there by round 2) and to CR-004
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
| 13 | MED | Addressed (as ADR-0019 Addendum A, see above; filed as ADR-0020 in revision 4). Web-verified facts with URLs and access date for PTB, aiogram and pyTelegramBotAPI against every listed criterion; decision: keep PTB 22.8 + typed `rawapi.py` adapter; Pillow rejected for the gatekeeper | §1, §14.1, §14.4, §19, ADR-0019 Addendum A |
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

## 21. Responses to design review round 2 (`docs/ux/cr003-review.md`, `docs/ux/cr003-verdict.json`)
All 22 findings are **addressed** in revision 3; none is backlogged or ignored. Two deviations are in form only, and both keep the reviewer's intent:
- **Ids (finding 5).** The reviewer proposed `pa_`/`sk_`. The code's id format (`ossbot.common.ids.new_id` and `ID_RE = ^[a-z]_…`) accepts exactly one lowercase letter, so the ids are `p_` (proposal approvals) and `s_` (skill approvals). The skill decision rows also live in a new CR-003 table, `skill_approvals`, that references FEAT-009's `skill_reviews` row, instead of fixing FEAT-009's id format. FEAT-009 is being built concurrently, and this way CR-003 depends on no FEAT-009 column beyond the row id.
- **Too-large stub (finding 13).** The reviewer proposed the stub text "use 🧭 Explore". That would need a resolvable Explore token on a card whose approval must never be approvable. So the approval is invalidated `render_failed` (which makes an Approve impossible through the existing `issue_card_tokens` check), and the stub points to `/logs <cid>` instead.

| # | Sev. | Response | Where |
|---|---|---|---|
| 1 | MED | Addressed: per-kind table; decision purposes single-use, view purposes multi-use (`single_use=0`, approval expiry, `_kill_tokens`); `card_purposes(kind, has_interdiff)`; `comment`/`issue_claim` get Approve, Reject and Edit text (→ `edit_comment` ForceReply); `push_update` gets Explore and Files, no Edit text; view handlers use `check_token`; the late mirror row re-uses live view tokens | §5.1, §5.14, §12.2, §12.4 delta 3 + §12.4.1, AC 5, AC 44, mockup 05 |
| 2 | MED | Addressed: kit tokens carry `{cid, rev, action_id}`; exact resolution query by `action_id` (`a.status`, no ambiguous column); `tg_views.action_id` for rule 5; explorer/file/interdiff only for `open_pr`/`push_update`; proposal Details by `(cid, version)`; Show card re-sends every open card (`Re-sent N cards`), armed sibling back at S1 | §4.4 r5, §5.2, §5.15, §12.2, §12.4 deltas 1 and 5, §14.2, AC 45, mockups 04, 12 |
| 3 | MED | Addressed with the recommended seventh delta: `reissue(keep_expiry)` (per-action inheritance; fresh only when expired or under `confirm_ttl_seconds` left), `create(expires_at)`, and the `sweep_expired` reference moved to the window start so navigation can't postpone the sweep; Show card/`/queue → Open` refuse on expired actions so only Retry and typed `/approve` revive; the toggle/reload re-issue stays fresh (stated) | §5.2, §5.12, §5.13, §12.2, §12.4 delta 7 + §12.4.3, §16, AC 11, AC 46 |
| 4 | MED | Addressed: `CardPublisher.recover()` at `_connect` before polling and every housekeeping tick (card-sent-without-message-id; delivered-but-never-armed keyed on the absence of `arm:<aid>`), single-transaction `_card_sent`/`_maybe_arm`, part counting only in `mark_sent`; the wrong parenthetical replaced | §12.2 step 3, §12.4 delta 8 + §12.4.4, §14.5, AC 31 |
| 5 | MED | Addressed: `proposal_approvals` and `skill_approvals` tables; ids `p_`/`s_` (deviation above); `DecisionService` protocol + `DecisionServices.for_id`; fan-out at both reload branches, all four kill paths, housekeeping and the tier switch (dry-run toggle stays PR-only, stated); refusals (Start work: paused/killed/draining/down; skills: paused/killed); recompute at Approve, Confirm and before the effect; `control.py`/`config_snapshot.py`/`state_machine.py` listed as call-site-only; new `REASON_LABELS` | §5.12, §5.15, §9, §10, §12.2, §12.4 deltas 4 and 9 + §12.4.2, §14.1, §14.2, §14.5, AC 47 |
| 6 | MED | Addressed with the recommendation: Telegraph is not governed by dry-run; its confirm carries `🧪 DRY RUN (n/a: Telegraph is not GitHub)` / `🔴 LIVE (n/a: …)` and "This publishes now, even in dry-run mode."; Publish and Unpublish refused while paused or killed (at tap, Confirm and before the API call); S3 lists it | D17, S3, §6.8, §10, §14.5, AC 27 |
| 7 | MED | Addressed: U12/AC 35 narrowed to unsolicited messages; Reviews answers are classic with the gatekeeper `🤖 Assistant · answer to your message` header and the body in a quote, only `Use as comment`; Control/OSS-feed text is answered in 💬 Assistant with a silent pointer; U6 restated per thread kind; new AC with a `🧩 Ready for review` lookalike | D21, U6, U12, §4.4 r7–9, §6.7, AC 35, AC 48, mockups 00, 07 |
| 8 | NIT | Addressed: Confirm panel re-rendered with the same tokens when a comment is saved while armed; Confirm can't interleave (PTB sequential updates, verified in the installed 22.8); a disarm race is healed by re-enqueueing S1 | §5.5, §5.9, AC 30, mockup 03 |
| 9 | NIT | Addressed: rows with `bind_tokens_json` or `approval_id` are never held or merged; sent individually and silently | §7.3, AC 49 |
| 10 | NIT | Addressed: page 3 Age and activity, page 4 Languages and PRs (pager `i/4`), self-describing labels ≤ 12 where 3 share a row | §8.4, AC 50, mockup 11 |
| 11 | NIT | Addressed: `🚫 Kind: <name>` (≤ 24); the confirm names the kind in full | §5.15, AC 51 |
| 12 | NIT | Addressed: reuse counts messages per thread from the gatekeeper's own records (`tg_thread_seq`, incremented by `mark_sent` and routing) | §5.3, §12.2, §14.2, AC 52, mockup 02 |
| 13 | NIT | Addressed: every required block bounded (≤ 1,000 total); `ViewTooLarge` → `render_failed` + fixed stub without `approval_id`, ERROR (stub-text deviation above) | §5.12, §12.1, §14.5, AC 53 |
| 14 | NIT | Addressed: the formula, `est 0 code`, and the flag condition, with both mockup numbers as worked examples | §5.1, AC 54, mockup 10 |
| 15 | NIT | Addressed: mockup 03 uses the `confirm_text` strings and notes that code wins; §5.9 lists them | §5.1, §5.9, AC 6, mockup 03 |
| 16 | NIT | Addressed: `tg_views` kinds `skill_card`/`skill_part`; replies go to Hermes in Assistant with the skill as quoted context, never to `review_comments` | §4.4 r5, §9, §14.2, AC 55, mockup 08 |
| 17 | NIT | Addressed: a stale `/down` shows only its timeout picker with the age; "stop commands run" covers `/pause`, `/kill`, `/drain`, `/dryrun on` | §4.4, AC 16 |
| 18 | NIT | Addressed: "existing" dropped; `NotifyRequest.priority` (P1/P2; P0 → 422, gatekeeper-internal only); worker notices → 📈 OSS feed as P2, P1 allowed | §6.7, §14.1, §14.5, §14.6, AC 35 |
| 19 | NIT | Addressed: purpose classes; navigation stays read-only usable while the action is `approved/executing/done/dry_run_done`; comment/send answer "Submitted: comment on the PR instead." | §4.4 r5, §5.2, §12.2, AC 56, mockup 02 |
| 20 | NIT | Addressed in ADR-0019: `Armed --> Stale` and `Delivering --> Stale` with their causes | ADR-0019 state diagram |
| 21 | NIT | Addressed: the confirm panel adds "Only secrets, email, phone and name are checked — read the answer for other personal details first." | §6.8, AC 27 |
| 22 | NIT | Addressed: new `12-queue.svg` (Gate 2 and Gate 1 sections, `[📥 Open n]`, sort toggles, pager, toast) | §5.13, §18, mockup 12 |

## 22. Responses to design review round 3 (`docs/ux/cr003-review.md`, `docs/ux/cr003-verdict.json`)
All 16 findings are **addressed** in revision 4; none is backlogged or ignored. The exact signatures, key formats, type shapes and literal lists the findings asked for are recorded as build acceptance criteria BA-1 to BA-14 (§15.1), each with its test, so they are checked at build time and don't sit in prose. Revision 4 also files the library evaluation as **ADR-0020** (user requirement): keep python-telegram-bot 22.8 with a typed 10.1–10.3 adapter, reject aiogram and pyTelegramBotAPI, and add no Pillow to the gatekeeper. ADR-0019 keeps a back-reference.

Two choices refine a fix's wording and keep its intent:
- **Finding 5, eligible states for `comment`/`issue_claim`.** The fix lists `WATCHING`, `VETTED` and `PACKAGING`. The predicate adds `AWAITING_APPROVAL`, because a live claim beside an `open_pr` (created in `PACKAGING`, still open once the contribution reaches `AWAITING_APPROVAL`) and a reply beside a `push_update` must stay re-sendable together (AC 45). Every state the finding names as unsafe (`IMPLEMENTING`, `EXPIRED`) stays ineligible.
- **Finding 6, Undo.** The fix makes Undo depend on CR-004 adding the edges, with dropping Undo as the fallback. CR-003 adds the two human-only edges as an explicit CR-004 amendment (§5.15, §14.8), the same way it already amends DESIGN §14.5 and §12.5 and CR-002's egress lists, so Undo is specified unconditionally.

| # | Sev. | Response | Where |
|---|---|---|---|
| 1 | HIGH | Addressed. `reissue(…, part_count: Callable[[str], int], keep_expiry)` creates each approval with its own `body_parts_total`; the `body_parts_total=1` parameter is gone. The one wrapper `AppContext.reissue(cid, actor, *, keep_expiry)` passes `cards.part_count` and calls `on_approval_created` for each new id after the commit. All five entry points use it. `ProposalApprovals.reissue` (no parts) and `SkillApprovals.reissue` (skill part count) follow the same rule. `recover()` step 0 heals a failed publish. `test_reissue_part_counts.py` covers claim (1) + `open_pr` (3) with part 2 failing once | §5.2, §5.12, §5.13, §8.1, §12.2, §12.4 deltas 5 and 7, §12.4.3, §12.4.4, §14.1, §14.5, §16, AC 57, BA-1, ADR-0019 Option A, mockups 04, 12 |
| 2 | HIGH | Addressed. New normative §12.5 "Update handler budget". Handlers do only DB work, outbox enqueues and local reads, plus at most one Bot API call besides `answerCallbackQuery`. Hermes turns, media download and stripping, Telegraph publish and unpublish, GitHub reads for `/work` and link cards, and document rendering run as tracked tasks through `Handlers.spawn(coro, kind)`, like `_run_submit`. There is one `asyncio.Lock` per `session_key` (`Still answering — tap ⏹ Stop or wait`). `/kill` and `drain()` cancel the chat-side kinds (`chat_turns.status='stopped'`); Telegraph is cancelled only before its request starts. `stopped_message_generation` ends the stream within 2 s. The comment save and Confirm-panel refresh stay inline, so §5.5 holds. The Confirm live preflight stays inline (reviewed order) but is bounded to 8 s. A handler timer turns any breach into a test failure. New AC 59 with `tests/telegram/test_handlers_nonblocking.py` | D14, §5.5, §6.1 steps 0 and 5, §6.5, §6.8, §5.19, §8.1, §12.5, §14.1, §14.5, §14.7, AC 15, AC 18, AC 59, BA-2, ADR-0020, mockup 07 |
| 3 | MED | Addressed. Purpose `stale_show_current`, arg `{cid}` / `{cid, gate: 1}` / `{skill}`, resolved by contribution with the reviewer's query → `AppContext.reissue(cid, keep_expiry=True)`; else the newest open proposal approval → `ProposalApprovals.reissue`; skills → `SkillApprovals.reissue`; else the toast `No open card for c_… (state X)`. The button is rendered only when such an approval exists. AC 10 extended (r2 → r3, v1 → v2) | §5.12, §5.16, §12.2, §14.6, §14.7, AC 10, BA-11, ADR-0019 S12 row and diagram, mockup 04 |
| 4 | MED | Addressed. Rule 5 is split by action kind. `open_pr`/`push_update` replies are review comments as before. `comment`/`issue_claim` replies open `Replace the comment text with your reply? [✅ Replace] [↩️ Cancel]`; Replace runs FEAT-005's `edit_comment` path (normalization, `policy.check_text`, a new card, the old one S12 `text_edited`). `use_as_comment` is offered only when the single open approval is `open_pr` or `push_update`. New AC 60 | §4.4 rules 5 and 7, §12.2, §14.2, §14.5, §14.6, §14.7, AC 60, BA-13, ADR-0019 Option A, mockup 05 |
| 5 | MED | Addressed. One predicate, `reissue_eligible`, used by `reissue`, `stale_retry`, typed `/approve`, Show current and comment replace: `pending_approval` (the never-written `approved` is dropped) and `AWAITING_APPROVAL` for `open_pr`/`push_update`, or the kind's states for `comment`/`issue_claim` (deviation above). Ineligible actions are skipped, and `Nothing to re-send (state X)` is shown when none remain. Any `confirmed` approval of the cid refuses the re-send with `Submitting… try again in a moment`, and a re-send never invalidates a confirmed approval. The move to `EXPIRED` cancels the `pending_approval` actions, so S12 drops Retry. `test_reissue_eligibility.py`; AC 58 | §5.2, §5.12, §8.1, §12.2, §12.4 delta 7, §12.4.3, §14.1, §14.5, AC 58, ADR-0019 Option A, mockups 04, 12 |
| 6 | MED | Addressed. Skip, Later and Never (after its confirm) call `ProposalApprovals.reject(approval_id, reason ∈ {skipped, snoozed, never_repo, never_kind})`: status `rejected` with `rejected_reason`, `_kill_tokens`, contribution → `PROPOSAL_REJECTED`/`PROPOSAL_SNOOZED` (actor human, audited). Undo is a single-use kit token, 10 min, for Skip and Later only. It uses the two new human-only CR-004 edges back to `PROPOSED`, removes the record, then calls `ProposalApprovals.reissue(cid, keep_expiry=True)` (a new `p_` approval with the old expiry). New AC 61 | §5.15, §12.4.2, §14.1, §14.2, §14.5, §14.7, §14.8, AC 61, BA-12, ADR-0019 diagram, mockup 10 |
| 7 | MED | Addressed. In flat mode every Hermes answer and unsolicited Hermes message gets the gatekeeper `🤖 <b>Assistant</b> · …` header and its whole body in one quote (rich: one `blockquote`/`expandable_blockquote` holding every body block; classic: `<blockquote expandable>`). Follow-up and feature buttons stay. U6 and U12 are restated as "separation by rendering, not by topic", and `docs/SETUP.md` recommends topics. AC 48 has a flat-mode case. §17 item 16 checks rich nesting, with a classic fallback | D21, U6, U12, §4.1, §6.7, §14.1, §14.7, §17, AC 48, BA-14, mockups 00, 07 |
| 8 | NIT | Addressed. The disarm edit's key is `disarm:<aid>:<armed_at>`. §12.2 states that dedupe keys are permanent and that repeatable edits need a per-occurrence component; the full key list is BA-3 | §5.5, §5.9, §12.2, §14.7, BA-3 |
| 9 | NIT | Addressed. The §7.1 P2 quiet-hours cell reads "held until quiet hours end, then batched (§7.3)" | §7.1 |
| 10 | NIT | Addressed. `CardPublisher` resolves `set_card_message`, `issue_card_tokens`, the renderer and the keyboard through `ctx.decisions.for_id(aid)` (protocol methods added). `AppContext.card_recover: Callable[[], None]` is set in `TelegramFrontend.start`, and `housekeeping_once` calls it | §12.4.2, §12.4.4, §14.1, BA-4 |
| 11 | NIT | Addressed. `Invalidated(approval_id: str, action_id: str \| None, cid: str \| None)`; `reissue_cards` skips `None` | §12.4.2, §14.1, BA-5 |
| 12 | NIT | Addressed. Every view maps to a listed kind (pickers and lists → `picker`, panels → `confirm_prompt`, others → `fyi`; table in §14.2). `ViewKind` is a pydantic `Literal` mirroring the CHECK and validated at enqueue. Tested by `test_view_kinds.py` | §12.2 step 3, §14.2, §14.7, BA-6 |
| 13 | NIT | Addressed. Split sends the fixed feedback directly, like Make it smaller, then an optional ForceReply with the placeholder `Anything to add? (optional)` (27 characters). New AC 62 | §5.18, AC 62, BA-7, mockup 10 |
| 14 | NIT | Addressed. With `forward_origin` set the notes are empty; otherwise the link card shows `Notes: "<first 80 chars>"`. New AC 63 | §5.19, §14.6, AC 63, BA-8, mockup 11 |
| 15 | NIT | Addressed. On a 400 for a rich edit, the classic view is sent as a new reply to the card, the old message is stubbed (or deleted), and `tg_views` is updated. §17 item 15 checks whether `editMessageText` can switch between rich and classic; the design doesn't depend on the answer. New AC 64 | §5.2, §14.5, §14.7, §17, AC 64, BA-9 |
| 16 | NIT | Addressed with the reviewer's first option. Eligibility is "the thread is not ✅ Reviews and the turn produced no feature card", using the new `chat_turns.feature_cards`, which `api/features.py` increments. New AC 65 | §6.8, §14.1, §14.2, AC 65, BA-10 |

## 23. Responses to design review round 4 (`docs/ux/cr003-review.md`, `docs/ux/cr003-verdict.json`)
All 8 findings (2 MEDIUM, 6 NIT) are **addressed** in revision 5; none is backlogged or ignored. As in round 3, exact signatures, constants and literal lists are build acceptance criteria (BA-15 to BA-19, and BA-1/BA-2 extended, §15.1), each with its test. The library and Pillow decisions of ADR-0020 are unchanged (keep python-telegram-bot 22.8 with the typed `rawapi.py` adapter; no Pillow in the gatekeeper); ADR-0020 records that the round-4 changes are library-independent.

Two choices refine a fix's wording and keep its intent:
- **Finding 2, the session lock.** The fix says "releases the session lock if held". Releasing an `asyncio.Lock` from a done callback has a race: the old task's callback can run after a newer turn of the same session has taken the lock, and would release the newer turn's lock. So the lock is replaced by a **session map** `session_key → Task`, and the callback deletes the entry only when it is this task. Nothing ever waited on the lock, so the behaviour (one turn per session, `Still answering` otherwise) is unchanged. The fix's other parts (`spawn(…, on_done)`, the callback that always runs, idempotent `stopped`/`failed` closing, the media buffer) are applied as written. A startup reset of leftover `streaming` rows is added, so U12's check is bounded even after a crash.
- **Finding 1, documents.** The fix lists log documents and `answer.md` as redacted. `.patch`, `pr_body.md` and the HTML diff's code are verbatim publish bytes, like the `<pre>` parts, so they get the fix's verbatim rule (scan, never alter; a hit is a bug → `render_failed` and a withheld stub) rather than redaction. The withheld stub has its own fixed text, so it isn't mistaken for the `ViewTooLarge` stub.

| # | Sev. | Response | Where |
|---|---|---|---|
| 1 | MED | Addressed. New invariant **U14 Redaction before any send** with one choke point, `telegram/ui/redact.py` over `common.redact.Redactor`: policy secret patterns, the gatekeeper's secret values (`TELEGRAM_BOT_TOKEN`, `GITHUB_WRITE_TOKEN`, `HERMES_API_KEY`, `OSSBOT_TOKEN_PEPPER`, mirror and Telegraph tokens) and the Kiro-key hash check. Applied to every untrusted leaf in `classic.py`/`rich.py`, every draft, the final and Stop answers, `answer.md`, log tails and documents, follow-up items (dropped on a hit) and `chat_turns.answer_text` before storage. Streaming: redact the whole text, then hold back the trailing incomplete token (`max(last whitespace, len − 256)`), with load-time checks that every secret value and pattern minimum is below 256. Verbatim publish bytes are scanned, never altered; a hit is ERROR + `render_failed` + stub. New AC 66 and `test_redaction_paths.py` (secrets split across 3 deltas; no ≥ 8-char prefix anywhere) | D8, U1, U14, §5.2 section 7, §5.4, §6.1 steps 3–5, §6.4, §6.8, §12.1, §14.1, §14.5, §14.7, §14.8, AC 66, BA-16, mockup 07 |
| 2 | MED | Addressed (session-map refinement above). `spawn(coro, kind, *, session_key=None, on_done=None)` registers one done callback that always runs, cancel-before-start included: it clears the task index, frees the session if the entry is this task, and calls `on_done`. `chat_turn`'s `on_done` (`finalize_turn`) sets `stopped`/`failed` and `finished_at` in one transaction, guarded by `status='streaming'`; `media` drops the album buffer; `telegraph` marks its panel. The coroutine's `finally` is no longer load-bearing. Startup resets leftover `streaming` rows. New AC 67, BA-15, and the batch case in `test_handlers_nonblocking.py` | §6.1 step 0, §6.5, §6.7, §12.5 items 2, 3 and 7, §14.1, §14.5, §14.7, AC 67, BA-15, ADR-0020 |
| 3 | NIT | Addressed. `TaskKind` gains `command` for registry command and confirm effects that await I/O beyond SQLite (the `/hermes` surface probe, root-helper and lifecycle requests of `/up`, `/down`, `/reload`, `/egress` including the proxy re-render, the `/model` writer). It edits the reply when done and is not cancelled by `/kill` (Phase 1 kill rules) | §8.1, §12.5 items 2 and 4, §14.1, §14.5, AC 59, BA-2, BA-18 |
| 4 | NIT | Addressed. D14 now says ≤ 2 s while a Hermes answer streams and ≤ 8 s during a Confirm live preflight. The handler timer uses `PREFLIGHT_TIMEOUT_S + 1` for the Confirm handler and `HANDLER_SLOW_S` for every other handler. The §8.1 `/kill` row lists `render` (and says `submit` and `command` keep their rules) | D14, §8.1, §12.5 item 6, §14.5, AC 59, BA-2 |
| 5 | NIT | Addressed. One `telegraph.eligible(turn_row)` re-evaluates the full eligibility from the current `chat_turns` row at render, at the `📖` tap, at Confirm and right before `createPage`, refusing with `Not published: this answer is no longer eligible`. AC 65 and `test_telegraph.py` gain the late-feature-card case | §6.8, §14.1, §14.5, §14.7, AC 65, BA-19 |
| 6 | NIT | Addressed. `AppContext.reissue_proposal(cid, actor, *, keep_expiry)` and `AppContext.reissue_skill(skill, actor, *, keep_expiry)` call the service and then `on_approval_created` for each new id after the commit; the services never publish. Every caller (Show current, `/queue → Open`, Undo, `/skills → Open card`) uses them, and `reissue_cards` publishes the reload re-issue the same way. BA-1's AST test covers all three services. Also fixed while there: the proposal and skill reload re-issue keeps the expiry, which §12.4.2 already said and §12.4.3 contradicted | §5.12, §5.13, §5.15, §8.1, §12.2, §12.4.2, §12.4.3, §14.1, §14.7, AC 57, BA-1, BA-17, ADR-0019 Option A, mockup 12 |
| 7 | NIT | Addressed. §14.8 lists the ADR-0004 amendment note (follow-up buttons D9, rich answers D8/U1, `stopped_message_generation` with `authorized_chat_only()` U10, Approve-after-parts restated for re-sends, delta 7), plus a DESIGN §14.3 pointer to U14 | §14.8 |
| 8 | NIT | Addressed. The section is `Gate 2 · approval cards (3)` in §5.13 and in mockup 12, whose layout is unchanged | §5.13, §18, mockup 12 |

## 24. Responses to design review round 5 (`docs/ux/cr003-review.md`, `docs/ux/cr003-verdict.json`)
All 8 findings (2 MEDIUM, 6 NIT) are **addressed** in revision 6; none is backlogged or ignored. Exact signatures, constants, key formats and queries are build acceptance criteria (BA-20 to BA-22 new, BA-3 and BA-16 extended, §15.1), each with its test. The library and Pillow decisions of ADR-0020 are unchanged (keep python-telegram-bot 22.8 with the typed `rawapi.py` adapter; no Pillow in the gatekeeper, only in `--network none` sandbox images); none of the round-5 fixes depends on the library.

Three choices pick one of the options a fix offered, or make it concrete:
- **Finding 3, whitespace-capable patterns:** *refuse at load* (not "warn and exclude"). The no-prefix guarantee then has no exceptions to document per pattern, and nothing is lost: today's `extra_patterns` are the four whitespace-free defaults, and multi-line secrets such as PEM blocks are caught on publish bytes by gitleaks.
- **Finding 4, where the scanner lives:** a shared `gatekeeper/secret_scan.py: SecretScanner` (not packaging importing `telegram/ui/redact.py`, which would make the packaging layer depend on the Telegram UI). `policy.check_text` gets a `secret_values` keyword, so every edit path that already calls it (S7 title and body, body as file, `edit_comment`, comment replace) refuses a gatekeeper secret value too, not only packaging.
- **Finding 6, the race between the cancel and the write:** the shielded section always sends the one announcement itself, choosing between the normal reply and the `(after stop)` panel edit by the `outer_cancelled` flag that `on_done` sets. So whichever way the cancel races the write, exactly one announcement is sent.

| # | Sev. | Response | Where |
|---|---|---|---|
| 1 | MED | Addressed. Every text that ends a stream early is built with `draft_visible()`: the Stop partial is `draft_visible(partial)` + `…` with `⏹ Stopped by you`, a 200 KB / 1 MB truncation is `draft_visible(text)` + `…(truncated)`, and the same string is stored in `chat_turns.answer_text`. Only a stream that completed normally uses `redact()` alone. U14's Streaming sentence says so. BA-16: `draft_visible` is the only builder for those paths, and the U14 AST test checks `chat/stream.py` calls it on both. AC 66 and `test_redaction_paths.py` gain "Stop tapped after the 2nd of 3 deltas of `HERMES_API_KEY`" and a 200 KB cut inside a `ghp_` token | U14, §6.1 steps 4–5, §14.1, §14.5 (stream truncation and stop-call rows), §14.7, AC 18, AC 66, BA-16, mockup 07 |
| 2 | MED | Addressed. The PR title wherever it is shown (card `PR title:` line, explorer section 9, `📋 Copy title`) and publish text quoted back in a prompt (the S7 Title/Body ForceReply, the `edit_comment` prompt) are in U14's verbatim list, §12.1 `scan()`, §5.4 and ADR-0019 Option A: `scan()`ed, never redacted; a hit follows the existing rule (ERROR, `render_failed`, `Card withheld` stub, no Approve). U8 now reads "(the `<pre>` parts and the PR title; the title is shown escaped in `<code>`, made visible but never defused or redacted, and `copy_text` carries the stored title unchanged)". AC 66: a secret value planted in the title withholds the card, and the title is never shown redacted | U8, U14, §5.1 notes, §5.2 section 9, §5.4, §12.1, §14.5, §14.7, AC 66, BA-16, ADR-0019 Option A, mockup 01 |
| 3 | NIT | Addressed (refuse at load, above). BA-16 refuses any `extra_patterns` entry that can match a whitespace character (`secret_scan.can_match_whitespace` over `re._parser`: `.`, `\s`, `\W`, `\D`, negated classes, `NOT_LITERAL`, whitespace literals and class members). §6.1 step 3 and U14 state that the guarantee covers secret values and whitespace-free patterns, that the Kiro-key hash check is a final-text backstop, and that the `ksk_` pattern covers Kiro keys while streaming | U14, §6.1 step 3, §14.7 `test_secret_scan.py`, BA-16 |
| 4 | NIT | Addressed (shared scanner, above). `policy.check_text(…, *, secret_values)` reports `Violation("gatekeeper_secret", <kind>)` for title, body, commit message and comment text, and packaging scans the patch (`Violation("gatekeeper_secret", "patch")`), so such a value takes the normal policy-violation path. A render-time `scan()` hit is then really a bug | U14, §5.1 notes, §5.4, §12.1, §14.1 (`secret_scan.py`, `policy.py`, `packaging.py`), §14.5, §14.7, BA-21 |
| 5 | NIT | Addressed. §5.5 now says "The `command` kind can invalidate PR approvals (`/reload`) while a comment handler awaits its edit; safety rests on the binding recheck." The re-read rule re-renders the approval's current state: S1 when `pending` (`s1:<aid>:<comment_id>`), S12 when invalidated or expired (`CardPublisher.render_stale`, `stale:<aid>:<comment_id>`). BA-3 lists the new key; AC 30 and `test_dedupe_keys.py` gain the `/reload` race | §5.5, §14.1 (`bot.py`), §14.7, AC 30, BA-3 |
| 6 | NIT | Addressed. The `telegraph` task sets `request_started = True` just before entering the shield; `on_done` enqueues `Not published (stopped).` only when it is false, else sets `outer_cancelled`; the shielded section, after the `telegraph_pages` write, sends `📖 Published (after stop) · public link · see /articles` when `outer_cancelled` is set (Unpublish: `🗑 Unpublished (after stop)`). New AC 68, BA-20, `test_telegraph.py` cases | §6.8 step 4, §12.5 item 2 (`on_done` and the kind table), §14.5 (two `/kill` rows), §14.7, AC 68, BA-3, BA-20 |
| 7 | NIT | Addressed. The U12 check is `(status='streaming' AND started_at >= now − 30 min) OR finished_at >= now − 10 min`, in §6.7, §14.6, the §14.5 rows and `test_hermes_routing.py` (stuck row at + 29 / + 31 min) | §6.7, §14.5, §14.6, §14.7, AC 67, BA-22 |
| 8 | NIT | Addressed. The §14.8 ADR-0004 amendment note and a DESIGN §8.4 T5 amendment both gain: "View and navigation tokens are multi-use until the approval ends (or 24/72 h for kit views), still message-bound, owner-only and opaque; every **decision** token (Approve, Confirm, Cancel, Reject, Start work, skill activation, Publish) stays single-use (CR-003 §12.4 delta 3)." | §14.8 |

