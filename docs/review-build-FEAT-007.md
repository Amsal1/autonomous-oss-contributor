# FEAT-007 build review, round 3: Kiro V3 provider fork, Hermes v0.22.2, capability tiers, assistant backend

Round 3 reviews `15299f1..a001f39`, the 11 commits since the round 2 review, against the full FEAT-007 scope from base `d2827d8`. All six round 2 findings are fixed, and the fixes hold live. The food photo now returns an estimate, because Hermes sends images natively and the provider's completion is awaitable. Hermes' plugin tools reach the gatekeeper through a lenient `<tool_call>` parse that accepts offered tools only. The read-only GitHub MCP server (GHMCP-1..4) is built for scout, self-review and Hermes. The probe reader moved to `ossbot.common` behind a sixth import contract, SSE lines and deltas are bounded, and pytest-xdist is pinned.

Watch for: four shared matrix rows (DES-AC-13, CR3-AC-59, CR3-AC-67, CR3-BA-15) name only closed FEATs but are still `missing`. Their remaining parts (`/model`, Stop via `stopped_message_generation`, the U12 409 refusal and the post-`/resume` turn) have no owning FEAT or plan item (confirmed, MEDIUM). The scout/reviewer side of the GitHub MCP server has never run live (NIT).

**Verdict**: NEEDS_CHANGES (CHANGES_REQUESTED: 0 HIGH, 1 MEDIUM, 1 NIT)

## High-level view

The provider fork changed only at the Hermes edge. Images go to Kiro as ACP image blocks, now that the managed config sets `model.supports_vision` and `agent.image_input_mode: native`, so Hermes no longer pre-describes them through an auxiliary call that crashed. Tool calls the model writes in non-OpenAI shapes are parsed leniently, only for tools Hermes offered in that completion, and only metadata is logged.

The GitHub MCP server is one stdlib wrapper used by both clients. It starts the pinned v2.0.2 binary with fixed `--read-only` flags, and every start first runs a `tools/list` check that refuses any tool without `readOnlyHint` or with a write verb (fail closed). The public-read token reaches only research-profile `kiro` jobs of scout and reviewer, and sandboxd's argv check and the Docker filter both enforce that. It also reaches Hermes through `hermes.env`, and its sha256 joins the key scans. Hermes' `/v1/toolsets` does not report MCP tools at v0.22.2, so on the Hermes side the guards are the wrapper check and `tools.include`. The Hermes path was exercised live. The Kiro-in-container path was not.

The matrix bookkeeping has one gap. Four shared rows recorded the FEAT-007 part but kept `missing` status, and no later FEAT is named for what remains.

<details>
<summary>Issues (2)</summary>

1. **F007-R3-1 Orphaned shared rows** (MEDIUM; `docs/TRACEABILITY.md`, `docs/plan/phase2-build-plan.md`): DES-AC-13, CR3-AC-59, CR3-AC-67 and CR3-BA-15 name only FEAT-005/006/007 (all completed) and stay `missing`. The remaining work (`/model`, `stopped_message_generation` ending a stream within 2 s, U12's 409 for a crashed `streaming` session, the next turn after `/resume`) is in no FEAT-009/010 item's Rows list. Set each row `done` if nothing remains. Otherwise add the owning FEAT to its phase column and the row id to that item's Rows (item 49 for the Stop/U12 parts, the `/model` item of FEAT-009 for DES-AC-13).
2. **F007-R3-2 Scout/reviewer GitHub MCP path untested live** (NIT; `.kiro/agents/ossbot-{scout,reviewer}.json`, `sandbox/entrypoints/kiro.sh`, `docs/TRACEABILITY.md` GHMCP-2): the wrapper in the container reads the token from `$HOME/.ossbot/github-read-token` and needs `HTTPS_PROXY`. Both depend on kiro-cli passing its environment to MCP children, which nothing has shown, and a miss fails closed into a scout with no GitHub tools. Literal `env` values (`HOME`, the 3128 proxy URL) need no `${VAR}` expansion and could go in the agents' `mcpServers.github`. Run one research-profile scout or reviewer job in the Phase 2 gate.

</details>

<details>
<summary>Details</summary>

### Shared rows with no owner left (F007-R3-1, MEDIUM, confirmed)

The matrix check for FEAT-007 (the build plan's PowerShell filter) lists 19 open rows. Fifteen also name FEAT-009, FEAT-010 or FEAT-011, which is expected. Four name only completed FEATs:

```
DES-AC-13  P2 FEAT-006 + P2 FEAT-007            remaining: /model (row text: "remain (FEAT-007/009)")
CR3-AC-59  P1-rework FEAT-005 + P2 FEAT-007     remaining: stopped_message_generation ends the stream within 2 s
CR3-AC-67  P1-rework FEAT-005 + P2 FEAT-007     remaining: next turn after /resume answered; crashed streaming row -> U12 409
CR3-BA-15  P1-rework FEAT-005 + P2 FEAT-007     both parts recorded as done, status still missing
```

`stopped_message_generation` is build-plan item 49 (FEAT-010), and `/model` belongs to FEAT-009. Neither item lists these row ids, and the FEAT-005 close-out set the rule "no row whose phase names only" a closed FEAT. As things stand, only the final gate's `$f='.'` check would surface these four rows, after FEAT-009/010 have closed without testing those acceptance criteria. CR3-BA-15 looks complete on its own evidence and may only need its status set.

### GitHub MCP inside the scout container (F007-R3-2, NIT, likely)

`kiro.sh` writes the token to a 0600 file under `$HOME/.ossbot` (denied to the read tool in both agents) and unsets it before kiro-cli starts. The agents' `mcpServers.github` has no `env`, so the wrapper gets `HOME` and the proxy variables only if kiro-cli forwards its own environment. The Hermes config sets them explicitly, and that path passed live. VERIFICATION and the GHMCP-2 row say the scout run was not forced (opus-priced). GHMCP-2 still reads `done`, although its runtime path has hermetic evidence only.

### What was verified, and how

- Scope: read context.json, phase2-build-plan.md (items 33-38, working rules 1-9, the GHMCP addition), FEAT-007.json (base `d2827d8`, status completed, round 3 findings), the DESIGN §15.3/§15.5 changes in this range, the ADR-0002 round 2-fixes addendum, the TRACEABILITY diff and the GHMCP, CR2-3f, CR2-7f, CR2-7i, CR3-IH-4, DES-V-30 and DES-AC-20 rows. Read the full `git diff 15299f1..HEAD` code paths: `kiro_acp/v3.py`, `sandbox_tools/github_mcp.py`, `common/{github_mcp_tools,probe_result}.py`, sandboxd `argv/main/profiles/runtime`, `dockerfilter/rules.py`, `chat/{backend,hermes_api,code_jobs,hermes_surface}.py`, `ctl/{doctor,hermes_render}.py`, `hermes/config/config.yaml.j2`, `scripts/lib/{hermes,kiro,images}.sh`, `sandbox/entrypoints/kiro.sh`, `scripts/setup-secrets.sh`, the agent JSONs and the new tests (`test_tool_bridge.py`, `test_github_mcp.py`, `test_doctor_github_mcp.py`, argv/rules/surface cases).
- Round 2 fixes: F007-R2-1 (d3d08fe, bc505fd), F007-R2-2 (bc505fd, d3d08fe, 757b0bb), F007-R2-3 (4d6fcb2, cf37562, c66c846, 38c7423), F007-R2-4 (757b0bb, b7b5ab6 contract "The gatekeeper never imports ossctl or the root probe"), F007-R2-5 (757b0bb `bounded_lines`, deltas capped at `MAX_ANSWER`), F007-R2-6 (b7b5ab6 pytest-xdist 3.8.0).
- Safety by reading: the lenient parse accepts only tools offered in that completion, drops every call when none were offered, and logs reason codes and decoder messages, never text. The V3 launch env is built from scratch, so the read token now in Hermes' environment does not reach kiro-cli. The read token is name-only in argv and gated twice, by `github_read_allowed` and the filter's `env_read_token_not_allowed`, so fetch/validate/code jobs never get it. The wrapper passes only named variables and refuses an empty listing. `setup-secrets` never copies the write token to sandboxd or Hermes (selftest case). No unit was added.
- Tests (re-run on the clean tree at a001f39): `DEV pytest -m "unit or telegram or acp or e2e" -n auto` 2895 passed in 78 s. `ruff check` clean, `ruff format --check` 335 files formatted, `mypy` no issues in 167 files, `lint-imports` 6 contracts kept, `shellcheck` clean on the five changed shell files. The hygiene and root suites were not re-run: their round 3 evidence was recorded after the `hermes.sh` change, and nothing doubtful stood out.
- Live, read-only (`.ossbot-local/review-f007-r3-root.sh` as root): the deployed `src`, `vendor`, `hermes/plugins/ossbot` and `hermes/config` trees equal the worktree. All seven Hermes/gatekeeper/runner/sandboxd units are active. As hermes: `kiro-cli 2.32.0`, `Hermes Agent v0.22.2 … d923a5eb487a`, `github-mcp-server` 2.0.2 (root 0755, wrapper identical to the app copy). The tier is `self_improve`. `/etc/hermes/config.yaml` (root 0644) has `supports_vision`, `image_input_mode: native`, `mcp-github` and the always-disabled toolsets, and only the `${GITHUB_READ_TOKEN}` placeholder. The probe result is `pass/clean`. The provider tool log shows `offered=47 … parsed=1` lines, matching VERIFICATION. Hermes logs and the four service journals contain 0 token-shaped strings. `ossctl status` shows `dry_run: true`, lifecycle up. Doctor gives PASS 45, including `github mcp (read-only): get_me Amsal1` and `chat relay: enabled`. `~hermes/.hermes/ossbot-features/` still does not exist (no live feature-store write; hermetic coverage only).
- Live records: the VERIFICATION round 3 table matches what is observable. FINAL_REPORT "User action needed" lists none, which fits. This review sent no Telegram message.
- Secrets: `gitleaks git --redact --log-opts=--all` scanned 153 commits with no leaks. No secret file was read.
- No GitHub write: nothing touches `submit.py`/`github_writer.py`. The MCP server runs `--read-only` with a public-read token. Dry-run is ON.
- Git: all 11 commits have Amsal Khan <md.amsalkhan@gmail.com> as author and committer, and `git status --porcelain` is empty. `origin/main` equals HEAD (a001f39). The reflog shows "update by push" about 20 s after each commit. Working rule 5 makes owner pushes expected, and no remote commit is missing locally, so this is not a finding. The vendor tree's upstream files are unchanged since b595c28, apart from the additions recorded in round 1.

</details>

<details>
<summary>File map</summary>

- `vendor/hermes-kiro-provider/kiro_acp/v3.py`: lenient `<tool_call>` parse for offered tools, tool preamble only when offered, metadata tool log, awaitable completion
- `hermes/config/config.yaml.j2`, `ctl/hermes_render.py`, `chat/hermes_surface.py`, `scripts/lib/hermes.sh`: native images, `mcp_servers.github` above locked, `mcp` extra, host install of the MCP binary and wrapper
- `sandbox_tools/github_mcp.py`, `common/github_mcp_tools.py`: the read-only wrapper and its 26-tool list
- `sandboxd/{argv,main,profiles,runtime/*}.py`, `dockerfilter/rules.py`, `sandbox/entrypoints/kiro.sh`, `sandbox/images/llm/Dockerfile`, `scripts/lib/{kiro,images}.sh`, `.kiro/agents/ossbot-{scout,reviewer,chat}.json`, `tests/unit/dockerfilter/corpus/recorded.json`, `config/versions.lock.yaml`: read token for scout/reviewer, baked binary, new digests
- `scripts/setup-secrets.sh`, `ctl/doctor.py`: token copies and hash, doctor `github mcp (read-only)`
- `common/probe_result.py`, `gatekeeper/{context.py,api/chat.py,chat/backend.py,chat/hermes_api.py,chat/code_jobs.py}`, `hermes/plugins/ossbot/tools.py`: probe reader in common, bounded SSE, input paths by attachment id
- `pyproject.toml`, `uv.lock`: pytest-xdist, sixth import contract
- `tests/**`: tool bridge, GitHub MCP wrapper/doctor/argv/rules/surface, backend bounds
- `docs/{DESIGN,TRACEABILITY,VERIFICATION,FINAL_REPORT}.md`, `docs/adr/0002…`, FEAT-007.json: decisions, rows, round 3 live checks

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 15299f1..a001f39` (this round), `d2827d8..a001f39` (the whole FEAT).

</details>
