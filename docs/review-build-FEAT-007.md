# FEAT-007 build review, round 4: Kiro V3 provider fork, Hermes v0.22.2, capability tiers, assistant backend

Round 4 reviews `a7480ea..6c0fcdc`, the two commits after the round 3 review, against the full FEAT-007 scope from base `d2827d8`. Both round 3 findings are fixed. The scout and reviewer agents now give the GitHub MCP wrapper literal `HOME` and proxy values. GHMCP-2 went back to `missing`, and its live in-container part is owned by item 57. The four orphaned shared rows now name FEAT-009 or FEAT-010 in their phase column and appear in item 47's or item 49's Rows, with the remaining work spelled out in the row and the item's Tests. Every gate re-ran green on the clean tree, and the rebuilt sandbox-llm image carries the committed agent files.

Watch for: CR3-AC-66 records no FEAT-007 part, although FEAT-007 built and tested the redacted `chat_turns.answer_text`. CR3-AC-66, MDL-3 and CR3-IH-4 are in no later item's Rows list, so only the phase-based matrix check reaches them (confirmed, NIT).

**Verdict**: APPROVED (0 HIGH, 0 MEDIUM, 1 NIT)

## High-level view

The scout/reviewer GitHub MCP fix keeps the token out of the agent files. Each `mcpServers.github.env` holds only `HOME=/home/agent`, where `kiro.sh` writes the 0600 token file, and the container's `http://proxy:3128`. Nothing depends on kiro-cli's `${VAR}` expansion or on it forwarding its own environment. `test_kiro_settings.py` pins the exact shape against `sandboxd.argv.PROXY_URL`. The image rebuild moved both sandbox digests and `agent_configs_sha256`, and the built image's agent files and entrypoint hash the same as the worktree. The path itself has still never run live. The matrix now says so (GHMCP-2 `missing`, live part in item 57) instead of reading `done`.

The matrix bookkeeping closes the round 3 gap. No open row that names FEAT-007 is left without a pending FEAT in its phase column, and the totals line matches a fresh count (done 262, missing 219, rework 13, deferred 28). One small bookkeeping gap remains on three rows the round 3 fix did not touch.

<details>
<summary>Issues (1)</summary>

1. **F007-R4-1 CR3-AC-66, MDL-3 and CR3-IH-4 handoff** (NIT; `docs/TRACEABILITY.md`, `docs/plan/phase2-build-plan.md`, FEAT-009/010 JSON): CR3-AC-66's evidence ends "… are FEAT-007/009" with no FEAT-007 part, although `TurnStore.done` stores `chat_turns.answer_text` redacted (`test_turns.py::test_stored_answer_is_redacted_and_bounded`, `test_backend.py::test_ask_stores_the_redacted_answer`). CR3-AC-66 and MDL-3 (FEAT-009) and CR3-IH-4 (FEAT-010) are in no item's Rows list there. Record the FEAT-007 part on CR3-AC-66, and add the three ids to item 47/49 (or the close-out items 48/53) Rows. The Phase 2 gate can do this.

</details>

<details>
<summary>Details</summary>

### Round 3 fixes

F007-R3-1 (6c0fcdc): DES-AC-13 now names FEAT-009 (item 47, `test_model_cmd.py`). CR3-AC-59, CR3-AC-67 and CR3-BA-15 name FEAT-010 (item 49), and item 49's Tests spell out the Stop-within-2 s case, the real-row AC 67/BA-15 cases with `finalize_turn(conn, turn_id)` as `on_done`, and `test_hermes_routing.py` for U12's 409. FEAT-009.json and FEAT-010.json carry the same edits. CR3-BA-15 stays `missing` on purpose: no Telegram message starts a chat turn before item 49.

F007-R3-2 (49df42c): the ADR-0002 addendum marks the `${VAR}` behaviour (expansion only for `kiroAgent.mcpApprovedEnvVars`) as `[VERIFY]`. This review did not re-check it, and the fix does not rely on it. If kiro-cli replaces the child environment instead of merging it, the wrapper still starts, because `/usr/bin/env python3` falls back to the default search path and `server_env` defaults `PATH`.

### Remaining bookkeeping (F007-R4-1, NIT, confirmed)

```
CR3-AC-66  FEAT-005 + FEAT-007 + FEAT-009   evidence: FEAT-005 part only; "... chat_turns.answer_text ... are FEAT-007/009"
MDL-3      FEAT-006 + FEAT-007 + FEAT-009   FEAT-007 part recorded; in no item's Rows
CR3-IH-4   FEAT-007 + FEAT-010              FEAT-007 part recorded; Rows of items 37/38 only, none in FEAT-010
```

The phase columns name pending FEATs, so the FEAT-009/010 close-out matrix checks will list these rows, and nothing is lost. The gap is that the FEAT-009/010 coders get no Rows pointer to them, and CR3-AC-66 under-reports work FEAT-007 already did and tested.

### What was verified, and how

- Scope: read context.json, phase2-build-plan.md (hard rules, matrix check, items 33-38 and the edited items 47, 49 and 57), FEAT-007.json (base `d2827d8`, status completed, round 3 fixes in findings), the round 3 review, and the full `git diff a7480ea..HEAD` (agents, `kiro.sh`, `test_kiro_settings.py`, `versions.lock.yaml`, ADR-0002 addendum, TRACEABILITY, VERIFICATION, FEAT-007/009/010/011 JSON). Rounds 1-3 covered the rest of the FEAT scope (provider import, V3/V2 engines, surface gate, permissions, token logging, root code origin, Hermes pins and hygiene, tiers and skill gate, attachments, vision, ADR research), and none of that code changed in this round.
- Matrix: ran the plan's matrix check for FEAT-007. It finds 20 open rows, each naming FEAT-009, FEAT-010 or FEAT-011, and none `rework`. Checked each id against the plan's `Rows:` lines (F007-R4-1 lists the three misses). The totals line matches a fresh group count.
- Tests on the clean tree at 6c0fcdc: `DEV pytest -m "unit or telegram or acp or e2e" -n auto` 2895 passed. `ruff check` clean, `ruff format --check` 335 files formatted, `mypy` no issues in 167 files, `lint-imports` 6 contracts kept. `shellcheck sandbox/entrypoints/kiro.sh` clean. The hygiene and tier system tests were not re-run, because `hermes.sh`, the units and the tier code are unchanged since their recorded passes.
- Live, read-only: Docker's image IDs for sandbox-llm, sandbox-full and proxy equal the `config/versions.lock.yaml` digests, and the repo lock equals `/etc/ossbot/versions.lock.yaml`. Inside the sandbox-llm image (`--pull never --network none`), `ossbot-scout.json`, `ossbot-reviewer.json` and `kiro.sh` have the same sha256 as the worktree. `ossbot-hermes`, `ossbot-hermes-probe.socket`, gatekeeper, sandboxd and runner are active. `ossctl doctor` gives PASS 45, with tier `self_improve`, a fresh hermes-side probe passing clean, `chat relay: enabled` and `github mcp (read-only): get_me Amsal1`. `ossctl status` shows `dry_run: true` and lifecycle up. The VERIFICATION round 4 table matches what was observed. Its PASS 46 is `--probes`, which this review did not run, to avoid model calls. This review sent no Telegram message and made no model call. One `docker run` by tag tried a registry pull before `--pull never` was added. The pull was denied, and it sent no project data.
- Secrets: `gitleaks git --redact --log-opts=--all` scanned 156 commits with no leaks. The new `env` blocks hold only a path and the proxy URL. No secret file was read.
- No GitHub write: nothing in the range touches `submit.py`/`github_writer.py`. The MCP server stays `--read-only` with the public-read token.
- Git: both commits have Amsal Khan <md.amsalkhan@gmail.com> as author and committer, and `git status --porcelain` was empty before this review's files. `origin/main` equals HEAD with "update by push" reflog entries, the same owner-push pattern accepted in round 3 under working rule 5. This review pushed nothing, and the reflog does not show who pushed.

</details>

<details>
<summary>File map</summary>

- `.kiro/agents/ossbot-{scout,reviewer}.json`: literal `env` (`HOME`, `HTTPS_PROXY`, `HTTP_PROXY`) on `mcpServers.github`
- `sandbox/entrypoints/kiro.sh`: comment on the literal env
- `tests/unit/sandboxd/test_kiro_settings.py`: pins the env against `PROXY_URL`
- `config/versions.lock.yaml`: new sandbox-full/sandbox-llm digests and `agent_configs_sha256`
- `docs/adr/0002-agent-runtime-hermes-kiro-acp.md`: round 3 fixes addendum
- `docs/TRACEABILITY.md`, `docs/plan/phase2-build-plan.md`, `docs/plan/task-ossbot-v1/features/FEAT-00{7,9}.json`, `FEAT-010.json`, `FEAT-011.json`: owners for DES-AC-13, CR3-AC-59/67, CR3-BA-15, GHMCP-2 (live)
- `docs/VERIFICATION.md`: FEAT-007 live checks round 4

Full diff: `git -C d:\Work\autonomous-oss-contributor diff a7480ea..6c0fcdc` (this round), `d2827d8..6c0fcdc` (the whole FEAT).

</details>
