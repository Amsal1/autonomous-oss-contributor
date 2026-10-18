# CR-003: Telegram user experience and the PR review flow

Status: Proposed (user request, 2026-10-18). Applies on top of DESIGN.md, CR-001 and CR-002 including Amendment A. Where this CR conflicts with DESIGN §14 (Telegram design) or ADR-0004, this CR wins and both must be updated to match. The review-surface decision is **ADR-0019**. Mockups, one per key screen, are in `docs/ux/` (index in §18).

## 0. Summary
The bot is Amsal's daily driver for two things: approving OSS contributions and using Hermes as a general multimodal assistant. Today the UX is a flat chat with one decorated card per candidate and escaped text replies. This CR makes it a structured workspace:
- **Topics:** Control, Reviews, OSS feed and Assistant, plus user-created chat topics.
- **A pinned live dashboard.**
- **A review flow you can do on a phone:** explorer, per-file diffs, line comments, interdiff, a mode-labelled two-tap confirm, and a live PR status card.
- **A streaming assistant** with sources, follow-ups and feature cards.
- **Notification discipline:** priorities, quiet hours, batching and a digest.
- **Pickers instead of typed arguments.**
- **Clear offline and error states.**
- **A small UI component kit** so later features render consistently.

The deterministic approval path is unchanged: the same tokens, binding and Confirm. The default needs zero extra setup. Richer layers, the private GitHub review mirror (ADR-0019 option C) and phase-2 items, are opt-in.

## 1. What the research changed
Model knowledge was stale. Everything below was checked against the live documentation on 2026-10-18 (sources in §19). "PTB" means python-telegram-bot 22.8, the pinned version, which fully supports Bot API 10.0. Newer methods and fields go through `Bot.do_api_request`, which the outbox already uses for every call (`telegram/bot.py: TelegramSender`).

| Capability (Bot API version) | What ossbot uses it for | PTB 22.8 | Limits and notes |
|---|---|---|---|
| Rich messages: `sendRichMessage`, `sendRichMessageDraft`, `editMessageText(rich_message)` (10.1–10.3) | Read-only views: explorer, file diffs, interdiff, tests, assistant answers, feature cards | **No**: raw call | ≤ 32,768 UTF-8 characters, ≤ 500 blocks, ≤ 16 nesting levels, ≤ 50 media, ≤ 20 table columns. Three input forms: `blocks`, `html` or `markdown`. Rich Markdown "can contain arbitrary HTML", including `<tg-button>`, so **only `blocks` is used** for interpolated content (U1). Entities are auto-detected unless `skip_entity_detection` is set |
| Rich blocks: `details`, `expandable_blockquote`, `pre` + `language`, `table`, `anchor`, `buttons` (10.2–10.3) | Collapsible hunks and logs, numstat tables, hunk anchors | No | In-body `tg-button`s are **not used** for any decision (U3) |
| `sendMessageDraft` (9.3; all bots since 9.5) with `can_stop`/`keep_on_stop` (10.3); `stopped_message_generation` update (10.3) | Assistant streaming with a Stop button | Draft yes; `can_stop` and the update **no** | Private chats only. The draft is a **30-second ephemeral preview**: the final text must be sent with `sendMessage`/`sendRichMessage`. Draft text is 0–4,096 characters; empty text shows "Thinking…" |
| Topics in private chats: `createForumTopic`, `message_thread_id`, `getMe.has_topics_enabled` (9.3/9.4) | Information architecture (§4.1) | Yes | Enabled per bot in @BotFather. Topic names are 1–128 characters. The bot can stop users creating or deleting topics |
| Button `style` = `success`/`danger`/`primary` (9.4) | Approve green, Confirm LIVE red, Kill red | Yes | Colour is never the only signal (§11) |
| `DisabledButton` (10.3) | Stale cards ("⛔ Not approvable") | **No**: raw | |
| `force_reply` on `InlineKeyboardMarkup` (10.3) | Comment prompts that also need buttons | **No**: raw | Can't be changed by a later keyboard edit |
| `date_time` entity / `<tg-time>` (9.5) | Every timestamp; relative "updated 3 min ago" that keeps ageing while the laptop sleeps | Parsed server-side | Format `r` (relative) or `w?[dD]?[tT]?` |
| Expandable blockquote, `pre` with a language (classic HTML) | Classic fallback for all views | Yes | Languages per libprisma (includes `diff`) |
| Ephemeral messages (10.2/10.3) | **Not used** (§8.3) | No | Designed for groups. `message_id` is 0, delivery is not guaranteed, and they may disappear on app restart |
| Guest mode (10.0), inline mode | Off (phase-2 hook, §13) | Yes | |
| `setMessageReaction` | Acknowledge received media with 👀 / ✅ | Yes | One reaction per message for bots |
| `message_effect_id` | Optional 🎉 on "merged" | Yes | Private chats only. Effect IDs are not documented in the Bot API; **[VERIFY]** |
| Checklists (`sendChecklist`) | **Not usable** | Yes | Business accounts only. Rich task-list items are used for display instead |
| Menu button, `setMyCommands` with 7 scopes | Owner-scoped command menu that changes with control state | Yes | ≤ 100 commands; command 1–32 `[a-z0-9_]`; description 1–256 |
| Pinned messages | Live dashboard | Yes | Any message can be pinned in a private chat; pin notifications are always off there |
| `copy_text` button | Copy title, branch, PR URL | Yes | 1–256 characters |
| `TextQuote` on replies | Line-level review comments | Yes | `text` + `position` (UTF-16) |
| General limits | | | Text 4,096 and caption 1,024 characters after entity parsing; `callback_data` 1–64 bytes; `answerCallbackQuery` text ≤ 200; about 1 message/s per chat; unfetched updates kept ≤ 24 h; bot download ≤ 20 MB, upload ≤ 50 MB |
| Mini Apps | Phase-2 only (ADR-0019 B3) | Partial | Init data is verified by HMAC with the bot token **or** by Ed25519 `signature` with Telegram's public key (no token needed). `DeviceStorage` 5 MB per user; `SecureStorage` 10 items (Keychain/Keystore). Methods are refused from foreign origins since 2026-07-20 |
| Telegram Serverless (2026-10-06) | **Rejected** for this bot | n/a | BotFather manages a webhook, and `getUpdates` is refused while a webhook is set. That breaks ADR-0004 (ADR-0019) |
| Telegraph API (`api.telegra.ph`) | Opt-in public articles for long assistant answers only (§6.8) | n/a (plain HTTPS) | Pages are **public to anyone with the URL**. The page path is derived from the title and date (`Title-MM-DD`), so URLs are guessable unless the title carries randomness. Methods: `createAccount` (returns an `access_token`), `createPage`, `editPage`, `getPage` (no token), `getViews`, `revokeAccessToken`. There is **no delete**: blanking via `editPage` is the only option. Content ≤ 64 KB; title 1–256 characters. Tags: `a aside b blockquote br code em figcaption figure h3 h4 hr i iframe img li ol p pre s strong u ul video`. The only attributes are `href` and `src`, so there is no syntax highlighting, no tables and no collapsible blocks. Pages open as Instant View inside Telegram |

Prior art:
- `SanNw/opencode-telegram` keeps the agent server on loopback. It tunnels only an authenticated Bridge, protected by Telegram-signed init data, an owner check and a P-256 trusted-device key, and its HTML previews are static and sandboxed.
- `RichardAtCT/claude-code-telegram` uses an allowed-users list and an optional inbound webhook server.

ossbot keeps the owner-only rule and the static, script-free previews. It does **not** add an inbound component by default.

## 2. Decisions
- **D1 Topics.** When `getMe().has_topics_enabled` is true (the user turns topics on in @BotFather), the gatekeeper creates and uses four system topics: 🚦 Control, ✅ Reviews, 📈 OSS feed, 💬 Assistant. A topic the user creates is a separate Hermes conversation. With topics off, everything works in one flat chat with section headers (§4.1).
- **D2 Commands.** Menu button = `MenuButtonCommands`. Commands are set only for `BotCommandScopeChat(owner)`; `BotCommandScopeDefault` is empty. The menu is re-set when control state changes; in killed mode it holds only `/status /resume /logs /help`.
- **D3 Dashboard.** One pinned message in Control, edited in place: at most once per 30 s and only when its content hash changes (§4.3).
- **D4 Review surface (ADR-0019).** Native Telegram by default: classic card plus rich read-only views with classic fallback. The private GitHub mirror is opt-in. The Mini App is phase-2. Serverless is rejected.
- **D5 Approval surfaces.** The approval card and Confirm stay classic HTML messages whose reply-markup tokens are bound to the message ID. They are never rich, never drafts, never ephemeral, and never use in-body buttons (U3).
- **D6 Comments.** Review comments are reply/quote messages and hunk-picker prompts. They collect into a draft review and are sent as one change request.
- **D7 Interdiff.** Every revised candidate shows a gatekeeper-computed interdiff first. It is display-only.
- **D8 Assistant streaming.** `sendRichMessageDraft` (or `sendMessageDraft` when rich is unavailable) with `can_stop`. The final message is sent as a rich message built from Hermes Markdown by our own converter, never through Telegram's Markdown or HTML modes (U1).
- **D9 Follow-up buttons.** Allowed only as gatekeeper tokens whose single effect is to send the button's text to Hermes as the user's next message. This amends ADR-0004 ("never with buttons") (U2).
- **D10 Feature cards.** Hermes supplies typed data; the gatekeeper validates it and renders the card. Hermes never supplies markup or buttons.
- **D11 Notifications.** Four priorities (P0–P3), quiet hours, `/quiet` and `/notify`, coalescing and batching, plus a daily and weekly digest. P0 is never silenced (U7).
- **D12 Arguments.** Every command argument has an inline-keyboard picker. Typed arguments still work and are validated the same way.
- **D13 Confirmations.** Destructive and "go" actions use in-place confirm panels and toasts. Ephemeral messages are not used (§8.3).
- **D14 Kill.** Typed `/kill` stays immediate. The dashboard 🛑 button needs a second tap within 10 s.
- **D15 Offline signal.** The pinned dashboard's relative `<tg-time>` keeps ageing on the client while the laptop is off. On a graceful `down` the bot's short description says it is offline. On reconnect, pending updates are processed with their age shown.
- **D16 Component kit.** All rendering goes through `ossbot.gatekeeper.telegram.ui`: card, list, pager, confirm, picker, progress, toast, doc, rich builder. Features register through an entry-point registry.
- **D17 Telegraph articles** (user suggestion, evaluated in §6.8). Off by default (`telegraph.enabled: false`). When enabled, `📖 Open as article` is offered **per message** only on eligible assistant answers, and publishing needs a tap plus a confirm tap. The gatekeeper publishes through a new egress category, after secret and personal-data scans. Telegraph is never used for review content, digests, images, health or nutrition data, or anything else. There is **no automatic publishing**: a length threshold only decides whether the button is offered. This overrides the "automatic above a threshold" option, because a public, undeletable publication must be a deliberate act, and rich messages already show answers up to 32,768 characters in the chat.

## 3. Safety invariants
Unchanged and binding (DESIGN I1–I9, ADR-0004, ADR-0014):
- **S1** Approve, submit, resume, dry-run off and skill activation are deterministic gatekeeper actions. Each is two taps (Approve, then Confirm) on opaque `t:<22 chars>` tokens (24 bytes): single-use, expiring, bound to `tg_message_id`, and accepted only when `from.id` is the owner in the owner's private chat. No LLM output, chat text, Mini App, GitHub review or mirror event can approve.
- **S2** Every approval binds the exact commit SHA, the derived patch hash and the title/body hashes, plus the mode (`dry_run` recomputed at Approve, at Confirm and before every write). Any edit, dry-run toggle, reload or new rev invalidates it.
- **S3** Nothing public happens before approval. Dry-run is on by default, and the mode is printed on every card and every Confirm sentence.
- **S4** No inbound exposure by default. Any internet-reachable component (phase-2 Mini App) must meet all of these: optional, off by default, read-only, Ed25519 init-data verification, no secrets, no bot token, never able to approve.

New in this CR. Each invariant names the layer that enforces it:

| # | Invariant | Enforced by | Why there |
|---|---|---|---|
| U1 | **Untrusted text is data, never markup.** This covers Hermes output, issue and PR text, logs, skill text, mirror comments and feature payloads. Classic messages escape it (`escape.esc`, `make_visible`, `defuse_commands`). Rich messages carry it as plain strings in `blocks` with `skip_entity_detection: true`. Telegram `markdown`/`html` rich modes and MarkdownV2 are never used with interpolated content | `telegram/ui/` renderers; an import-linter contract keeps `gatekeeper.chat` and `gatekeeper.features` from calling the outbox except through `telegram.ui` | One choke point that can be tested exhaustively |
| U2 | **Only the gatekeeper mints buttons.** Every `callback_data` is a gatekeeper token. Labels from untrusted sources are sanitized (§11.3) and never contain approval words. A follow-up token can only relay its stored text to Hermes; it never reaches the command registry | `telegram/ui/kit.py` `button()`, `chat/followups.py` | Hermes could otherwise forge a convincing "Approve" |
| U3 | **Approval and Confirm surfaces are classic messages with reply-markup tokens bound to their message ID.** They are never drafts, never rich, never ephemeral, never in-body buttons. Approve appears only on the card | `telegram/cards.py`, `CardPublisher` | Keeps the reviewed token code unchanged; ephemeral messages have `message_id` 0 |
| U4 | **Display artifacts are never bound or trusted.** Rich views, interdiff, HTML diff and mirror commits are all derived from `derived/<cid>/<rev>/`. Only `patch_sha256`, `head_sha` and the title/body hashes are bound | `review/` modules read only the derived directory | ADR-0014 |
| U5 | **The mirror can write only to the configured private repo.** The token is scoped by GitHub to that repo and proven by the enable-time probe. The writer refuses any other path. The repo must be private with Actions disabled before every push. Mirror content never goes upstream. GitHub reviews never approve | GitHub token scoping + `review_mirror_writer.py` (import-linter: only `review_mirror.py` imports it) + `review_mirror.py` pre-push checks | Defence in depth: token, code and runtime check |
| U6 | **Model-authored text is labelled.** Planner rationale, risk claims, self-review, "coder says" and Hermes text are prefixed 🤖 and shown in a quote. Gatekeeper-verified facts carry ✅ and no 🤖 | `telegram/ui/kit.py` `claim()` vs `fact()` | Prompt injection must not look like verification |
| U7 | **P0 alerts are never silenced** by quiet hours, `/quiet` or `/notify` | `telegram/notify.py` | Safety |
| U8 | **Bot-command links in untrusted classic text are defused.** U+2060 WORD JOINER goes after `/` when a letter follows. This is display only and is never applied to bytes that will be published (those are always in `<pre>`) | `escape.defuse_commands` | A tap on a link like "/dryrun" inside a Hermes answer would send that command |
| U9 | **Opt-in surfaces stay off until confirmed.** The mirror, Telegraph articles and the phase-2 Mini App are enabled only through `config/telegram.yaml` plus a human-confirmed `/reload`, plus a passing doctor probe | config snapshot (DESIGN §7.9) | Security-relevant config never changes silently |
| U10 | **Topic routing never changes authorization.** Every update still needs `from.id == owner`, `chat.type == private` and `chat.id == owner`. `message_thread_id` decides presentation and routing only | `telegram/auth.py` (unchanged) | |
| U11 | **Nothing is published to the web except approved GitHub actions and explicitly confirmed Telegraph articles.** An article can be created only by the gatekeeper, from an eligible `chat_answer` view (§6.8), after a publish tap and a confirm tap, and after the secret and personal-data scans pass. LLM components can't reach Telegraph at all (`telegra.ph`, `api.telegra.ph` and `edit.telegra.ph` are always denied for them) | `gatekeeper/telegraph.py` (the only module importing `telegraph_writer.py`, import-linter) + egress categories | The publish is public and irreversible, and its token stays with the deterministic component |

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
- The recommended BotFather setting allows users to create topics (**[VERIFY]** the setting name), since each new topic is a new chat with Hermes.

| Topic | Contains | Default sound |
|---|---|---|
| 🚦 Control | Pinned dashboard, command replies, P0 alerts, lifecycle summaries (CR-001), "local operator action" FYIs | P0 loud, rest silent |
| ✅ Reviews | Every approval card (PR, push_update, comment, claim, skill), explorer and file messages, draft reviews, Confirm panels, live PR status cards, drafted replies, merged/closed summaries. A PR's whole lifecycle stays in one topic because replies across topics are not supported (`ReplyParameters`) | P1 loud (rate-limited), updates silent |
| 📈 OSS feed | Discovery, vetting, stage progress, non-critical failures, digests | Silent (P2/P3) |
| 💬 Assistant | Default Hermes conversation (session `tg-<uid>-t<thread_id>`) | Normal (the reply to your own message) |
| 💬 user topics | One Hermes session each; renamed with `/title` | Normal |

Flat mode (topics off, or a topic call fails with "thread not found" after one re-create): every message is sent without a thread, with the section icon as its first character (CR-002 Amendment A, separation). Hermes sessions use `tg-<uid>-<n>` and `/new` increments `n`. The mode is re-checked at each start and on every "thread not found" error.

### 4.2 Menu button and command scopes (D2)
- `setChatMenuButton(owner, MenuButtonCommands)`.
- `setMyCommands(scope=BotCommandScopeChat(owner))` with the registry's commands. Descriptions are ≤ 60 characters, plain, verb first.
- `setMyCommands(scope=Default)` is set to the empty list, so strangers see nothing.
- On a control-state change (running, paused, killed, draining, down) the owner's command list is re-set:
  - killed: `/status /resume /logs /help`
  - down or draining: adds `/up` or `/undrain`
- The global commands `/start /help /settings` are always present (Telegram's recommended global commands).

### 4.3 Pinned live dashboard (D3, D15; mockup `06-pinned-dashboard.svg`)
One message in 🚦 Control, recorded in `tg_views(kind='dashboard')` and pinned silently. Content (classic HTML, ≤ 1,500 characters):
```
🟢 <b>ossbot</b> · UP · 🧪 <b>DRY RUN</b>                  (🟡 DRAINING · 💤 DOWN · ⏸ PAUSED · 🛑 KILLED (soft|full))
Egress: open · Hermes: self_improve · Rich views: on
Running: c_01JDEF implement 12m · c_01JGHI validate 3m   (≤ 3 lines, then "+N more")
Approvals: 2 pending · oldest expires <tg-time r>
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
- On `down` (CR-001) the gatekeeper edits the dashboard to `💤 DOWN (graceful) since <tg-time>` before the outbox is flushed. It sets `setMyShortDescription("💤 Offline since <HH:MM> — wake the laptop to reconnect")` and restores "🟢 Online" on `up`. Because the footer is a relative `<tg-time>`, a stale dashboard visibly ages ("Updated 3 hours ago") even when the bot is unreachable.

### 4.4 Update routing table (normative)
`telegram/routing.py` classifies every authorized update in this order. The first match wins.

1. `callback_query`: the token handler (existing).
2. `stopped_message_generation`: the chat stream with that `draft_id`, cancelled (§6.1). An unknown `draft_id` is ignored at DEBUG.
3. A message whose text starts with `/`: the command registry (existing). This applies in any topic.
4. A reply to a `pending_input` prompt (ForceReply correlation, 15 min; existing): that input kind (comment, change request, title, body, reject reason, feature fix).
5. A reply to a `tg_views` message of kind `file`, `explorer`, `interdiff` or `card` whose approval is `pending`: a **review comment** (§5.5). A document reply to an "Edit body" prompt is rule 4.
6. Free text in ✅ Reviews that is not a reply: relayed to Hermes in session `tg-<uid>-reviews`. The answer carries a button `[📝 Use as comment on <cid>]` when exactly one approval is pending; that is a token that converts the stored message into a comment.
7. Everything else goes to Hermes, in the session of its topic (or the flat session): text, photos, albums, documents, voice, locations, and replies to any other bot message (the quoted context is passed, CR-002).

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
<b>langfuse/langfuse</b> ⭐ 36.0k · 14 ext PRs merged/90d · 1st review ~9h
Issue <a href="https://github.com/langfuse/langfuse/issues/1234">#1234</a> <b>Fix trace export when spans are empty</b>
🤖 <i>Why: export crashes on empty span lists; maintainer confirmed in thread.</i>
✅ CONTRIBUTING · AI policy (disclosure) · DCO signed · unclaimed · no competing PR
Risk 🤖 low (0.82) · Change ✅ 3 files +42 −7 · Tests ✅ lint · unit 212 · smoke
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
- **The verbatim parts** follow as messages 2..K+1: the body as `PR body (i/K)` and then the commit message, unchanged from DESIGN §14.4.

Keyboard, in two phases. The token code is unchanged: Approve is minted only when `body_parts_delivered == body_parts_total`.
```
while delivering:  [⏳ delivering…] [❌ Reject]
                   [🧭 Explore] [🧾 Files (3)]
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
- `⬇️ Show card` re-sends the card at the bottom with `/approve <cid>` semantics. Tokens rotate in one transaction (N4), and the old card becomes "moved to a newer message".
- **The explorer never carries Approve** (U3).

### 5.3 S3: File view and diff (`02-diff-explorer.svg`)
`Open` (Files) or `Open file` (Diff) sends **one message per file**, as a reply to the card, recorded in `tg_views(kind='file', path, page)`.

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
   - The gatekeeper searches `quote.text` (≤ 1,024 characters) in the text of the page shown, which is stored in `tg_views.state_json` as page text and line map.
   - One match anchors the comment to `path` and `line_start..line_end` (new side for `+` and context lines, old side for `-` lines).
   - Several matches are resolved by `quote.position`. If still ambiguous, a line picker offers the candidate line numbers, ≤ 6 buttons.
   - No quote, or no match, anchors the comment to the file (`line` NULL).
2. **`💬 Comment`** on a file message opens a hunk picker (`H1 @@ -40,7 +40,12 @@` …, ≤ 8 per page). A hunk tap sends a ForceReply prompt (`pending_input(kind='comment', path, hunk)`; placeholder "Comment on hunk 2 (≤ 4000 chars)"). On the explorer the button gives a general comment with no path.
3. **Reply to the card or the explorer**: a general comment.

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

`✏️ Request changes` on the card is the same path, with the draft review included automatically. While an approval is `armed`, new comments are stored but the draft message says "Confirm pending — comments will apply if you cancel". The Confirm panel warns "⚠️ 3 unsent comments will be discarded". Confirm marks them `discarded_at`.

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
- "✅ touched" is deterministic: the interdiff changes the commented file inside `line_start-3 .. line_end+3`, or adds the file a general comment named. "⚠️ not touched" otherwise.
- "🤖 coder:" text is the implementer's report, ≤ 120 characters each, untrusted.
- A `🔀 Interdiff` button (row 2, replacing `Files` count) opens the interdiff as file-view messages (kind `interdiff`), with the same comment features.
- How the interdiff is computed (gatekeeper, at `/package` of rev N; ADR-0019):
  - when `base_sha` is unchanged: `ossbot.common.gitcmd.INTERDIFF = ["-c","diff.algorithm=myers","-c","diff.renames=false","-c","core.quotePath=true","diff","--no-ext-diff","--no-textconv","--no-color","--full-index", old_head, new_head]`;
  - otherwise: `RANGE_DIFF = [<same -c>, "range-diff","--no-color", f"{old_base}..{old_head}", f"{new_base}..{new_head}"]`, and the block says "base moved def5678 → 9a1b2c3".
  - Both run in the rev N mirror with a temporary `objects/info/alternates` line for the rev N−1 mirror, which is removed right after.
  - Output goes to `derived/<cid>/<N>/interdiff.patch` and `interdiff.json` (`mode, base_changed, files[{path, added, deleted}]`).
  - If it fails (previous mirror gone, git error), the block reads "Interdiff unavailable (<reason>) — review the full diff" and the card is still sent (WARNING).
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
- Cancel or expiry returns to S1 with rotated tokens (existing `rotate_card_tokens`).

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
- header `⚪ <b>Superseded</b> · r3 is newer`, `⌛ <b>Expired</b>`, or `⚪ <b>Invalidated</b> · <reason>`. Reasons: `dry-run turned off`, `config reloaded`, `title edited`, `content or mode changed`, `kill`;
- the original header struck through (`<s>`);
- keyboard `[⛔ Not approvable](disabled)` and `[⬇️ Show current]` (when a current card exists) or `[🔁 Retry]` (when the cause allows a fresh card).

Body-part messages are edited to a one-line stub `⚪ part of superseded c_01JABC r2`, so stale verbatim bytes can't be mistaken for current ones. Stale explorer and file messages get the same stub. A tap on any stale token answers "Superseded: content or mode changed since this card; review the new card." (existing).

### 5.13 Queue and priority
- **Ordering** (`telegram/review/queue.py`, deterministic, unit-tested): `(class_rank, expires_at, -target_priority, created_at)`.
  - Class 0: someone is waiting (a `comment` replying to a maintainer, a `push_update` triggered by a maintainer review).
  - Class 1: `issue_claim`.
  - Class 2: other `push_update`.
  - Class 3: `open_pr`.
  - Class 4: other `comment`.
  - Skill cards (§9) are class 3.
- **Loudness:** at most `telegram.loud_cards_per_hour` (default 2) new cards per hour notify with sound. Classes 0–1 always notify unless quiet hours apply. The rest go out with `disable_notification: true` and are counted on the dashboard ("📥 4 pending, 2 new silently").
- **`/queue`** shows the ranked list, 5 per page: `1. 🔁 PR #5678 fix · langfuse · expires <tg-time r>` with `[Open]` buttons (re-send the card with `/approve` semantics), plus `[⬆️ Oldest first] [🎯 By priority]` sort toggles.
- **Expiry warning:** a pending approval with < 12 h left gets one silent P2 reminder in the digest batch. One with < 1 h left gets a P1 reminder: a reply to the card, `[⬇️ Show card]`.

### 5.14 Review mirror integration (only when enabled; ADR-0019 option C)
When `review_mirror.enabled` is on and the push succeeded:
- the card's row 4 is `[🐙 Review on GitHub ↗]`;
- explorer section 1 shows `🐙 Mirror PR #12 · tree-equal ✅`.

Mirror review comments arrive as described in ADR-0019:
- A submitted review becomes a change request automatically, with one Telegram note replying to the card: `🐙 GitHub review · 3 comments → change request (r3 starting)`.
- Single comments join the draft review, with source `mirror`.
- A GitHub "Approve" produces `🐙 You approved on GitHub — approvals only count here` and `[⬇️ Show card]`.

When a mirror push fails (push protection, network, token), the card shows `🐙 Mirror unavailable: <reason>` and the native flow continues.

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
6. **`allowed_updates`** gains `"stopped_message_generation"` (ADR-0004 amendment). PTB 22.8 does not model the update. It reaches a `TypeHandler(Update)` through `update.api_kwargs["stopped_message_generation"]` (**[VERIFY]** PTB's handling of unknown update keys). Authorization: `chat.id == owner`.
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
  - Link previews are off (`link_preview_options.is_disabled`), except when the answer cites exactly one source: then `url` = that source, `prefer_small_media`, `show_above_text: false`.
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
  - label ≤ 40 characters, otherwise cut at a word boundary with "…"; text ≤ 200 characters.
- Each item becomes a `chat_followup` token (24 h, single-use, bound to the answer message, arg = the text). A tap echoes `↪️ <text>` as a bot message in the same topic and relays the text to Hermes as the user's next turn. It never reaches the command registry.
- One row per button, ≤ 3 rows.

### 6.5 Photos, albums, documents, voice and locations (CR-002 Amendment A)
- **Acknowledge:** `setMessageReaction(👀)` on the user's message as soon as it is accepted. It becomes `👌` on success (or `🤷` on unsupported input) when the answer is sent.
- **Albums:** messages with the same `media_group_id` arriving within 1.5 s of each other are one Hermes turn, capped at 10 images.
- **Images:** downloaded (≤ 20 MB, type allowlist), EXIF/GPS stripped (CR-002 toggle), sent as `input_image` data URLs. The vision model comes from `models.yaml roles.vision`. The footer shows `👁 vision: <model>`.
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
Features are registered in the gatekeeper (§12.3). Hermes calls the plugin tool `ossbot_feature_card(feature, kind, data)`, which makes `POST /v1/features/{feature}/cards` (role `hermes`, ≤ 30/h). The gatekeeper validates `data` with the feature's pydantic model (`extra="forbid"`, bounded fields), stores it in the feature store, renders the card with the kit, and sends it into the conversation's topic (session key → thread).

- **Nutrition log** (`07-assistant-chat.svg`, right phone):
  ```
  🍽 Meal logged · lunch · <tg-time>
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
- the turn had no attachments;
- the turn made no `memory`, `session_search` or `ossbot_*` tool calls (they may carry personal or OSS data);
- the answer is not in a feature topic or in ✅ Reviews.

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
- New egress category `telegraph` = `api.telegra.ph` for the **gatekeeper only** (proxy port 3150).
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
- **`/notify`** shows a toggle grid (✅/⬜, 2 per row) for: approvals, maintainer activity, CI, pipeline stages, discovery, digest daily, digest weekly, assistant-back notices. Toggles cover P1 and P2 only; P0 has no toggle.

### 7.3 Dedup, coalescing and batching
- **Dedup:** the existing `tg_outbox.dedupe_key`.
- **Coalescing:** an FYI with the same `(cid, event_kind)` within 10 minutes edits the earlier message instead of sending a new one, e.g. "CI: 3/9 → 7/9 → 9/9 ✅".
- **Batching:** P2 rows due within a 2-minute window, or held by quiet hours, merge into one `📈 <b>N updates</b>` message (OSS feed) with one line each, the full list in an expandable blockquote, and `[📥 Queue]` when any approval is involved.

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
  - Hermes usage (turns and models).
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

Arguments are chosen with pickers (`Picker`, ≤ 8 options per page, ≤ 3 per row, a pager row). Typed arguments are accepted and validated with the same pydantic model as the picker token's `arg`.

| Command | Class | Picker / flow | Notes |
|---|---|---|---|
| `/start` | read | Welcome + `/dashboard` | Global command |
| `/help` | read | Generated from the registry, grouped Control / Reviews / Assistant / Settings | Global command |
| `/settings` | personal | `[🖼 Rich views: auto] [🔕 Notifications…] [🌙 Quiet hours…] [📰 Digest…] [🐙 Mirror: off ⓘ]` | Mirror enabling is config (U9); the button explains how |
| `/status` | read | Full status text (existing) + `[📌 Dashboard]` | |
| `/dashboard` | read | Re-send and re-pin | |
| `/queue` | read | Ranked pager (§5.13) | |
| `/approve [cid]` | read | Picker of pending approvals → re-send the card (token rotation N4) | Never approves by itself (existing) |
| `/review [cid]` | read | Picker → explorer for that card | New |
| `/reject [cid] [reason]` | config | Picker → reasons `[Not valuable] [Wrong approach] [Repo unsuitable (7d)] [Other…]` → confirm | Existing behaviour |
| `/pause` | stop | Immediate | |
| `/resume` | go | Confirm: "Resume: scheduling restarts; approvals need Approve+Confirm again." | |
| `/kill [reason]` | stop | **Immediate** when typed; the dashboard button is two-tap (D14) | Soft kill (existing) |
| `/dryrun` | stop/go | Shows the current mode + `[🧪 Turn ON]` (immediate) or `[🔴 Turn OFF (LIVE)…]` → confirm: "LIVE mode: approved actions will publish as Amsal1. N open approvals will be re-issued." | Either toggle invalidates and re-issues cards (existing) |
| `/drain` · `/undrain` | stop · go | `/drain` immediate; `/undrain` confirm | CR-001 |
| `/down` | stop+confirm | Timeout picker `[60 s] [90 s] [120 s]` → confirm: "Down (workers): running stages get 90 s, then re-queue. The gatekeeper stays up so /up works." | Disruptive, so one confirm even though it is a stop |
| `/up` | go | Confirm | Refused while killed (CR-001) |
| `/reload` | go | Diff → confirm (existing) | Also the only way to enable the mirror (U9) |
| `/egress` | config | `[allowlist] [research] [open ✅]` → confirm panel listing the categories per profile that change | CR-002 |
| `/hermes` | config | `tier` → `[locked] [research] [self_improve ✅]` → confirm with the toolset diff → re-runs the surface probe | CR-002 |
| `/model` | config | Role picker (`chat, vision, planner, coder, reviewer, summarizer`) → catalog pager (8 per page, the current one marked ✅) → effort picker when supported → confirm → writes `models.yaml` | Existing writer |
| `/repos [filter]` | read/config | Pager of targets with badges → repo card `[Enable/Disable] [Allow AI]` → confirm | Existing |
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
- **Two-tap danger button** on the dashboard (`🛑 Kill`, `⬇️ Down…`): the first tap edits the keyboard to `[🛑 Tap again to kill (10 s)](danger) [↩️ Cancel]`. The second-tap token expires after 10 s. A typed `/kill` stays immediate.
- **Toasts** (`answerCallbackQuery`, ≤ 180 characters) are for transient acknowledgements ("Saved", "Cancelled; nothing was submitted."). `show_alert: true` is only for refusals that need attention ("Paused or killed: nothing was approved. /resume first.").
- **Picker confirm** for config pickers: the picker tap edits the message into the confirm panel, so there is one message per flow.

### 8.3 Why no ephemeral messages
Bot API ephemeral messages (10.2/10.3) are built for groups: a private overlay for one member. They have `message_id` 0, "it is not guaranteed that the ephemeral message will be received", and they can disappear on app restart. In a private chat with a single user every message is already private. A confirm surface must be durable and message-bound (U3).

ossbot therefore uses in-place edits plus toasts. If Telegram later supports ephemeral messages in private chats, they may be used for transient help text only, never for tokens.

## 9. Skill approval card (CR-002 self_improve; `08-skill-approval.svg`)
A staged skill (new or changed) produces an approval card in ✅ Reviews, with the same machinery as a PR card: verbatim parts, delivery before Approve, two taps, single-use message-bound tokens.

Binding: `{v:1, kind:"skill_activate", skill, version, files_sha256 (sorted path→sha256), tools_requested, tier, policy_hash}`. A new staged version, a tier change or a reload invalidates it.
```
🧠 <b>Skill ready for approval</b> · nutrition-log v3 (update) · s_01JXYZ
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
- Confirm text: `Confirm: activate skill nutrition-log v3 (sha256 9f2c…e1a0) for Hermes. Tools stay within tier self_improve. Expires in 5 min.`
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
| Laptop asleep, off, or offline | Nothing can answer: the gatekeeper is the only poller | His messages show as sent with no reply. The pinned dashboard footer keeps ageing ("Updated 3 hours ago"). After a graceful `down`, the dashboard reads `💤 DOWN (graceful) since 22:14`, and the bot's short description (profile) reads "💤 Offline since 22:14 — wake the laptop to reconnect". Telegram keeps unfetched updates ≤ 24 h |
| Back online | Gatekeeper start / poller reconnect | Within seconds of the first successful `getUpdates`: one Control message, `⏰ <b>Back online</b> · offline 8 h 52 m (graceful down) · 2 messages waiting`, then each pending update handled per §4.4 stale rules, with its age shown. The dashboard refreshes and the short description returns to 🟢 |
| Network flaps while up | PollStats | Nothing until 3 consecutive failures over ≥ 2 min; then the dashboard line `Telegram: reconnecting (3 failures)` shows on the next successful edit. Reconnects are counted in `/status` (CR-001) |
| Soft kill | Control state | Every non-allowed command: `🛑 <b>Killed (soft)</b> since <tg-time> · reason <code>hermes_tool_surface</code>. Only /status, /resume, /logs and /help work.` with `[▶️ Resume…] [📜 Logs]`. Assistant messages: `🤖 Assistant is stopped while killed.` The dashboard header turns 🛑. The command menu is reduced (§4.2) |
| Full kill / `kill.ps1` | Gatekeeper stopped | Same as offline (no answers). The FYI arrives at the next gatekeeper start ("Killed (full) at 14:02 by kill.ps1; resumed at …") |
| Paused | Control state | Approve and Confirm refused with the existing toasts; the dashboard shows `⏸ PAUSED · <reason>` |
| Draining or down (workers) | Lifecycle | Approve and Confirm refused ("ossbot is draining: nothing was approved. The card stays valid; retry after up."); the dashboard shows `🟡 DRAINING` or `💤 DOWN (workers)` with `[⬆️ Up…]` |
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
| 🔎 📄 🛠 | web search / page / tool | 🌙 | quiet |

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
- **Sizes:** ≤ 2 buttons per row when any label is > 12 characters, otherwise ≤ 3. ≤ 4 rows on cards and ≤ 5 on pickers (plus one pager row). Labels ≤ 20 characters target, 24 hard cap (truncated with "…" by the kit). Labels start with a verb.
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
- **Button labels from untrusted or model text** (follow-ups, feature items, topic names): `make_visible` (reject when any hidden character is found), collapse whitespace, ≤ 40 characters, and no leading `/`.

### 11.4 Limits and chunking (enforced in `telegram/ui/limits.py`; property-tested)

| Item | Telegram limit | ossbot budget |
|---|---|---|
| Classic text | 4,096 characters after entity parsing | ≤ 3,900 escaped characters; `<pre>` parts ≤ 3,500 (existing packer, tags balanced across parts) |
| Rich message | 32,768 characters, 500 blocks, 16 levels, 50 media, 20 columns | ≤ 30,000 characters, ≤ 450 blocks, ≤ 8 levels, ≤ 20 media, ≤ 12 columns; split at block boundaries; one `pre` ≤ 12,000 characters (split at lines) |
| Caption | 1,024 | ≤ 900 |
| `callback_data` | 1–64 bytes | Always `t:<22>` = 24 bytes |
| Callback answer text | 0–200 | ≤ 180 |
| Button label | not documented | ≤ 20 target / 24 hard |
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
- `sanitize.py`: `defuse_commands`, `label_from_untrusted`, `domain_of`.
- `limits.py`: the constants of §11.4 and the packers.
- `icons.py`: the §11.1 vocabulary as constants. Tests forbid other leading emoji in kit templates.
- `send.py`: turns a `View` into outbox rows (`message_thread_id` from `topics.py`, `disable_notification` from `notify.py`), then mints and binds tokens (§12.2).

### 12.2 Tokens for kit components ("mint then bind")
Approval and Confirm tokens keep the current code path (minted after delivery and attached by `editMessageReplyMarkup`, U3). Other kit buttons use this sequence:
1. Mint the token rows with `tg_message_id = NULL`.
2. Send the message with those `callback_data`s.
3. In the outbox `on_sent` hook, run `UPDATE ui_tokens SET tg_message_id = ? WHERE token IN (…) AND tg_message_id IS NULL`.

An unbound token is never accepted, because the handler requires `tg_message_id == callback.message.message_id` and NULL never matches. This halves the API calls per interactive message without weakening the binding.

Token purposes added:
- `nav`: multi-use, 24 h, arg `{view, section|page|path}`.
- `comment_hunk`, `comment_line_pick`, `review_send`, `review_discard`, `comment_undo`.
- `edit_text_pick`, `body_file`.
- `show_card`, `queue_sort`, `picker`, `confirm_cmd` (existing pattern), `danger_second_tap` (10 s).
- `chat_followup`, `chat_retry`, `chat_stop_fallback`, `use_as_comment`.
- `quiet_set`, `notify_toggle`, `digest_set`, `settings_set`.
- `feature_action`.
- `article_publish` (24 h), `article_confirm` (single-use, 5 min), `article_unpublish` (§6.8).
- `skill_approve`/`skill_confirm`/`skill_reject` (the approval machinery).
- `mirror_open` is a URL button, so it needs no token.

A daily GC deletes expired tokens older than 7 days.

### 12.3 Guide: adding a feature (also goes into AGENTS.md)
1. **Package:** create `src/ossbot/gatekeeper/features/<name>/` with `__init__.py` exposing `FEATURE = FeatureSpec(name, version, schema_models, store_migrations, cards, actions)`. Register it with the entry point group `ossbot.telegram_features` in `pyproject.toml`.
2. **Data:** pydantic models (`extra="forbid"`, bounded strings and numbers) for every `data` payload Hermes may send. Validation errors return 422 to the plugin and are never shown raw.
3. **Store:** SQLite at `/srv/ossbot/gatekeeper/features/<name>.db`, with numbered migrations in the package. The feature may not open any other DB.
4. **Cards:** functions `View <- (data, prefs)` built only with `telegram.ui.kit`. Golden tests in `tests/telegram/features/test_<name>_cards.py` cover LIVE-independent rendering, classic and rich, and limits.
5. **Actions:** `Action(name, safety ∈ {read, personal}, handler, input=None|"force_reply")`. `go` and `config` are not allowed for features (import-linter contract: `ossbot.gatekeeper.features` cannot import `approvals`, `submit`, `github_writer`, `review_mirror_writer`, `state_machine`, `control`).
6. **Hermes side:** a skill (self-written, approved through §9, or hand-authored) or a plugin tool that calls `ossbot_feature_card(feature, kind, data)`. External providers need a secret in `setup-secrets.ps1` (documented key name, optional) and an egress category if the gatekeeper or a sandbox calls them.
7. **Docs and tests:** one line in README's feature list. Run `pytest -m "unit or telegram"`.

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
| 16 | Mini App review dashboard (ADR-0019 B3) | Full web diff viewer | Tailscale (Funnel) account | S4 rules: optional, read-only, Ed25519, no secrets, never approves | Phase 2 (only if A + C fall short) |
| 17 | Weather/news line in the digest | Daily context | Open-Meteo (no key) | Read-only public API via Hermes | Phase 2 |
| 18 | Telegraph articles for long assistant answers (§6.8, D17) | Instant View reading of long write-ups | `TELEGRAPH_ACCESS_TOKEN` (gatekeeper) | Public and undeletable: opt-in per message, two taps, scans, eligibility rules, gatekeeper-only egress (U11) | v1.x, off by default |

## 14. Implementation

### 14.1 Files

Python stack, unchanged and locked: Python 3.12, python-telegram-bot 22.8 (raw `do_api_request` for 10.1–10.3), FastAPI over UDS, httpx, pydantic 2, Jinja2 (HTML diff) and SQLite. No new runtime dependency.

| Path | Change |
|---|---|
| `src/ossbot/gatekeeper/telegram/ui/{__init__,kit,blocks,classic,rich,sanitize,limits,icons,send}.py` | **New** component kit (§12) |
| `src/ossbot/gatekeeper/telegram/review/{explorer,files,comments,interdiff_view,status_card,queue,stale,edit_text}.py` | **New** review flow (§5) |
| `src/ossbot/gatekeeper/telegram/{topics,routing,dashboard,notify,digest,drafts,pickers}.py` | **New** (§4, §6.1, §7, §8) |
| `src/ossbot/gatekeeper/telegram/cards.py` | Decorated S1 layout via the kit (FEAT-005 item 22); skill card; stale rendering |
| `src/ossbot/gatekeeper/telegram/keyboards.py` | `style`, `disabled`, URL/copy buttons in `to_json`; the regrouped card rows |
| `src/ossbot/gatekeeper/telegram/escape.py` | `defuse_commands`; the `tg-time` helper |
| `src/ossbot/gatekeeper/telegram/handlers.py` | Delegates classification to `routing.py`; new token purposes; `stopped_message_generation` `TypeHandler` |
| `src/ossbot/gatekeeper/telegram/bot.py` | `ALLOWED_UPDATES += ["stopped_message_generation"]`; topic bootstrap; command scopes and menu button; draft lane task; short-description updates on up/down |
| `src/ossbot/gatekeeper/telegram/commands/{builtin,registry}.py` | New commands (§8.1); safety class `personal`; picker integration; `menu()` per control state |
| `src/ossbot/gatekeeper/outbox.py` | Columns `priority`, `thread_key`, `coalesce_key`; quiet-hours hold; batching; "thread not found" recovery |
| `src/ossbot/gatekeeper/chat/{backend,hermes_api,stream,md2blocks,followups,media}.py` | FEAT-005 item 23 plus streaming, rendering, follow-ups and media (§6) |
| `src/ossbot/gatekeeper/review_html.py` + `templates/review.html.j2` | **New** HTML diff (§5.4) |
| `src/ossbot/gatekeeper/interdiff.py` | **New**: runs `INTERDIFF`/`RANGE_DIFF` at `/package` (§5.7) |
| `src/ossbot/gatekeeper/packaging.py`, `derive.py` | Call `interdiff.py`; retention amendment (§5.7) |
| `src/ossbot/common/gitcmd.py` | `INTERDIFF`, `RANGE_DIFF`, mirror plumbing argv constants |
| `src/ossbot/gatekeeper/review_mirror.py`, `review_mirror_writer.py` | **New**, opt-in (ADR-0019 C); import-linter contract "only review_mirror imports review_mirror_writer" |
| `src/ossbot/gatekeeper/telegraph.py`, `telegraph_writer.py` | **New**, opt-in (§6.8). Import-linter: only `telegraph` imports `telegraph_writer`; egress category `telegraph` (gatekeeper only); `config/egress.yaml` `always_denied` += `telegra.ph`, `api.telegra.ph`, `edit.telegra.ph` for every other profile |
| `src/ossbot/gatekeeper/features/{__init__,registry}.py` | **New** registry (§12.3); `api/features.py` route `POST /v1/features/{feature}/cards` (role hermes) |
| `src/ossbot/gatekeeper/api/notify.py` | Optional `priority` (P1/P2 only from Hermes; P0 is never accepted from Hermes) |
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
                                      'digest','feature','picker','progress','chat_answer')),
  approval_id TEXT, cid TEXT, rev INTEGER, path TEXT, page INTEGER,
  state_json TEXT NOT NULL DEFAULT '{}', content_sha256 TEXT, updated_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE review_comments (
  id TEXT PRIMARY KEY, cid TEXT NOT NULL, rev INTEGER NOT NULL, approval_id TEXT,
  source TEXT NOT NULL CHECK (source IN ('telegram','mirror')), path TEXT, side TEXT CHECK (side IN ('old','new')),
  line_start INTEGER, line_end INTEGER, quote TEXT, text TEXT NOT NULL,
  tg_message_id INTEGER, gh_comment_id INTEGER, outdated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, change_request_id TEXT, discarded_at TEXT);
ALTER TABLE change_requests ADD COLUMN comments_json TEXT;           -- structured comments (§5.5)
ALTER TABLE tg_outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 2;
ALTER TABLE tg_outbox ADD COLUMN thread_key TEXT;                    -- topic name, resolved at send time
ALTER TABLE tg_outbox ADD COLUMN coalesce_key TEXT;
CREATE TABLE ux_prefs (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE digest_runs (kind TEXT NOT NULL, period_start TEXT NOT NULL, sent_at TEXT, stats_json TEXT NOT NULL,
  PRIMARY KEY (kind, period_start));
CREATE TABLE chat_turns (
  turn_id INTEGER PRIMARY KEY, session_key TEXT NOT NULL, thread_id INTEGER, user_message_id INTEGER NOT NULL,
  run_id TEXT, status TEXT NOT NULL CHECK (status IN ('streaming','done','stopped','failed')),
  started_at TEXT NOT NULL, finished_at TEXT, model TEXT, answer_message_id INTEGER);
CREATE TABLE review_mirror (
  cid TEXT NOT NULL, rev INTEGER NOT NULL, branch TEXT NOT NULL, pr_number INTEGER, mirror_head TEXT NOT NULL,
  tree_equal INTEGER NOT NULL, pushed_at TEXT, closed_at TEXT, last_error TEXT, PRIMARY KEY (cid, rev));
CREATE TABLE reminders (id TEXT PRIMARY KEY, due_at TEXT NOT NULL, text TEXT NOT NULL, created_by TEXT NOT NULL,
  thread_id INTEGER, fired_at TEXT, cancelled_at TEXT);
CREATE TABLE telegraph_pages (path TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, turn_id INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL, published_at TEXT NOT NULL, unpublished_at TEXT);   -- audit event on publish/unpublish
```
`ui_tokens.purpose` is free text (the comment in `0001_init.sql` lists the purposes), so no CHECK constraint changes. The new purposes are listed in §12.2.

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
| `stopped_message_generation` update | `TypeHandler(Update)` reading `update.api_kwargs`; listed in `allowed_updates` **[VERIFY]** |

Everything else is PTB-native: topics, `style`, reactions, effects, scopes, pins, `copy_text`, link previews. When PTB adds 10.1+ support, the raw calls can switch to typed methods without changing the kit's API.

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

### 14.6 External input validation

| Input | Required | Type / limits | On failure |
|---|---|---|---|
| `callback_data` | yes | `^t:[A-Za-z0-9_-]{22}$` (existing); the token row must match purpose, message, expiry and use | `answerCallbackQuery` with the rejection text; WARNING |
| Picker / nav token `arg` | yes | JSON validated by the purpose's pydantic model (`extra="forbid"`) | Treated as an invalid token |
| Review comment text | yes | Non-empty after strip; ≤ 4,000 characters; UTF-8 | Reply with the limit; nothing stored |
| `quote` | optional | ≤ 1,024 characters; used only for mapping | File-level comment |
| Body-as-file document | optional | ≤ 64 KiB; `.md`/`.txt` or a text MIME type; strict UTF-8; normalized; `policy.check_text`; ≤ 60,000 characters | Exact reason; no override stored |
| Title edit | yes | 1–256 characters, single line (existing) | Existing |
| `/quiet` argument | optional | One of the picker values, or `<1-24>h`, or `until HH:MM` | Usage hint |
| `/title` | yes | 1–48 characters after sanitizing; no controls or bidi | Usage hint |
| `/remind` (typed) | yes | `when` parsed as an ISO date-time or `in <n>(m|h|d)`, ≤ 365 d; text ≤ 500 | Usage hint |
| `message_thread_id` | optional | int; a known system topic, or treated as an assistant conversation | n/a |
| `stopped_message_generation` | n/a | `chat.id == owner`; `draft_id` in `chat_turns` with `status='streaming'` | Ignored |
| Hermes SSE | n/a | Each event ≤ 1 MB; the answer ≤ 200 KB; unknown event types ignored; tool names ≤ 64 characters; URLs http(s) only, ≤ 2,048 | Truncate or ignore + WARNING |
| Follow-ups block | optional | ≤ 3 items; label ≤ 40; text ≤ 200; no hidden characters; not starting with `/`; no approval words | Item dropped |
| Feature payload | yes | The feature's pydantic model (`extra="forbid"`; bounded numbers, e.g. protein 0–500 g; strings ≤ 200) | 422 |
| `ossbot_reminder_create` | yes | `due_at` in the future, ≤ 365 d; text ≤ 500; ≤ 50 active | 422 |
| Telegraph article content | n/a (gatekeeper-built) | From the stored `chat_turns` answer only; eligibility rules (§6.8); title 1–256 characters (≤ 80 + suffix used); converted nodes ≤ 60 KB (API limit 64 KB); tag and attribute allowlist; `href` http(s) only | Button not offered, or publish refused with the reason |
| Mirror review/comment JSON | n/a | `user.login == profile.github_login`; body ≤ 65,536; `path` ∈ the rev's numstat; `line` int ≥ 1; `commit_id` == current `H'` else `outdated` | Ignored + WARNING |
| `config/telegram.yaml` | yes | Strict schema; times `HH:MM`; weekly `Mon..Sun HH:MM`; numbers within ranges (`loud_cards_per_hour` 0–20, intervals ≥ the minimums in §11.4) | Snapshot load or reload refused (existing) |
| `profile.yaml telegram.timezone` / `quiet_hours` | optional | IANA zone (`zoneinfo`); `HH:MM-HH:MM` (may wrap midnight) | Start refused with a message (consistent with the profile rules) |

### 14.7 Tests

**Unit** (fast, hermetic):
- `test_kit_limits.py`: Hypothesis property tests. Every View renders ≤ budget in both modes, with balanced tags, every `callback_data` ≤ 64 bytes, labels ≤ 24, ≤ 3 per row.
- `test_blocks_untrusted.py`: random untrusted strings, including `<tg-button>`, `**`, `/kill`, `tg://` and bidi characters. They come out as plain `RichText` strings with `skip_entity_detection` set and are never in `html`/`markdown` fields.
- `test_defuse_commands.py`
- `test_md2blocks.py`: golden Markdown → blocks, plus malformed input.
- `test_followups.py`: sanitization and the drop rules.
- `test_routing.py`: every rule of §4.4, including stale ages.
- `test_queue_order.py`
- `test_notify_quiet.py`: P0 always loud; wrap-around quiet hours; held P2 → batch.
- `test_quote_mapping.py`
- `test_interdiff.py`: bare repos; same base → tree diff; moved base → range-diff; missing old mirror → unavailable.
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
- the killed-mode reduced menu;
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
  - interdiff (S6) + retention amendment.
- **M2** (extends FEAT-005 item 23, FEAT-007): chat streaming via drafts and Stop, md2blocks, sources and footer, follow-ups, media UX, `/new` `/title` `/memory`.
- **M3:** rich views for explorer, file, interdiff, tests and assistant (behind `auto`); digest; status card S10/S11; reminders (§13 item 3).
- **M4** (opt-in): review mirror (ADR-0019 C) + `ossctl review-mirror`.
- **v1.x:** Telegraph articles (§6.8; off by default, with `ossctl telegraph setup|rotate`).
- **Phase 2:** feature registry consumers (nutrition log first), §13 v1.x and phase-2 items, Mini App (only if needed).

Docs and AGENTS.md updates ship with M1. DESIGN §14 is updated to point here, and DESIGN §12.5 retention is amended.

## 15. Acceptance criteria
1. Every message the gatekeeper sends passes the §11.4 budgets: Hypothesis tests over all View builders, in classic and rich modes, with random untrusted input. Every `callback_data` is exactly 24 bytes, `t:` plus 22 characters.
2. No untrusted string ever appears in a rich `html` or `markdown` field, and every rich payload has `skip_entity_detection: true`. A test injects `<tg-button type="callback_data" data="x">`, `**`, `/dryrun off` and `tg://` into Hermes output, issue titles and logs, and asserts they render as literal text.
3. In classic mode, `/kill`-style text inside a Hermes answer is not a clickable command: a word joiner follows the slash. Verbatim `<pre>` bytes of PR bodies, comments and commit messages are byte-identical to the stored values.
4. The approval card and Confirm are classic messages. Approve exists only on the card; the explorer, file, interdiff, draft-review and status messages contain no approve or confirm token. A test enumerates the purposes on every kind of view.
5. Approve is minted only after all body parts are delivered (existing AC 14), with the new keyboard layout. Golden S1 cards exist in LIVE and DRY RUN for `open_pr`, `push_update`, `comment`, `issue_claim` and `skill_activate`.
6. The Confirm text equals the DESIGN §12.4 sentence for the current mode. A dry-run toggle between Approve and Confirm leaves the old Confirm token invalid (existing behaviour, re-tested through the new UI).
7. A reply to a file message with a quote that matches one line range stores a `review_comments` row with that `path` and range. An ambiguous quote shows a line picker. A non-matching quote stores a file-level comment.
8. "Send N comments" creates exactly one `change_requests` row whose `comments_json` has N entries. The contribution moves to `CHANGES_REQUESTED` and the old card shows S5 with no approvable button.
9. A revised card (rev N) shows the interdiff from `INTERDIFF` when the base is unchanged, or from `RANGE_DIFF` when it moved. The "touched" flags are computed from the interdiff, and the card states that the full patch is what gets approved. With the rev N−1 mirror missing, the card still arrives with "Interdiff unavailable".
10. Every invalidation reason edits the old card to S12 within one housekeeping tick (≤ 10 s) and stubs its body parts. A stale token tap answers "Superseded…".
11. `/queue` orders pending approvals by `(class_rank, expires_at, -target_priority, created_at)`. At most `loud_cards_per_hour` cards per hour go out without `disable_notification`.
12. With topics enabled in the fake `getMe`, the four system topics are created once and reused across restarts. Approval cards go to Reviews. A "thread not found" error re-creates the topic once and then falls back to flat mode, and the card is still delivered.
13. In the owner scope, the command menu matches the registry; in the default scope it is empty. In killed mode the owner's menu is `/status /resume /logs /help`.
14. The dashboard is edited at most once per 30 s and never when its content hash is unchanged. Its footer is a relative `tg-time`. A deleted dashboard is re-sent and pinned.
15. Typed `/kill` is immediate. The dashboard Kill needs a second tap within 10 s, and a second tap after 11 s is refused.
16. A go or config command sent more than 10 minutes before processing produces only its confirm prompt, with the age shown. A stop command runs and notes the delay.
17. P0 messages are loud during quiet hours and while `/quiet` is active. P1 becomes silent and P2 is held and later batched into one message.
18. An assistant turn sends a draft within 1 s, then at most one draft per second, a heartbeat at least every 20 s, and a final persisted message. `stopped_message_generation` stops the Hermes run and persists the partial answer with "Stopped by you".
19. A follow-up tap sends exactly its stored text to Hermes as a new user turn and never invokes the command registry. A suggestion starting with `/` or containing "approve" never becomes a button.
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
28. Egress: `telegra.ph`, `api.telegra.ph` and `edit.telegra.ph` get 403 from every profile except the gatekeeper's `telegraph` category, in every egress mode, including `open`.

## 16. Out of scope
- Building the Mini App (ADR-0019 B), Telegram Serverless, webhooks and any inbound component.
- Inline mode, guest mode, business features, payments and Stars.
- Ephemeral messages (§8.3).
- Approving anything outside the Telegram card: GitHub reviews, Mini App, reactions, reply keyboards and voice are all excluded.
- A persistent reply keyboard. The menu button and dashboard cover it. It could be added later with fixed label → read-only command aliases.
- Multi-user support, groups and channels.
- Implementing a specific reverse-image-search provider (needs a user decision, CR-002), calendar, and the other phase-2 items in §13.
- Changing the approval/token core, the binding, or the Confirm sentences (S1, S2).
- Automatic Telegraph publishing, and Telegraph for any content type other than eligible assistant answers (§6.8).

## 17. Items to verify at implementation
Check by web search or live probe; record results in VERIFICATION.md or the ADR-0019 addendum.
1. Rich-message rendering on Amsal's Telegram clients (Android/iOS/desktop versions): `details`, `table`, `pre language=diff`, anchors. Quote-reply on rich messages. If quote-replies don't work there, file views default to classic when the user prefers line comments (`ux_prefs.file_view: classic`).
2. Whether `sendMessageDraft`/`sendRichMessageDraft` count against the per-chat send rate. How drafts behave in topics.
3. PTB 22.8 handling of `stopped_message_generation` in `allowed_updates` and `api_kwargs`.
4. The BotFather setting name for private-chat topics, and whether a "General" topic exists in private-chat topic mode. Whether pins are per topic in topic mode.
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

## 18. Mockups (`docs/ux/`)
SVG phone screens (390 × 844 frame) with a constraints panel on each. Labels, rows, lengths and token sizes follow §11.4.

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
| `09-offline-error.svg` | Laptop asleep → back online catch-up; soft-killed mode; Hermes unavailable |

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

