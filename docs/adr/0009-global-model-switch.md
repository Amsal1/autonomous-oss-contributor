# ADR-0009: Global model switch via config/models.yaml
Status: Accepted (amended in design rev 5 on 2026-10-18; revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §15.2, §16

## Context
New Kiro models appear often (catalog on 2026-10-17 includes Claude Opus 5.5, Sonnet 5.5, Fable 5.1 preview, GPT-5.6 tiers). One place must control which model each role uses. Kiro V3 headless continues with a warning if it cannot set the requested model. V3 ACP changes the model per session with `session/set_config_option configId:"model"`.

## Decision
- `config/models.yaml` (git-tracked) holds `default {model, effort}`, per-role entries (chat, planner, vetter, coder, reviewer, summarizer), `fallbacks`, catalog settings; reviewer has `require_distinct_from: coder`. It is the one config file read live from the repo mount, because it only selects models.
- Every pipeline kiro-cli invocation passes `--model` and `--effort`, resolved fresh (no restart).
- Hermes's V3 ACP adapter compares the resolved chat model/effort before each turn and sends `set_config_option` on change, so the chat switch is live. Only the V2 fallback re-renders env and restarts Hermes.
- Writers: `ossctl model set` and Telegram `/model` (confirm) through the gatekeeper, atomic ruamel.yaml rewrite (comments kept), audited. Ids must exactly match the catalog; `--force` (ossctl only) accepts a looser regex.
- Daily catalog job (`kiro-cli chat --list-models --format json` in a sandbox) notifies new ids with "Set as default / role / Ignore".
- The runner records the actual model from stream-json and flags mismatches.

## Consequences
The file can be dirty in git after a Telegram switch; `--commit` option provided. **Verify:** list-models JSON schema, id spelling, and the values advertised in the ACP `model` option.

## Revision 5 amendment (consolidation, 2026-10-18)

Merged from CR-002 Amendment A and CR-004 (DESIGN §16.1).

- New roles `vision` (image turns of the assistant; must be an image-capable catalog id) and `scout` (CR-004 scout stage; inherits `planner` until set). `/model` and `ossctl model set` switch them like the others.
- Re-affirmed user decisions: `config/models.yaml` plus `/model` is the one global switch, new catalog models produce one Telegram card each, every role web-searches unknowns, and token usage is unlimited.
