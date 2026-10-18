# ADR-0019: PR review surface: native Telegram explorer by default, private GitHub review mirror as the opt-in rich layer
Status: Accepted (2026-10-18; CR-003 approved and merged into DESIGN.md §14; design loop ended after round 6, remaining findings carried as build criteria in docs/ux/cr003-carried-findings.md; "CR-003 §N.x" in this ADR = DESIGN §14.(N+6).x). Was: Proposed (CR-003), revision 6 after design review rounds 1–5 (`docs/ux/cr003-review.md`) · Date: 2026-10-18 · Design: CR-003 §5, DESIGN §14.3–§14.4a · Related: ADR-0004, ADR-0014, ADR-0020 (Telegram client library and Pillow, formerly Addendum A of this ADR), CR-004

## Context
Amsal reviews almost every candidate on his phone. Today's review surface (DESIGN §14.4) is one card, the verbatim PR body and commit message in `<pre>` parts, a diff paged through `editMessageText`, and a `.patch` file. It is safe, but on a phone it is flat. It has no per-file navigation, no way to comment on a line, and no "what changed since my last review".

Hard constraints that any surface must keep:
- ADR-0004: one bot, polled by the deterministic gatekeeper with long polling (python-telegram-bot 22.8). There is no webhook, no inbound port and no static IP.
- Approve, submit, resume, dry-run off and skill activation stay deterministic gatekeeper actions. Each is two taps (Approve, then Confirm) on single-use, expiring tokens. A token is bound to its message and accepted only from the allowlisted user in the private chat. No LLM is in that path.
- ADR-0014: an approval binds the gatekeeper-derived patch, the head SHA and the title/body hashes. Any edit invalidates it.
- Nothing public happens before approval. Dry-run is on by default and always labelled.

### Research (October 2026; sources in CR-003 §19)
- Bot API 10.1–10.3 added **rich messages**: `sendRichMessage`, `sendRichMessageDraft`, and `editMessageText(rich_message=…)`. A rich message holds up to 32,768 characters and 500 blocks. Blocks include headings, tables, collapsible `details`, expandable quotes, `pre` with a language, in-document anchors and in-body buttons. A message can be built from typed **blocks** with no markup parsing at all.
- Bot API 10.3 added disabled buttons and `force_reply` on inline keyboards. Bot API 9.4 added button styles (`success`, `danger`, `primary`). Bot API 9.3 and 9.4 added topics in private chats. Bot API 9.5 added the `date_time` entity (`<tg-time>`), which the client renders in local time or as relative time.
- A user's reply can carry a `quote` (`TextQuote`: the selected text plus its UTF-16 position). That anchors a comment to the exact lines the user selected.
- Mini Apps can be validated without the bot token, using the Ed25519 `signature` field and Telegram's published public key. They also offer `DeviceStorage` (5 MB per user), `SecureStorage` (Keychain/Keystore, 10 items), Main and Secondary buttons, fullscreen mode and theme parameters. Since 2026-07-20, Mini App methods can't be called from foreign origins.
- **Telegram Serverless** (2026-10-06) hosts JavaScript handlers, endpoints, a SQLite database and Mini App files at `app<id>.tgcloud.ai`. BotFather manages a **webhook** for the bot, and the Bot API refuses `getUpdates` while a webhook is set. Turning Serverless on for the ossbot bot would therefore break ADR-0004.
- python-telegram-bot 22.8 (2026-06-12, still the latest release) fully supports Bot API 10.0. The 10.1–10.3 features need raw calls through `Bot.do_api_request`, which the outbox already uses for every send. Whether to keep PTB at all was re-evaluated against aiogram and pyTelegramBotAPI in **ADR-0020**: PTB stays, with a typed adapter for 10.1–10.3.
- `Telegram.WebApp.sendData` is "only available for Mini Apps launched via a Keyboard button" (a reply-keyboard `web_app` button). It sends ≤ 4,096 bytes as a `web_app_data` service message and closes the Mini App. Mini Apps opened from an inline button or the menu button can't use it.
- Prior art: `SanNw/opencode-telegram` routes only an authenticated "Bridge" through an HTTPS tunnel and keeps the OpenCode server on loopback. Access is protected by Telegram-signed init data, an owner check and a trusted-device key. HTML previews are static and sandboxed. Even that design adds an internet-reachable component. ossbot does not need one by default.

## Options

**A. Native Telegram.**
- The decorated card stays a classic HTML message (the bound approval surface).
- An explorer message walks the sections. A file-view message shows one file at a time with syntax-highlighted hunks (Prev/Next edit it in place; a file picker jumps): a rich message when available, classic `<pre><code class="language-diff">` pages otherwise.
- Long sections sit in expandable blockquotes or `details` blocks. Test-log excerpts are included.
- Two documents are attached: a full `.patch` and a self-contained, script-free HTML diff.
- To comment, the user replies to a file message, optionally quoting lines, or taps 💬 and picks a hunk. Comments collect into a draft review that is sent as one change request.
- A revised candidate shows a gatekeeper-computed interdiff.

**B. Telegram Mini App diff viewer.** A web app with a split or unified diff, a file tree, logs and inline comments. Approve stays a chat button. It needs a way to get data into the app without a static IP:
- **B1 static app on GitHub Pages with end-to-end encrypted data.** The app code would be public, and GitHub Free serves Pages only from public repositories. The ciphertext has no carrier. A URL fragment holds only a few KB. Telegram files can't be fetched without the bot token. A secret gist needs a new gist-write credential and is readable by URL. In practice B1 does not work for real diffs.
- **B2 Telegram Serverless.** The platform webhook conflicts with long polling, so B2 needs a second bot. It also needs a Node toolchain, a separate CLI token, and a way to move bundles from the gatekeeper into the serverless database: bot-to-bot messages to that second bot. Pre-approval diffs would sit in Telegram's serverless database, in plaintext unless also end-to-end encrypted.
- **B3 optional tunnel to a read-only local review server.** Tailscale Funnel gives a stable `*.ts.net` name, and TLS ends on the device, so Tailscale can't read the content. A Cloudflare named tunnel needs a domain, and TLS ends at Cloudflare. Cloudflare quick tunnels are for testing only and do not support SSE. The server would verify the Ed25519 init-data signature, so it holds no bot token. It would hold no secrets and serve only derived artifacts. **Comments are constrained by `sendData`**: it works only when the Mini App was launched from a reply-keyboard `web_app` button, carries ≤ 4,096 bytes, and closes the app. A review card's inline button or the menu button can't be used for a commenting Mini App, and CR-003 §16 rules out a persistent reply keyboard. So B3 has two possible comment models:
  - **B3-view (chosen if B3 is ever built):** the Mini App is view-only, opened from an inline `web_app` button on the explorer; comments stay native Telegram replies and quotes (option A).
  - **B3-batch:** during a review session the gatekeeper shows a **temporary one-button reply keyboard** (`[🌐 Review in app]`, `one_time_keyboard`), removed with `ReplyKeyboardRemove` when the session ends or after 30 minutes. The user writes comments in the app and submits **one batch** (≤ 4,096 bytes of JSON, about 10–20 short comments), which closes the app and arrives as a `web_app_data` update that the gatekeeper validates like any comment. More comments mean reopening the app.

  The server stays read-only in both models. This is the cleanest B, but it is a new internet-reachable path plus a web front-end to build and maintain.

**C. Private GitHub review mirror.**
- The gatekeeper pushes a display copy of the candidate to a private repo owned by Amsal1 (e.g. `Amsal1/ossbot-review`) and opens a PR there.
- Amsal reviews in the GitHub mobile app, with its diff UI, file tree, inline comments and suggestions.
- The gatekeeper polls the mirror PR and turns his review into a change request. It is a write to GitHub, but not a public one. It uses a separate fine-grained token scoped to that one private repo.

**D. Telegraph article (Instant View)** for the review text (user suggestion, 2026-10-18). This is evaluated and **rejected for review content**:
- Telegraph pages are public to anyone with the URL.
- The API can't delete a page; blanking it is the only option.
- Content is capped at 64 KB.
- Only basic tags are allowed: no syntax highlighting, no tables, no collapsible blocks.

Pre-approval diffs, vetting and maintainer notes, and unreleased changes would become public and permanent before any approval, which violates "nothing public before approval". CR-003 §6.8 allows Telegraph only for opt-in, scanned assistant answers.

**Combinations.** A+C, A+B3 and A+C+B3, evaluated in the second table below. A is the base of every combination because it is the only option that needs no setup and adds no exposure.

### Evaluation

| | A native | B1 Pages + E2E | B2 Serverless | B3 tunnel Mini App | C private mirror |
|---|---|---|---|---|---|
| Phone UX | Good with rich messages (file per message, highlighted hunks, collapsible sections); fair on classic fallback | Excellent if it worked | Excellent | Excellent | Excellent (GitHub mobile diff, tree, expand context, suggestions, rendered Markdown) |
| Inline comments | Yes: reply or quote on file messages, hunk picker | Needs a write path | Yes (endpoint) | Only via a one-shot reply-keyboard button (≤ 4 KB per batch, the app closes); none from inline- or menu-launched apps | Yes, native review comments and suggestions |
| New exposure | None | Public app code; a third-party store for ciphertext | Second bot, JS runtime on Telegram, CLI token | **Internet-reachable server** via tunnel; tunnel daemon and egress | New write token, scoped to one private repo; no inbound |
| Data leaving the laptop, and to whom | Telegram (cloud chats use server-client encryption, like today's cards) | GitHub Pages (app), gist host (ciphertext) | Telegram Serverless DB | Tunnel provider (Funnel: metadata only; Cloudflare: plaintext); Telegram | GitHub (Microsoft), private repo: the touched files at base and head, title/body, commit message, validation summary |
| User setup | None | Pages repo, BotFather URL | BotFather Serverless, second bot, Node | Tailscale/Cloudflare account, BotFather URL | Create a private repo, disable Actions, one fine-grained token |
| Build effort | Medium | High, and blocked on the carrier | High (new stack) | High (web app, server, tunnel provisioning, uid, doctor) | Medium (plumbing trees, push, PR, poll, map comments) |
| Failure modes | Rich rendering differs between client versions (classic fallback); Telegram outage | Carrier limits | Webhook conflict; vendor lock-in; platform is new | Tunnel down, DNS lag, WebView quirks, exposed attack surface | GitHub outage, push protection blocking a test secret, token expiry, repo made public |
| Can it approve? | Only through the chat card (unchanged) | Never | Never | Never | **Never**: GitHub review states are ignored for approval |

#### Combinations (each row states what the combination adds **on top of A**)

| | A+C | A+B3 | A+C+B3 |
|---|---|---|---|
| Phone UX | A for quick reviews, GitHub mobile for deep ones (tree, expand context, suggestions); one tap between them (card row 4) | A plus a web diff viewer inside Telegram; no app switch | Three ways to read the same diff; the extra one adds little over C |
| Inline comments | A's replies and quotes **plus** native GitHub line comments and suggestions, mapped into the same draft review | A's replies and quotes; B3 adds batched comments only through the temporary reply keyboard (≤ 4 KB, app closes), or none in B3-view | C's native comments already cover everything B3-batch could add |
| Marginal exposure | One write token scoped to one private repo; **no inbound path** | **An internet-reachable server** (tunnel), a tunnel daemon and its egress, a Mini App origin | The union: write token **and** an inbound path |
| Marginal data leaving the laptop | Touched files at base and head, PR text and a validation summary to a private GitHub repo | Tunnel provider metadata (Funnel) or plaintext (Cloudflare), plus Telegram WebView caches | The union of both |
| Marginal user setup | Private repo, Actions off, one fine-grained token, `ossctl review-mirror enable` | Tailscale account and Funnel, BotFather Mini App URL | Both |
| Marginal build effort | Medium: plumbing trees, push, PR, poll, comment mapping (CR-003 §14.8 M4) | High: web front-end, read-only server, Ed25519 validation, tunnel provisioning, doctor checks | High + medium |
| Marginal failure modes | GitHub outage, push protection, token expiry, repo made public (P0 check) | Tunnel down, DNS lag, WebView quirks, a new attack surface to patch | Both sets; neither layer can break A |
| Can it approve? | Never | Never | Never |

**Recommended: A+C** (A always on; C opt-in). **A+C+B3 is rejected for now**, because B3 would add the only inbound path in the whole system, and its comment model (one-shot batches through a temporary reply keyboard) gives no comment capability that C's native GitHub review comments lack. A+B3 is rejected because it pays B3's exposure for a viewer that C already provides without any inbound path.

## Decision
1. **A is the default and is always on.** It works with zero extra setup. The approval card and the Confirm step stay classic HTML messages whose reply-markup tokens are bound to the card's message ID (CR-003 U3). Rich messages are used only for read-only views: explorer, files, interdiff, tests and assistant answers. They are built in **blocks mode** with plain strings and `skip_entity_detection`, so no untrusted byte is ever parsed as markup. On a client or API failure the gatekeeper falls back to the existing classic renderer automatically.
2. **C is the opt-in rich layer.** `review_mirror.enabled: false` by default. Enabling it requires the mirror repo, its token, a passing `ossctl review-mirror enable` doctor probe and a human-confirmed `/reload`. When it is on, the card gains a URL button `🐙 Review on GitHub`, and mirror review comments become draft-review comments in Telegram (CR-003 §5.14). C beats B as the rich layer for three reasons: GitHub mobile already is a first-class diff reviewer, C adds no inbound path, and there is no web UI to maintain.
3. **B is deferred to phase 2. Mini App: not in v1, requires no-exposure hosting or explicit user approval** (user decision, 2026-10-18). v1 has no internet-reachable component of any kind: no Cloudflare Tunnel, Tailscale Funnel, ngrok, public webhook or inbound port. Revisiting the Mini App needs a fresh user decision. If it is ever built, it is B3 and must stay optional, off by default and read-only. It must verify init data with Ed25519 (no bot token on the server), hold no secrets and never approve. It starts as **B3-view**: opened from an inline `web_app` button, view-only, with comments as native replies (option A), because `sendData` is unavailable to inline- and menu-launched Mini Apps. B3-batch (a temporary one-button reply keyboard, one ≤ 4,096-byte batch per open) is allowed later only with its own review, since it is the one place a reply keyboard would appear. **Serverless is rejected for this bot** while ADR-0004's long polling stands. B1 is rejected because there is no data carrier. **D (Telegraph) is rejected for every kind of review content**, because its pages are public and can't be deleted.
4. **The approval binding is unchanged.** Every display artifact is derived from `derived/<cid>/<rev>/` and is never bound or trusted: rich views, interdiff, HTML diff and the mirror commits. Display artifacts include the mirror commits, whose SHAs differ from the real ones. The binding stays `patch_sha256`, `head_sha` and the title/body hashes (ADR-0014).

## Review flow (states and screens)
Field-level content, keyboards and limits are in CR-003 §5. Mockups are in `docs/ux/`.

```mermaid
stateDiagram-v2
  [*] --> Proposed: scout proposal (CR-004 gate 1, CR-003 §5.15)
  Proposed --> Proposed: Adjust plan / Make it smaller (new version, old card stale)
  Proposed --> Delivering: Start work + Confirm → plan, implement, validate, /package
  Proposed --> Declined: Skip / Later (ProposalApprovals.reject; PROPOSAL_REJECTED / PROPOSAL_SNOOZED)
  Declined --> Proposed: Undo within 10 min (new p_ approval, expiry kept; CR-003 §5.15)
  Declined --> [*]
  Proposed --> [*]: Never (after its confirm) / expiry
  Delivering --> Ready: all body parts delivered (Approve token minted)
  Ready --> Exploring: Explore / Files / Tests
  Exploring --> Ready
  Ready --> Commenting: reply/quote on file msg, hunk 💬, mirror review
  Commenting --> ChangesRequested: Send N comments / Request changes
  Ready --> ChangesRequested: Request changes (free text)
  ChangesRequested --> Delivering: new rev packaged (new card r+1, interdiff first)
  Ready --> Ready: Edit title/body (new card, old approval invalidated)
  Ready --> Armed: Approve
  Armed --> Ready: Cancel / confirm expiry (disarm_expired housekeeping, CR-003 §12.4 delta 2)
  Delivering --> Proposed: size class above approved, only with on_overrun=reapprove (RESCOPE_REQUIRED)
  Delivering --> Paused: hard size ceiling hit (ceiling-hit card)
  Armed --> Submitting: Confirm (mode recomputed)
  Submitting --> Live: SUBMITTED (LIVE) / DRY_RUN_DONE
  Live --> ReplyCard: maintainer comment → drafted reply (same two-tap flow)
  Live --> Merged
  Live --> Closed
  Ready --> Stale: any invalidation (dry-run toggle, reload, new rev, edit, expiry)
  Armed --> Stale: kill / dry-run toggle / reload / edit / new rev (Confirm token dies with the approval)
  Delivering --> Stale: kill / dry-run toggle / reload / edit / new rev (no Approve is ever minted)
  Stale --> Delivering: Show current (resolved by contribution) / Retry (eligible actions only); AppContext.reissue(), per-action part counts, expiry kept (CR-003 §12.4 delta 7)
  Stale --> [*]
```

| Screen | State | What the user sees | Mockup |
|---|---|---|---|
| S0 Proposal (gate 1) | Proposed | Proposal card: problem, approach, likely files, size estimate (code/test), validation plan, signals, confidence; Start work (two taps), Adjust plan, Make it smaller, Skip, Never, Later; versions reply to each other with a deterministic diff | `10-proposal-card.svg` |
| S1 Preview card | Delivering → Ready | Header with mode badge, subject, verified checks, model claims marked 🤖, change stats, size estimate vs actual (code/test split, overrun highlighted, never blocked by default), expiry as relative `tg-time`; body and commit message parts below; keyboard minted only after delivery | `01-pr-preview-card.svg` |
| S2 Explorer | Exploring | One message, sent as a reply to the card and edited in place: 1 Summary → 2 Why this issue → 3 Signals → 4 Risk → 5 Files → 6 Diff → 7 Tests → 8 Self-review → 9 PR text | `02-diff-explorer.svg` |
| S3 File view | Exploring | One file-view message per rev, edited in place: hunks highlighted as `diff`; collapsible long hunks; ◀ Prev file · 📂 picker · Next file ▶; 💬 Comment; `.patch`/HTML documents | `02-diff-explorer.svg` |
| S4 Draft review | Commenting | Each comment acknowledged (file:line, Undo); one draft-review message: "📝 3 comments · Send · Discard" | `02-diff-explorer.svg` |
| S5 Changes in progress | ChangesRequested | Old card stubbed "✏️ Changes requested → r3 in progress"; stage progress edited in place | `04-interdiff.svg` |
| S6 Revised card | Delivering/Ready (r+1) | New card, sent as a reply to the old one: what you asked for → interdiff (gatekeeper) → "coder says" per comment (🤖) → "touched in interdiff" flag per comment (gatekeeper) | `04-interdiff.svg` |
| S7 Edit text | Ready | Picker: Title / Body / Body as file / Clear my edits; ForceReply; new card, old one stale | `01-pr-preview-card.svg` (notes) |
| S8 Confirm | Armed | Card edited to the Confirm panel: 🧪 DRY RUN or 🔴 LIVE banner, exact Confirm sentence, review coverage, unsent-comment warning, 5-minute relative expiry | `03-approve-confirm.svg` |
| S9 Submitting | Submitting | Progress lines: fork, push, PR | `05-submitted-status.svg` |
| S10 Live status | Live | A status card edited in place (CI, reviews, maintainer comments); P1 events arrive as replies; drafted replies use S1/S8 | `05-submitted-status.svg` |
| S11 Merged/closed | Merged/Closed | Status card finalized; summary message (with a message effect when configured) | `05-submitted-status.svg` |
| S12 Stale | Stale | Card header "⚪ Superseded/Expired/Invalidated: <label>" (label from the real reason code, CR-003 §5.12), disabled button "⛔ Not approvable", "⬇️ Show current" (shown only when the contribution has an open card, which it re-sends whatever rev or version that is) or "🔁 Retry" (a fresh card via `AppContext.reissue()`, only while the action is still eligible, CR-003 §12.4.3) | `04-interdiff.svg` |

Queue and priority (CR-003 §5.13): pending approvals sort by `(class_rank, expires_at, -target_priority, created_at)`. Classes, highest first:
0. Someone is waiting: a reply to a maintainer, or a `push_update` a maintainer asked for.
1. `issue_claim`, and every `👤` user-directed proposal or preview.
2. Other `push_update`, and ceiling-hit cards.
3. `open_pr` previews, `RESCOPE_REQUIRED` cards and skill cards.
4. Autonomous proposals (gate 1), ordered by CR-004's rank score; promoted to class 2 within 6 h of expiry.
5. Other comments.

At most `telegram.loud_cards_per_hour` (default 2) new cards notify with sound; the rest are sent silently and counted on the dashboard. `/queue` (mockup `12-queue.svg`) lists Gate 2 items, then Gate 1 items, with numbered Open buttons. Opening an item re-sends its card with the expiry kept, so the item keeps its place in the list.

## Option C, normative details
- **Repo:** `profile.yaml review_mirror.repo` (e.g. `Amsal1/ossbot-review`). The user creates it, with a README on `main`.
  - Before every push, and every 10 minutes while it is enabled, the gatekeeper checks `GET /repos/<mirror>`: `private == true` and `visibility == "private"`. It also checks `GET /repos/<mirror>/actions/permissions`: `enabled == false`.
  - Either check failing disables the mirror and raises an alert. A repo that is public is a P0 alert.
  - Actions must be off because the mirrored tree contains upstream workflow files, and a PR would run them.
- **Token:** `GITHUB_REVIEW_MIRROR_TOKEN` in `gatekeeper.env` (0600 gatekeeper).
  - Fine-grained, resource owner Amsal1, "Only select repositories" = the mirror.
  - Permissions: Contents RW, Pull requests RW, Metadata R, and Administration R (only to read the Actions setting; **[VERIFY]** the minimum permission).
  - GitHub documents that only classic tokens can write to public repositories the user does not own, and the repository selection excludes his own public forks.
  - The existing classic `public_repo` token cannot reach private repos, so the two tokens can't be swapped by accident.
- **Probe at enable time** (`ossctl review-mirror enable`, human-initiated only, never during automated builds):
  - `GET /user` must return `login == profile.github_login`.
  - A foreign-write probe on one of Amsal1's public repos: `POST /repos/<public repo>/git/refs` with an all-zero SHA and a random `refs/heads/ossbot-probe-*` name. It cannot create anything. 403 or 404 passes. 422 means the token can write there, so enable is refused and a P0 alert says to revoke the token.
  - `GET /repos/<mirror>` with the classic write token must return 404.
  - The token's `github-authentication-token-expiration` header is recorded. A P1 reminder goes out 14 days before expiry.
- **Sparse snapshot commits (display only):**
  - Built in the per-rev bare mirror with the hardened `GIT_ENV`/`GIT_ARGS` and a temporary `GIT_INDEX_FILE` under `/srv/ossbot/gatekeeper/work/`, using plumbing only (`read-tree --empty`, `ls-tree -z`, `update-index --cacheinfo|--force-remove`, `hash-object -w --stdin`, `write-tree`, `commit-tree`).
  - `B'` holds the base versions of exactly the touched paths. It is a root commit (`commit-tree <tree>` with no parent).
  - `H'` holds the head versions, plus `.ossbot-review/{PR_BODY.md,COMMIT_MSG.txt,REVIEW.md}` built by the gatekeeper. It is committed **with `B'` as its parent** (`commit-tree <tree> -p <B'>`), so the two branches share history and GitHub can open a PR between them (without a common ancestor GitHub refuses the PR).
  - A new rev builds a fresh `B'`/`H'` pair; the force-push replaces both branches, so GitHub's "force-pushed" compare link stays meaningful.
  - Commit messages and PR text never contain `@`, `#N` or bare URLs: upstream links appear only inside code spans, so no cross-references or notifications are created.
  - Fixed identity: `ossbot review mirror <noreply@invalid>`, with the package timestamp as the date.
  - **Integrity check before push:** `diff --raw B' H'` outside `.ossbot-review/` must equal `diff --raw <base> <head>` (paths, modes, blob IDs). A mismatch means no push and an ERROR log. A candidate that touches `.ossbot-review/` is refused for mirroring.
  - Pushes are small: only the touched files.
- **Push and PR:**
  - `review_mirror_writer.py` is the only module allowed to write. It is importable only by `review_mirror.py`, enforced by an import-linter contract. It hard-refuses any URL or API path outside `/repos/<mirror>/` and `https://github.com/<mirror>.git`.
  - It force-pushes `H':refs/heads/review/<cid>` and `B':refs/heads/review-base/<cid>`. Force is allowed **only** here; the main writer still never force-pushes.
  - It opens or updates the PR: head `review/<cid>`, base `review-base/<cid>`, title `[<cid> r<rev>] <title>`, and a body with the card facts and instructions.
  - A new rev force-pushes over the old one. GitHub then shows "force-pushed" with a compare link, which is a second interdiff for free.
- **Reading back:**
  - Polling: every 60 s while the approval is pending, every 10 min otherwise, with ETags. It reads `pulls/<n>/reviews`, `pulls/<n>/comments` and `issues/<n>/comments`.
  - Only comments by `profile.github_login` count. Each comment's `path` must be in this rev's numstat. A comment whose `commit_id` is not the current `H'` is flagged outdated.
  - A submitted review (Comment or Request changes) becomes **one** change request with all its comments and suggestions, verbatim. A Telegram note goes out ("🐙 GitHub review: 3 comments → change request r3").
  - Single comments outside a review join the Telegram draft review.
  - An **Approve** review is ignored for approval. Telegram replies: "Approvals only count here", with `⬇️ Show card`.
  - A commit the user makes on the mirror branch, such as "edit file" in GitHub mobile, becomes a change request carrying its compare patch as guidance. It is never pushed upstream. The next rev overwrites it.
- **Retention:**
  - When the approval is consumed, rejected or terminal, the gatekeeper closes the mirror PR and deletes both branches.
  - GitHub keeps `refs/pull/<n>/head` in the private repo, so `ossctl review-mirror purge` documents deleting and recreating the repo.
  - Backups never include mirror state.

## Option A, normative summary
- **Card:** classic HTML, ≤ 3,900 escaped characters, with the existing two-phase keyboard. The keyboard is regrouped per action kind (CR-003 §12.4.1):
  - `open_pr`:
    - Row 1: Approve, Reject.
    - Row 2: Explore, Files. On revised cards, Interdiff replaces Files.
    - Row 3: Request changes, Edit text.
    - Row 4: mirror URL, only when enabled. When the mirror push lands after the card was sent, row 4 is added with `editMessageReplyMarkup` re-using the **same** callback tokens already on the card (nothing is minted or rotated). An armed card gets it when it returns to S1.
  - `push_update`: Approve & Push, Reject fix / Explore, Files / Request changes.
  - `comment` and `issue_claim`: Approve & Post, Reject / Edit text. Edit text opens the comment edit prompt directly. There is no explorer, because the verbatim body is the whole content.
  - Decision buttons are single-use. View buttons (Explore, Files, Interdiff, Edit text) are **multi-use** until the approval ends, so they work however often they are tapped.
  - Tests and logs move into the explorer.
  - Approve exists only on the card, never in explorer or file views.
- **Explorer and file messages** (only for `open_pr` and `push_update`):
  - They are sent as replies to the card, so tapping the quote header jumps back.
  - There is one file-view message per `(cid, rev)`. Prev/Next edit it in place, and a file picker jumps between files.
  - Their tokens are multi-use navigation tokens (24 h, message-bound) with `approval_id = NULL` and `{cid, rev, action_id}` in their argument. They resolve the current approval of **their own outbound action**, which stays unambiguous when a claim or a drafted comment is open beside the PR. So a Cancel or a re-sent card never kills them (CR-003 §12.2, §12.4).
  - After Confirm, they stay usable read-only.
  - Model-authored text is always labelled 🤖 and quoted. Gatekeeper-verified facts carry ✅ and no label.
- **Re-sending a card** (Show card, Show current, `/queue → Open`, `/approve`, Retry) always goes through the one wrapper `AppContext.reissue(cid, actor, keep_expiry=True)` (CR-003 §5.2, §12.4 deltas 5 and 7, §12.4.3):
  - **Part counts per action.** It re-creates every **eligible** open card of the contribution, each with its own `body_parts_total` from `cards.part_count(action_id)`. So a re-sent `open_pr` card (≥ 2 parts) gets Approve only after all its verbatim parts are delivered, as ADR-0004 requires, and a one-part sibling claim gets it after its one part.
  - **Expiry.** Each approval keeps its expiry.
  - **Publishing.** It publishes every new card after the transaction commits.
  - **Eligibility.** An action is eligible when it is `pending_approval` and its contribution is in a state where the action can still be submitted. A re-send is refused while any approval of the contribution is `confirmed` ("Submitting… try again in a moment"); a re-send never invalidates a confirmed approval.
  - **Stale cards.** S12 "Show current" resolves by contribution (purpose `stale_show_current`), so after r3 is packaged it re-sends r3, and after proposal v2 it re-sends v2.
  - **Proposal and skill cards** are re-sent through their own wrappers, `AppContext.reissue_proposal` and `AppContext.reissue_skill`, which publish every new card after the service's commit. The decision services themselves never publish (CR-003 §12.4.3).
- **Redaction.** Every text a review view shows (issue text, logs, model claims, test tails) passes the one redactor before it is sent (CR-003 U14). The verbatim body, commit-message and comment parts, the `.patch`, the HTML diff's code and **the PR title wherever it is shown** (the card's `PR title:` line, explorer section 9, `📋 Copy title`), plus publish text quoted back in an edit prompt, are never altered, because they must equal what is published and what the approval binds. The title is shown escaped in `<code>` and made visible, but never defused or redacted. These bytes are only scanned, and a hit withholds the card (`render_failed`, the `Card withheld` stub, no Approve). Packaging and the edit paths gate the same bytes first with the same scanner, including the gatekeeper's own secret values (`Violation("gatekeeper_secret", <surface>)`), so a render-time hit is a bug. What you see is what you approve.
- **Replies to `comment` and `issue_claim` cards** are not review comments. They open a confirm panel, "Replace the comment text with your reply?", whose Replace runs the S7 `edit_comment` path (DESIGN §14.11.8, PLAN item 45) (a new card; the old one goes stale `text_edited`). Review comments exist only for `open_pr` and `push_update` (CR-003 §4.4 rule 5).
- **Comments:**
  - A reply to a file message becomes a comment on that file.
  - A `quote` resolves to lines by searching the quoted text in that page's diff text. Ties are broken by `position`. An ambiguous match opens a line picker. No match anchors the comment to the file only.
  - The hunk picker works in both rendering modes.
  - Comments are stored in `review_comments` and sent together, with optional free text, as one `change_requests` row whose `comments_json` holds the structure.
- **Interdiff:**
  - Computed at `/package` of rev N against rev N−1, using the fixed commands `ossbot.common.gitcmd.INTERDIFF` (tree diff, same base) and `RANGE_DIFF` (base moved).
  - In base-moved mode, `range-diff` output is not a patch: the file list comes from comparing the normalized per-file hunks of the two derived `FORMAT_PATCH` outputs, "touched" is file-level only, and the interdiff view is a plain `pre` with file-level comments (CR-003 §5.7).
  - The rev N mirror gets a temporary `objects/info/alternates` entry for the rev N−1 mirror. To keep that mirror available, DESIGN §12.5 retention changes: the newest **shown** rev's mirror is kept until the next `/package` or a terminal state, not deleted at `IMPLEMENTING`.
  - The interdiff is display-only. The card says "you approve the full patch".
- **Documents:**
  - `.patch` is the existing derived patch.
  - `ossbot-review-<cid>-r<rev>.html` is rendered with Jinja2 autoescape from derived data only. It has inline CSS and no script. Its CSP is `default-src 'none'; style-src 'unsafe-inline'; img-src data:`. It works in iOS Quick Look, Android Chrome or Files, and desktop browsers. **[VERIFY]** how Telegram opens it on Amsal's phone.

## Consequences
- **Good:** the default needs no setup and no new exposure. Phone review gets per-file navigation, line comments, interdiffs and test evidence. The approval code path keeps its rules (token format and checks, binding, Confirm sentences); the UI needs nine bounded deltas, each tested, listed in CR-003 §12.4. The richest layer (C) reuses GitHub's mature mobile reviewer, with no inbound path and a token that cannot write anywhere else.
- **Costs:**
  - Raw Bot API calls for 10.1–10.3 features, behind a typed adapter (`telegram/rawapi.py`, pydantic models), until PTB v23 ships typed support. This was chosen over switching libraries (ADR-0020): it keeps lenient decoding on the safety-critical polling path and costs no migration.
  - Two renderers (rich and classic) with property tests.
  - Interdiff needs the previous mirror kept a little longer.
  - C adds a second GitHub credential to rotate.
  - C adds GitHub (Microsoft) as a holder of pre-approval content for touched files, in a private repo. Telegram already holds the same content today.
- **Risks:**
  - Rich messages are new and render differently across client versions. The automatic fallback and the live check in CR-003 §17 cover this.
  - If the user flips the mirror repo to public, pre-approval content becomes public. The visibility check runs before every push and every 10 minutes and raises a P0 alert, but content already pushed is exposed. Rotation is documented.

## Verification items
1. Rich-message rendering, quote-replies on rich messages, and `details` and `diff` highlighting on Amsal's Telegram clients (phone and desktop).
2. How each client opens `.html` documents.
3. The minimum fine-grained permission needed to read `actions/permissions`.
4. The foreign-write probe's status codes for a fine-grained token.
5. Whether GitHub creates any cross-reference from code-span URLs (expected: none).
6. The Serverless webhook behaviour, recorded only if B2 is ever reconsidered.
7. End-to-end: `stopped_message_generation` arrives through PTB 22.8 in `api_kwargs` and is accepted in `allowed_updates` (the PTB side is verified from source, ADR-0020).

## Addendum A: Telegram client library (moved to ADR-0020)
The library evaluation that was Addendum A in revisions 2 and 3 of this ADR is now **[ADR-0020](0020-telegram-library.md)**, unchanged in substance. It covers python-telegram-bot vs aiogram vs pyTelegramBotAPI vs an own client, and decides to keep python-telegram-bot 22.8 with a typed Bot API 10.1–10.3 adapter. It also holds the Pillow decision (not added to the gatekeeper; allowed only in `--network none` sandbox images) and the sources. Earlier references to "Addendum A" or "§A.n" mean the matching section of ADR-0020.

## Responses to design review round 1
- **Finding 9 (combinations not evaluated):** addressed. The combinations table above covers A+C, A+B3 and A+C+B3 on every row with marginal exposure and effort, and states "Recommended: A+C" and why A+C+B3 is rejected.
- **Finding 10 (`sendData`):** addressed. The constraint is stated in Research and in B3; B3 starts view-only (B3-view), B3-batch uses a temporary one-button reply keyboard with one ≤ 4 KB batch per open; the evaluation cell and Decision 3 are corrected.
- **Finding 13 (library evaluation, user-required):** addressed as Addendum A with web-verified facts, URLs and the access date. It is written here because this revision step may edit only CR-003, this ADR and `docs/ux/`; it moves verbatim to `docs/adr/0020-telegram-library.md` in the next docs commit that may create ADR files. (Done in revision 4: ADR-0020.)
- **Finding 22 (mirror graph, late URL row):** addressed in Option C (`H'` parent `B'`) and Option A (row 4 via `editMessageReplyMarkup` with the same tokens).
- **Finding 1 (token core) and finding 3 (base-moved interdiff):** reflected in the state diagram (confirm-expiry disarm), Option A (kit tokens with `approval_id NULL`) and the interdiff summary; the normative text is CR-003 §12.4 and §5.7.
- **CR-004 (orchestrator-relayed user requirement):** the review flow now starts at the proposal card (S0), and the queue classes and size behaviour follow CR-004 Amendment B (overrun highlighted, not blocked; `RESCOPE_REQUIRED` only with `reapprove`; ceiling-hit card).

## Responses to design review round 2
The normative fixes are in CR-003 (§21 maps each of the 22 findings). This ADR changes where the decision text depends on them:
- **Finding 20 (state diagram):** addressed. The diagram adds `Armed --> Stale` and `Delivering --> Stale`, with their causes (kill, dry-run toggle, reload, edit, new rev). It also adds `Stale --> Delivering` for a re-sent card, which keeps its expiry.
- **Findings 1 and 2 (card buttons, several open actions):** reflected in Option A. Keyboards are per action kind, view buttons are multi-use, the explorer exists only for patch-carrying kinds, and view tokens resolve their own outbound action (`action_id`).
- **Finding 3 (re-sends reset the TTL):** reflected in Option A and the queue paragraph. Re-sends keep the expiry, so `/queue` order is stable (CR-003 §12.4 delta 7).
- **Finding 22 (queue mockup):** the queue paragraph points to the new `docs/ux/12-queue.svg`.
- **Findings 4–19 and 21:** none of them changes this ADR's decision (A by default, C opt-in, B deferred, Serverless and Telegraph rejected for review content). Their fixes are CR-003-only.

## Responses to design review round 3
The normative fixes are in CR-003 (§22 maps all 16 findings). This ADR changes where its text depends on them:
- **Finding 1 (re-sent cards arming early):** Option A "Re-sending a card" now names the one wrapper `AppContext.reissue()`, which passes a per-action part count, so Approve still appears only after every verbatim part of each re-sent card is delivered (ADR-0004). The state diagram's `Stale --> Delivering` edge says so.
- **Finding 3 (Show current):** the S12 row and the diagram say Show current resolves by contribution and is shown only when an open card exists.
- **Finding 4 (replies to comment and claim cards):** Option A states that such a reply opens the replace-text confirm, never a review comment.
- **Finding 5 (eligibility):** Option A and the S12 row restrict re-sends and Retry to eligible actions, and refuse while a sibling is confirmed.
- **Finding 6 (proposal Skip/Later/Never):** the diagram gains the `Declined` state, the reject call and the 10-minute Undo edge.
- **User requirement (library ADR):** Addendum A is filed as ADR-0020, and this ADR keeps a back-reference.
- **Findings 2 and 7–16:** they don't change this ADR's decision. Their fixes are CR-003-only.

## Responses to design review round 4
The normative fixes are in CR-003 (§23 maps all 8 findings). This ADR changes where its text depends on them:
- **Finding 1 (redaction):** Option A states that review views are redacted (CR-003 U14) and that the verbatim publish bytes are scanned, never altered.
- **Finding 6 (who publishes proposal and skill re-sends):** Option A names `AppContext.reissue_proposal` and `AppContext.reissue_skill` as the publishing wrappers.
- **Findings 2–5, 7 and 8:** they don't change this ADR's decision (A by default, C opt-in, B deferred). Their fixes are CR-003-only; finding 2's update-handler change is recorded in ADR-0020 because that ADR owns the sequential-processing decision.
- **Library decision:** still in [ADR-0020](0020-telegram-library.md) (formerly Addendum A of this ADR), unchanged.

## Responses to design review round 5
The normative fixes are in CR-003 (§24 maps all 8 findings). This ADR changes where its text depends on them:
- **Finding 2 (the PR title is publish bytes):** Option A "Redaction" lists the PR title on the card, in explorer section 9 and in `📋 Copy title`, and publish text quoted in edit prompts, as scanned and never redacted; a hit withholds the card.
- **Finding 4 (gatekeeper secret values at packaging):** Option A "Redaction" says packaging and the edit paths gate the same bytes first with the same scanner, so a render-time hit is a bug.
- **Findings 1, 3 and 5–8:** they don't change this ADR's decision (A by default, C opt-in, B deferred). Their fixes are CR-003-only. Finding 8's token-use sentence (view tokens multi-use, decision tokens single-use) is an ADR-0004 and DESIGN §8.4 amendment note listed in CR-003 §14.8; the "single-use" tokens in this ADR's Context are the decision tokens, which stay single-use.
- **Library decision:** still in [ADR-0020](0020-telegram-library.md), unchanged.
