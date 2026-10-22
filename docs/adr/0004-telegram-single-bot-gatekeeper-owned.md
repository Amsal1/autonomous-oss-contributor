# ADR-0004: One Telegram bot, owned by the gatekeeper; Hermes reached via its local API
Status: Accepted (amended in design rev 5 on 2026-10-18; revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §14

## Context
The user wants one dedicated bot for everything. Approvals and kill must be deterministic. The Bot API delivers each update to one consumer per token (a second `getUpdates` poller gets a Conflict; webhooks need public HTTPS). Whoever polls the token sees every callback, including Approve taps.

## Decision
- The gatekeeper polls the single bot (python-telegram-bot 22.x, through proxy port 3150). Commands and callbacks are handled deterministically. Only updates from the configured user ID in that user's private chat are accepted. Callback data is an opaque single-use 128-bit token bound to the message ID; approvals need Approve then a Confirm whose text states LIVE or DRY RUN.
- The PR body and commit message are shown verbatim (escaped `<pre>`); Approve appears only after every body part was delivered.
- Non-command text is relayed to Hermes's API server (`127.0.0.1:8642`, bearer key, `X-Hermes-Session-Key`); replies are rendered as escaped text with no buttons. Windows cannot reach that port (`.wslconfig localhostForwarding=false`).
- Fallback `telegram.mode: dual`: Hermes's native gateway on a second chat-only bot, same single allowlisted user; approvals stay on the gatekeeper bot.

## Alternatives
- Hermes owns the single bot and forwards callbacks: rejected; approval taps would transit an LLM-tool process that could replay or synthesize them.
- Two bots from the start: safe and simple, but contradicts "one bot for everything"; kept as fallback.

## Consequences
Hermes-native Telegram features (inline model picker, clarify buttons, streaming edits) are unavailable in single mode. The relay is behind `ChatBackend`, so switching is configuration only. **Verify:** Hermes `/v1/responses` session semantics against `/v1/capabilities` at v0.22.2.

## Revision 5 amendment (consolidation, 2026-10-18)

Merged from CR-003 (DESIGN §14.6–§14.25; the amendments CR-003 §14.8 asked for).

- Follow-up buttons are allowed as gatekeeper tokens whose single effect is to send the button's visible label (≤ 64 characters, byte for byte) to Hermes as the user's next message (D9). This amends "never with buttons".
- Hermes answers are streamed through drafts and rendered by the gatekeeper from typed blocks; untrusted text is never markup (D8, U1). Placement and labelling follow U6 and U12.
- `allowed_updates += stopped_message_generation`, authorized by `authorized_chat_only()` (U10); `authorized()` is unchanged.
- The Approve-after-parts rule also holds for re-sent cards (delta 7).
- Token use: view and navigation tokens are multi-use until the approval ends (or 24/72 h for kit views), still message-bound, owner-only and opaque; every decision token (Approve, Confirm, Cancel, Reject, Start work, skill activation, Publish) stays single-use (delta 3).
- The library stays python-telegram-bot 22.8 with a typed Bot API 10.1–10.3 adapter over `do_api_request` (ADR-0020). Long polling only; no webhook, no inbound port, no Mini App in v1 (user decision 2026-10-18).

## Implementation addendum: copyable ids (2026-10-22, post-v1 OD-C3)

Owner request (message 12): every id one tap to copy. Telegram copies `code`/`pre` text on a tap, so contribution, approval, proposal, action and hold ids, commit SHAs, branch names and PR number references render as `<code>` (classic) or a `code` node (rich), only through `telegram/ui/ids.py` (`k.Id`, `ids.html`, `ids.autocode`, and the renderers for ids in `Const` template text). FYIs become classic HTML built by `ids.autocode` from the caller's plain text: escaped, no caller markup, still never control (U1, I8). Untrusted `Text` and publish bytes (`Verbatim`, U8/I12) are never rewritten; a branch name is redacted and escaped before it is wrapped. Toasts and button labels stay plain. A test lint (`tests/telegram/idcheck.py`, wired into every `TgRig` test and the demo transcript) fails on any prefixed id shown outside code. No invariant changes: approvals, tokens, bindings and the confirm texts are untouched.
