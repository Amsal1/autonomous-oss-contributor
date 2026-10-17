# ADR-0009: Global model switch via config/models.yaml

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §16

## Context
New Kiro models appear often (catalog on 2026-10-17 includes Claude Opus 5.5, Sonnet 5.5, Fable 5.1 preview, GPT-5.6 tiers). One place must control which model each role uses. Kiro V3 headless continues with a warning if it cannot set the requested model.

## Decision
- `config/models.yaml` (git-tracked) holds `default {model, effort}`, per-role entries (chat, planner, vetter, coder, reviewer, summarizer), `fallbacks`, catalog settings; reviewer has `require_distinct_from: coder`.
- Every pipeline kiro-cli invocation passes `--model` and `--effort` explicitly, resolved fresh from the file (no restart). The chat role renders Hermes's provider args and the watchdog restarts Hermes on change.
- Writers: `ossctl model set` and Telegram `/model` (confirm) both go through the gatekeeper, which rewrites the file atomically with ruamel.yaml (comments kept) and audits it.
- Daily catalog job runs `kiro-cli chat --list-models --format json` in a sandbox and notifies new ids with "Set as default / role / Ignore" buttons.
- The runner records the actual model from stream-json and flags mismatches.

## Consequences
The file can be dirty in git after a Telegram switch; `--commit` option provided. **Verify:** list-models JSON schema and id spelling.
