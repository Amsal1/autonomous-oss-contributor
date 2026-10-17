# ADR-0004: One Telegram bot, owned by the gatekeeper; Hermes reached via its local API

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §14

## Context
The user wants one dedicated bot for everything. Approvals and kill must be deterministic. The Bot API delivers each update to one consumer per token (a second `getUpdates` poller gets a Conflict; webhooks need public HTTPS). Whoever polls the token sees every callback, including Approve taps.

## Decision
- The gatekeeper polls the single bot (python-telegram-bot 22.x). Commands and callbacks are handled deterministically. Only updates from the configured user ID in that user's private chat are accepted. Callback data is an opaque single-use 128-bit token bound to the message ID; approvals need Approve then Confirm.
- Non-command text is relayed to Hermes's OpenAI-compatible API server (`127.0.0.1:8642`, bearer key) with a stable session key; replies are rendered as escaped text with no buttons.
- Fallback `telegram.mode: dual`: Hermes's native gateway on a second chat-only bot, same single allowlisted user; approvals stay on the gatekeeper bot.

## Alternatives
- Hermes owns the single bot and forwards callbacks: rejected; approval taps would transit an LLM-tool process that could replay or synthesize them.
- Two bots from the start: safe and simple, but contradicts "one bot for everything"; kept as fallback.

## Consequences
Hermes-native Telegram features (inline model picker, clarify buttons, streaming edits) are not available in single mode. The relay is behind a `ChatBackend` interface so switching is configuration only. **Verify:** Hermes API session semantics (`/v1/responses` or `/v1/runs` with `X-Hermes-Session-Key`).
