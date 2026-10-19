Add a Kiro CLI V3 ACP adapter next to the V2 path
## Summary

Kiro CLI 2.32 ships a second ACP server generation (`kiro-cli acp --agent-engine=v3`). It rejects the v2 launch flags (`--agent`, `--model`, `--effort`, `--trust-*`), negotiates optional methods through `agentCapabilities._meta.kiro.extensionMethods`, selects mode/model/effort per session, and changes the model with `session/set_config_option` instead of `session/set_model`. The Kiro migration guide recommends keeping separate adapters while both generations are supported, so this adds a V3 adapter beside the existing V2 code instead of changing it.

- `kiro_acp/v3_core.py`: stdlib-only launch spec, `session/new`/`session/load` params, NDJSON JSON-RPC transport (one object per line, size limit, closes after repeated malformed lines), permission answers, and an optional surface check (expected mode, allowed tool ids).
- `kiro_acp/v3.py`: the provider client. One `kiro-cli acp --agent-engine=v3` process; a fresh session per completion (Hermes re-sends the transcript, as with the V2 path); model and effort from an optional models file, applied through `_meta.kiro` on `session/new` and `session/set_config_option` when the session reports another value; image parts become ACP image blocks when `promptCapabilities.image` is advertised; a dropped transport is reloaded with `session/load` and the prompt is not resent; `--auth-method=client` answers `_kiro/auth/getAccessToken` and keeps the token out of every log record.
- `kiro_acp/hermes_plugin.py` and two small hooks in `__init__.py`: `HERMES_KIRO_ACP_ENGINE=v3|v2` (default `v3`). `kiro_acp_client.py` and `kiro_acp_models.py` are unchanged.

Configuration: everything is set through `HERMES_KIRO_ACP_*` environment variables (`CLI`, `HOME`, `CWD`, `WORKSPACE`, `PROXY`, `EXPECTED_MODE`, `ALLOWED_TOOLS`, `AUTH`, `TOKEN_FILE`, `MODELS_FILE`, ...). The defaults are generic: `kiro-cli` under `~/.local/bin`, working directories under `~/.hermes`, no proxy, and the custom Kiro agent `hermes-chat` as the expected session mode.

Security notes: the V3 client advertises no client capabilities, so the agent gets no file or terminal bridge (V2 offered `fs/read_text_file`/`fs/write_text_file` within the cwd). Every `session/request_permission` is answered with a reject option or `cancelled`; options are never synthesized and no consent metadata is returned. Unknown agent-to-client requests get `-32601`.

## Testing

Contract tests against a scripted fake V3 server (stdio NDJSON): launch argv, capability gating, `session/new` params, model switch without restart, image blocks, permission answers, token redaction, unknown requests, reconnect without resend, engine selection. Also used live against kiro-cli 2.32.0.

## AI disclosure

Written with an AI coding assistant (Kiro) and reviewed by me before submission.
