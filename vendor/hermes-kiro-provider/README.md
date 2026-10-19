# hermes-kiro-provider

Use the [Kiro CLI](https://kiro.dev/) (AWS's agentic coding CLI) as a model
provider inside [Hermes Agent](https://github.com/NousResearch/hermes-agent) —
Kiro's agent runs as an ACP subprocess behind Hermes' normal provider
interface, so `hermes -m kiro-acp`, `/model`, profiles, and the gateway all
work like any other backend.

Shipped as a **standalone plugin repo** per the upstream vendor-integration
policy ([hermes-agent#80362](https://github.com/NousResearch/hermes-agent/pull/80362)):
zero edits to hermes-agent core; it rides the documented
`auth_type="external_process"` provider seam
([Model Provider Plugins → External-process (ACP) providers](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin)).

## Prerequisites

- hermes-agent installed and working (`hermes doctor` clean)
- [Kiro CLI](https://kiro.dev/docs/cli/) installed and **logged in**
  (`kiro-cli` on PATH; login state in `~/.kiro`)

Verified against Kiro CLI 2.22.0 (`kiro-cli acp`, ACP protocol v1 over stdio).

## Install

**Via the plugin installer** (recommended):

```bash
hermes plugins install tobenwarrior/hermes-kiro-provider
```

**Or manual drop-in**:

```bash
git clone https://github.com/tobenwarrior/hermes-kiro-provider.git
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers"
cp -r hermes-kiro-provider "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/kiro-acp"
```

Both paths register the `kiro-acp` provider for the next session.

## Use

```bash
hermes -m kiro-acp "what can you do?"
```

Or pick **Kiro (AWS)** in `hermes model` / `/model`. The provider name and
aliases are `kiro-acp`, `kiro`, `kiro-cli`.

Auth is handled by the `kiro-cli` subprocess itself (your existing `~/.kiro`
login) — no API keys to configure.

## Configuration (optional env overrides)

| Env var | Purpose | Default |
|---|---|---|
| `HERMES_KIRO_ACP_COMMAND` | Path to a custom kiro binary | `kiro-cli` |
| `KIRO_CLI_PATH` | Older alias for the same | — |
| `HERMES_KIRO_ACP_ARGS` | Extra argv (shlex-split), e.g. `"acp --agent builder --effort high"` | `acp` |

## How it works

`KiroACPProfile` is a `ProviderProfile` with `auth_type="external_process"`
that spawns `kiro-cli acp` and adapts it to Hermes' OpenAI-style client
interface by extending the in-tree `copilot-acp` ACP shim (`agent.copilot_acp_client`)
— session lifecycle, streaming updates, permission handling, and the
file-safety bridge are inherited, so upstream fixes flow through. Model
listing is delegated to the subprocess; `model="kiro-acp"` means "use the
session default model".

## Development

```bash
# unit + discovery tests (need a hermes-agent checkout; defaults to ~/.hermes/hermes-agent)
HERMES_REPO=/path/to/hermes-agent python -m pytest tests/ -q
```

Tests cover the profile contract (registration, env overrides, client
construction) and real discovery from an isolated `$HERMES_HOME` — no live
Kiro calls.

## License

MIT
