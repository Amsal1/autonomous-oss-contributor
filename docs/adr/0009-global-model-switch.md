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

## Implementation addendum (FEAT-006 item 28, 2026-10-18)

- §23 item 9, checked before the parser was written. The Kiro docs do not publish the `--list-models --format json` schema. `kiro-cli chat --help` (2.32.0, run in `sandbox-llm` with `--network none`) lists `--list-models` plus `-f/--format` "for list commands". The serde names in the `kiro-cli-chat` binary show a `ModelListResult {models, default_model}` whose entries are `ModelInfo` (`model_id`, `model_name`, `description`, `context_window_tokens`, `rate_multiplier`, `rate_unit`, `token_limits`, `supported_input_types`, `prompt_caching`, ...). ACP `session/new` advertises `models.availableModels[{modelId, name, description}]` (agent-cockpit issue 105, kiro-cli 1.29). Ids use dot notation for minor versions: `claude-sonnet-4.6` works, `claude-sonnet-4-6` fails with "Invalid model ID" (kirodotdev/Kiro issue 10722). `common.models.parse_list_models` accepts `{models: [...]}`, a bare list, and `{availableModels: [...]}`. It keeps `model_id`/`modelId`/`id`, the name and `supported_input_types`, and drops anything outside the `--force` regex. The live run with the sandbox key (item 32) confirms the real shape and id spelling.
- `resolve(role)` follows `default` and role-name references, with cycles refused when the file is parsed. Effort comes from the role, then the inherited role, then `default.effort`. `require_distinct_from` moves the reviewer to the first accepted fallback that differs from the coder's model. If none differs, the model is kept and the runner flags it (§15.3). The reader caches on mtime and size. A broken file keeps the last good version (persisted, so a restart still resolves) and alerts once per broken version.
- Setting `vision` checks the catalog's `supported_input_types` when it is known: a model without `IMAGE` is refused unless `--force` is given. The first upsert into an empty catalog sends one summary ("Kiro catalog loaded: N models"); after that, each new id gets one FYI. A forced id that later appears in the catalog is not announced. `ossctl models refresh` stores a `runner_requests` row, and the runner claims it on its next tick (`POST /v1/models/refresh/claim`).
