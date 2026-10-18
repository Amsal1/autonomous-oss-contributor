# CR-003 / ADR-0019 / ADR-0020 design review, round 6

Reviewed: `docs/plan/CR-003-telegram-ux.md` (revision 6: §0–§6, §12.1–§12.5, §14.1, §14.5, §15, §15.1, §18, §24 read in full; the rest re-checked against the round-5 review where revision 6 left it unchanged), `docs/adr/0019-pr-review-surface.md` (revision 6), `docs/adr/0020-telegram-library.md` (revision 3), the text and constraint panels of all mockups `docs/ux/00..12-*.svg` (01, 03 and 07 read in full).
Context read: the round-5 review, ADR-0004 and DESIGN §14 (as cited by the CR), CR-004 (ceiling, overrun, states), CR-002 (egress, as cited).
Code spot-checked for revision 6's claims: `gatekeeper/policy.py` (`check_text` signature and checks, `TOKENISH_RE`, `kiro_key_hashes`, `Violation`), its two production callers (`packaging.py` for title, body and commit message; `api/actions.py` for `comment`), `telegram/escape.py` (`make_visible` → `⟦U+XXXX⟧`, 8 characters per invisible character; `code()`), `telegram/cards.py` (today the title is rendered in full in `<code>`), `common/config.py` (`TextPolicy.title_max: PosInt = 256`, no upper bound), `approvals.confirm_text`.
Not re-checked (no web access, and not needed this round): the live web sources in §19 and ADR-0020; the PTB 22.8 internals verified in round 4.

**Verdict: CHANGES_REQUESTED** (0 HIGH, 1 MEDIUM, 5 NIT).

## Overall
Revision 6 resolves all 8 round-5 findings, as asked and with sound refinements:
- **Stop partial and truncation** (R5-1): `draft_visible()` is the only builder for drafts, Stop partials and truncated answers, and the stored `answer_text` is the same string. The hold-back argument holds: a secret still arriving has no whitespace, is < 256 characters and ends at the tail, so its first character is always after `max(last whitespace, len − 256)`. AC 66 now tests Stop after the 2nd of 3 deltas and a 200 KB cut inside a `ghp_` token.
- **PR title as publish bytes** (R5-2): in U8, U14, §5.1, §5.2 section 9, §12.1 `scan()`, §14.5, AC 66, ADR-0019 Option A and mockup 01 it's scanned, never redacted or defused.
- **Whitespace patterns refused at load** (R5-3), **shared `SecretScanner` with `check_text(…, secret_values)`** (R5-4), **current-state re-render with per-occurrence keys** (R5-5), **Telegraph `request_started`/`outer_cancelled`** (R5-6), **U12 time bound** (R5-7) and the **token-use amendment note** (R5-8): all applied, with BA-20..22 and BA-3/BA-16 extended.

One design-level gap is left, and it comes from revision 6's own rule: the PR title is now "never altered", but the card budget rule (unchanged) still cuts an over-long untrusted part of a required block with "…", and mockups 01 and 03 show the title cut on the card and on the Confirm panel.

## Required confirmations

| Item | Status |
|---|---|
| **HIGH 1 (re-send part counts)** | **Resolved at the design level** (unchanged since round 4). `ApprovalService.reissue(…, part_count, keep_expiry)` gives each action its own `body_parts_total`. `AppContext.reissue`, `reissue_proposal` and `reissue_skill` are the only callers, and they publish after the commit. `recover()` step 0 heals a lost publish. Covered by ACs 57/58, BA-1 (AST guard), BA-17 and `test_reissue_part_counts.py` (part 2 fails once → no Approve until it lands). ADR-0019 Option A matches. |
| **HIGH 2 (non-blocking handlers; `/kill` ≤ 2 s during a Hermes stream)** | **Resolved at the design level.** §12.5 is normative: inline work is bounded, the rest is spawned with done-callback cleanup (cancel-before-start included), `/kill` cancels `KILL_CANCELS`, Stop is handled in the slot, and the only inline exception is the 8 s-bounded Confirm preflight (D14, §8.1). Covered by ACs 15, 18, 59, 67 and BA-2/15/18; the fake-Bot-API suite fails on `handler_slow`. |
| **ADR-0020 library decision** | **Met.** Keep python-telegram-bot 22.8 with a typed `rawapi.py` adapter over `do_api_request`. aiogram is rejected (strict decoding has blocked polling, #1890 and #1840; bus factor 1), pyTelegramBotAPI is rejected (GPL-2.0, full migration), and an own client is rejected. Scored on unknown-field tolerance, Bot API 10.1–10.3 coverage, licence, migration cost and tie-breakers, with revisit triggers. The round-5 response records that the fixes are library-independent. |
| **Pillow decision** | **Met.** Not in the gatekeeper (a pure-Python metadata walker, no pixel decoding in the TCB); allowed only in `--network none` sandbox images. |
| **ADR-0019 Addendum A promoted, back-reference** | **Met.** ADR-0019's Related line and its "Addendum A (moved to ADR-0020)" section point to ADR-0020, and ADR-0020's header says it supersedes Addendum A. |
| **Implementation details carried as build ACs (§15.1)** | **Met.** BA-1..BA-22 hold the signatures, key formats, queries, constants and literal lists, each with a test. Not re-litigated. |
| User requirements (messages 2–12 as far as CR-003 owns them) | Met: telegra.ph evaluated and adopted opt-in (D17, §6.8); the library evaluated (ADR-0020); the PR review flow designed end to end (§5, ADR-0019); size is soft with `xl` and user-directed big work allowed, and tests are counted apart (D18, D19, D22, §5.1); the proposal gate exists (D18, §5.15). |

## Findings

### 1. MEDIUM: the card budget rule can cut the PR title, which revision 6 defines as publish bytes that are never altered; mockups 01 and 03 show it cut
Where:
- CR §12.1 budget rule ("Required on every card: the header, the mode line, the subject/title (or issue) line …"; bounds table "subject/title or issue line ≤ 300"; "An untrusted part that is longer is cut with '…' inside its bound").
- Against: §5.1 notes ("The PR title is publish bytes … escaped and made visible inside `<code>`, but never defused or redacted"), U8, U14 ("never altered, because they must equal what is published"), ADR-0019 Option A ("What you see is what you approve") and DESIGN §14.4a ("Every public action shows the exact bytes it will publish before Approve exists").
- Mockup 01 (`PR title: fix(web): handle empty spans…`; its panel says "header, mode, title, size, expiry never cut") and mockup 03 (`PR: fix(web): handle empty spans…` on both Confirm panels).

Code: `policy.text.title_max` is 256 code points, and the schema has no upper bound (`PosInt`). Each character costs up to 8 escaped characters on the card: `make_visible` turns an invisible character into `⟦U+XXXX⟧`, and `&` escapes to `&amp;`. So a valid title can need up to about 2,048 escaped characters, or more if `title_max` is raised. Today `cards.render` prints the whole title.

The design never says whether the `PR title:` line is the bounded "subject/title" block (then it is cut with "…" above 300) or an optional block (then the budget rule may drop it). Either way, the card that carries Approve can show less than the title `open_pr` publishes and `title_sha256` binds. The tail could be hidden behind "…", or the title could be missing entirely. This is the round-5 finding-2 problem (the card shows other bytes than it publishes) through truncation instead of redaction. Reaching it takes a title of escapable or invisible characters, which a prompt-injected coder can write. The `⚠️ hidden chars` line and explorer section 9 lessen it, but Approve doesn't require opening the explorer.

Fix:
- §12.1 required blocks: add "**the PR title line** (`open_pr`; it is publish bytes and is **never cut or dropped**)". Bounds table: add "PR title line ≤ 2,100 (`title_max` × 8, the worst-case visible-and-escaped cost per character, plus the label and `[edited by you]`)". Keep the 300 bound and the "…" cut for the issue line only. Change the sentence to "An untrusted part that is longer is cut with '…' inside its bound, **except publish bytes (the PR title), which are never cut**". The required set becomes ≤ 3,100, still under the 3,900 budget.
- §14.3 / the strict config schema: `policy.text.title_max ≤ 256`. That matches the `copy_text` limit, keeps the bound above valid, and is refused at load otherwise.
- §5.9: the Confirm panel's copy of the title line (it shows "the card's first 3 lines", and mockup 03 includes `PR:`) follows the same rule. Either it shows the full title or the panel has no title line. Pick one and match mockup 03 to it.
- AC 1 / AC 53: a Hypothesis case where a 256-character title made of `&` and U+200B renders the card within 3,900 with the whole title inside `<code>` and no "…" there.
- Mockups 01 and 03: show the title in full (wrapped), not ellipsized.

### 2. NIT: drafts in flat mode and in ✅ Reviews are not specified, so raw Hermes text can stream unwrapped next to approval cards
Where: CR §6.1 step 2 (every turn starts a draft), U6/U12 and BA-14 (flat mode and Reviews: gatekeeper header first, body in one quote, via `answer_view`), §4.4 rule 7 (the Reviews answer is "a **classic** message … never rich").

Only final messages go through `answer_view`. A flat-mode or Reviews turn still streams `sendRichMessageDraft`/`sendMessageDraft` with Hermes text and no header, in the chat that holds the cards. Drafts have no buttons and last 30 s, so no decision is reachable from them. But they are the one place a lookalike `🧩 Ready for review … ✅` can still appear unlabeled, which is what U12 exists to prevent.

Fix: §6.1 step 2–3: "In ✅ Reviews no draft is sent; `sendChatAction(typing)` is used while waiting. In flat mode, every draft is built by `answer_view.render(…, thread_kind='flat')`: the gatekeeper header comes first and the draft text sits in one quote." Extend `test_answer_view.py`/AC 48 with a lookalike draft in flat mode.

### 3. NIT: on the normal (no-kill) publish path the Telegraph confirm panel stays at `⏳ Publishing…`
Where: CR §6.8 step 1 (the Confirm handler edits the panel to `⏳ Publishing…`), step 4 (success is a new reply to the answer), §14.5 Telegraph rows ("User sees" texts with no target named), BA-20 ("exactly one announcement").

The `/kill` paths now always resolve the panel. On the normal path, though, the panel keeps saying `⏳ Publishing…` beside the `📖 Published` reply, and the failure texts don't say whether they edit the panel or reply. Step 4's "So the panel never stays at `⏳ Publishing…`" holds only for the `/kill` cases.

Fix: step 4 and BA-20: "The shielded section always edits the panel to its outcome (`📖 Published · see reply below`, or `📖 Published (after stop) · public link · see /articles`), under the dedupe key `article:<page_path>:announce`. The normal path also sends the step-4 reply (`article:<page_path>:reply`)." Every §14.5 Telegraph failure text is a panel edit.

### 4. NIT: graceful shutdown doesn't await the shielded Telegraph request, so a created page can go unrecorded
Where: CR §12.5 item 4 (`Handlers.drain()` cancels the `KILL_CANCELS` kinds, then awaits every task in `self.tasks`), the `telegraph` kind (the request, the `telegraph_pages` write and the announcement run under `asyncio.shield(inner)`), BA-20.

`inner` is not a spawned task, so `drain()` doesn't await it. A `down.ps1` during a ~1 s `createPage` can stop the loop after Telegraph created the page and before `telegraph_pages` records it. The result is a public, undeletable page with no `/articles` entry and no Unpublish handle.

Fix: BA-20: `inner` is created with `Handlers.spawn(inner_coro, "telegraph_commit")`. `TaskKind` gains `telegraph_commit`, which is **not** in `KILL_CANCELS`, so `drain()` awaits it like `submit`. `test_telegraph.py` gains "drain during a held `createPage` → page recorded".

### 5. NIT: "packaging and every edit path already gate them, so a render-time hit is a bug" is not true for skill parts
Where: CR U14 and §12.1 `scan()` (skill parts are verbatim and scanned), §12.1 "One scanner" bullet (only packaging and the `check_text` edit paths are named), §9 (skill cards). U14 itself notes that Hermes holds `HERMES_API_KEY` and may repeat it, and Hermes writes the skills.

A Hermes-staged skill containing a gatekeeper secret value reaches the card renderer unchecked and gets the `Card withheld … (bug)` stub. The outcome is safe (nothing is activated), but the user gets a dead end labelled as a bug instead of a reason.

Fix: §9 / §12.4.2: `SkillApprovals.create` (or the FEAT-009 staging hook it wraps) runs `ctx.secret_scanner.scan` over every skill file first. On a hit it refuses with the reason `gatekeeper_secret (<file>)`, shown on a plain notice in 💬 Assistant, and creates no approval. Add the case to `test_secret_scan.py`.

### 6. NIT: `📋 Copy title` assumes the title fits `copy_text`, but the units may differ
Where: CR §5.2 section 9 and §5.8 (`copy_text`, ≤ 256 characters, the stored title unchanged), §1 table (`copy_text` 1–256 characters), `policy.text.title_max` (256 Python code points).

If Telegram counts `copy_text` in UTF-16 units, as it does entity offsets, a 256-code-point title with astral emoji is too long. The picker send then fails with 400, and the rule says the title must not be cut.

Fix: §5.8: "`📋 Copy title` is shown only when the title's UTF-16 length is ≤ 256; otherwise the button is omitted and the picker says `Title too long to copy; it is shown in full in 🧭 Explore → PR text`." Add a §17 **[VERIFY]** item for the `copy_text` length unit.

## Verified assumptions
- **`policy.check_text`** is `check_text(kind, text, bundle, confidentiality, kiro_key_sha256=None)`, with `TextKind = title|body|comment|commit_message`. It checks length, title shape, the secret patterns (defaults plus `extra_patterns`), the Kiro-key hash over `TOKENISH_RE` candidates, the confidentiality values and the disclosure marker, but **not** the gatekeeper's own secret values. A keyword-only `secret_values` parameter is a compatible, feasible addition (BA-21).
- **Production callers of `check_text`** are exactly `packaging.py` (commit message; title and body for `open_pr`) and `api/actions.py` (the `comment` kind). Both are covered by BA-21's "every production call passes `secret_values=`" AST test.
- **Packaging's patch scan** today is the Kiro-hash scan of `derived.patch` (`policy.TOKENISH_RE`, `policy.kiro_key_hashes`). The new `Violation("gatekeeper_secret", "patch")` scan fits beside it.
- **Title rendering today** (`telegram/cards.py`): `PR title: {code(view.title)}`, where `code()` = `make_visible` + `esc` inside `<code>`, the whole title with no per-line cut. The hidden-character count includes the title. `make_visible` emits `⟦U+XXXX⟧` (8 characters) for BMP invisible, bidi and control characters only.
- **`TextPolicy.title_max`** is `PosInt = 256` with no upper bound, and `config/policy.yaml` sets 256 (finding 1).
- **`confirm_text`** returns the DRY RUN sentence or `Confirm: LIVE — <what> as <login> (author <name>). Expires in 5 min.` per outbound kind, as §5.9 quotes.
- **Hold-back argument** (§6.1 step 3, BA-16): with values free of whitespace and < 256 characters, and patterns that never match whitespace and have a minimum width < 256, an incomplete secret at the tail always starts after `max(last whitespace, len − 256)`. So `draft_visible` never shows a prefix, and AC 66's "ends before the key's first character" follows.
- **Telegraph cancel race** (BA-20): `on_done` runs after the outer task finishes, and the shielded `inner` reads `outer_cancelled` only at its announcement. Whichever order they run in, exactly one announcement is enqueued, under one dedupe key.
- **CR-004 consistency**: the ceiling (`max_files: 80`, `max_changed_lines: 3000`, `max_test_lines: 3000`), `overrun_tolerance` 0.5, and `on_overrun: flag|reapprove` with `RESCOPE_REQUIRED` only for `reapprove` and the ceiling match CR-003 D18/D19 and §5.1. `config/policy.yaml` still holds the Phase 1 values (25/600), which the Phase 2 build changes. That isn't a design conflict.
- **ADR-0019** (revision 6) back-references ADR-0020, and its Option A "Redaction" paragraph matches U14 (title scanned, never redacted; packaging gates first with the same scanner). **ADR-0020** (revision 3) keeps the decision and records the round-5 changes as library-independent.
- **Mockups 01 and 07** carry revision 6's text (title as publish bytes, Copy title unchanged; Stop partials and truncated answers held back). The other mockups match the CR where revision 6 didn't change them, apart from the title ellipsis in 01 and 03 (finding 1).

## Unverified or wrong assumptions
- **Wrong (finding 1):** that the PR title is "never altered" on the card. The unchanged §12.1 budget rule can cut it with "…" or drop it, and mockups 01 and 03 show it cut.
- **Overstated (finding 5):** that a render-time `scan()` hit is always a bug. No pre-render gate covers Hermes-staged skill files.
- **Overstated (finding 3):** that "the panel never stays at `⏳ Publishing…`". That holds only on the `/kill` paths.
- **Gap (finding 4):** that the shielded section always records the page. That holds for `/kill`, but not for a graceful shutdown.
- **Unverified (finding 6):** the unit of the `copy_text` 256 limit (UTF-16 or code points).
- **Unverified (no web access this round):** the Bot API 10.1–10.3 facts, the Telegraph API facts, PyPI versions and dates, licences, PTB #5261, aiogram #1890/#1840/PR #1848, and pyTelegramBotAPI release levels and contributor counts in §19 and ADR-0020. Round 2 checked the shared ones live.
- **Unverified (already `[VERIFY]` or §17, accepted as implementation-time checks):** rich nesting inside quotes, `editMessageText` rich↔classic switching, draft rate accounting, effect IDs, how `.html` documents open, the Hermes stream/stop/session endpoints, Kiro audio, the mirror permission minimum and probe codes, the Telegraph host list, U+2060 per client, EXIF on photos and label truncation.
