# ADR-0003: A deterministic gatekeeper is the only GitHub write-token holder

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §8, §12

## Context
LLM sessions read hostile text (issues, comments, repo files, web pages). If any LLM-adjacent process can perform GitHub writes or decide approvals, prompt injection can publish under `Amsal1`.

## Decision
A small Python service (`ossbot-gatekeeper`, Unix user `gatekeeper`) is the trusted computing base:
- only process that can read the classic PAT; only module calling write APIs is `gatekeeper/submit.py` (import-linter enforced);
- owns the contribution state machine, approvals (bound to `binding_hash` over kind, repo, base/head SHA, bundle hash, title/body hashes, policy hash), rate limits, policy checks, kill/pause/dry-run state (dry-run default ON), hash-chained SQLite audit log;
- exposes a Unix-socket API with roles from `SO_PEERCRED` (operator, worker, sandbox, requester). There is no approve endpoint at all; approval exists only as a Telegram two-tap flow.
- Asymmetric control: any role may request stop; only the human may request go.

## Alternatives
- Let Hermes hold the token with "careful prompts": rejected (prompting is not a control).
- Put the pipeline runner and gatekeeper in one process: rejected (runner parses large untrusted LLM output; keep the TCB small).

## Consequences
More moving parts (runner ↔ gatekeeper API). Every public action is re-validated at execution time, so stale approvals fail closed.
