# Phase 2 build plan (ossbot v1)

This is the build-ready sequence for Phase 2. It implements `docs/DESIGN.md` revision 5 (the only spec; CR-001…CR-004 are merged and historical) as sequenced by `docs/PLAN.md` items 22–57, and it closes every open row of `docs/TRACEABILITY.md`. Item numbers match PLAN.md so the FEAT files (`docs/plan/task-ossbot-v1/features/FEAT-005…011.json`) and the matrix stay in step; item 23a is new (planner decision B2). Every item names the rows it closes, the files and functions it touches, the tests it adds or updates, and how to verify it.

Baseline, checked by the planner on 2026-10-18 at commit `e0ecf11`: `DEV pytest -m "unit or telegram or acp or e2e"` → 1557 passed, 11 deselected (no acp/e2e tests exist yet). The matrix has 508 rows (its "Totals" line says 507; `CR3-v1.x` was miscounted): 110 done, 26 deferred, 304 missing, 68 rework. Every open row is named by an item below (checked by script; `CR3-v1.x`, which PLAN.md missed because its cell contains an escaped `|`, is added to item 52). Each close-out commit updates the "Totals" line.

## Hard rules for every coder and reviewer

1. Dry-run stays ON for the whole build. Never run `/dryrun off`, `ossctl dryrun off` or anything that flips it.
2. Zero live GitHub writes: no fork, push, PR, comment, issue claim, star, review, mirror push or gist. GitHub write paths are tested only against `tests/fakes/fake_github.py`. Live GitHub use is read-only (`ossctl targets verify`, discovery reads, doctor probes).
3. Telegram messages go only to the configured owner (`profile.telegram.user_id`), only through product code (gatekeeper outbox, `ossctl` subcommands).
4. Never print, log, `cat`, grep or commit secret values. Never read `/etc/ossbot/secrets/**` yourself; only product code running as its own uid (or root product code) does. Refer to secrets by key name. gitleaks must stay clean.
5. Build and test inside the WSL distro `ossbot` only (prefixes below). Redeploy with `powershell -NoProfile -File d:\Work\autonomous-oss-contributor\scripts\bootstrap.ps1 -Only provision` before any live check, system test or doctor run against changed code; the services run the deployed copy in `/opt/ossbot/app`. A second redeploy must print only `unchanged`.
6. Hermetic tests are required for every code change wherever the repo has a suite (it does: pytest unit/telegram/acp/e2e, root sandbox/e2e_docker, shell system tests, PowerShell tests). Lint gate on every Python change: `DEV ruff check src tests ; DEV ruff format --check src tests ; DEV mypy ; DEV lint-imports`. `ROOT shellcheck <file>` on every changed shell file.
7. Git: Windows git only, from `d:\Work\autonomous-oss-contributor`. Before every commit: `Remove-Item Env:GIT_AUTHOR_NAME,Env:GIT_AUTHOR_EMAIL,Env:GIT_COMMITTER_NAME,Env:GIT_COMMITTER_EMAIL -ErrorAction SilentlyContinue`, then `git var GIT_AUTHOR_IDENT` must start with `Amsal Khan <md.amsalkhan@gmail.com>`. Stage files by name. Conventional Commits. Never push, pull, fetch, add remotes, amend, rebase, force or rewrite history; never change git config; never skip hooks. If `.git/index.lock` exists, wait and retry. `git status --porcelain` is empty after each FEAT's final commit.
8. Never overwrite pre-existing `.kiro/` files (only `.gitkeep` exists today). No personal values in tracked files outside `docs/` (owner values only in gitignored `config/profile.yaml`; tests use `tests/fixtures/profile.test.yaml`).
9. No exposure (v1): no Mini App, no tunnel (cloudflared, ngrok, Tailscale Funnel/serve), no webhook, no inbound port.
10. DESIGN invariants I1–I13 (§8.3) are non-negotiable. LLM and Hermes output is untrusted data: parsed through pydantic (`extra=forbid` unless DESIGN says otherwise), rendered escaped, never executed. argv lists only, never `shell=True`. Only `gatekeeper/submit.py` imports `gatekeeper/github_writer.py`; every new privileged writer (`review_mirror_writer.py`, `telegraph_writer.py`) gets its own import-linter contract.
11. A `§23 [VERIFY]` item is checked (web search or live probe) before the code that depends on it, and the result goes into `config/versions.lock.yaml` or an "Implementation addendum" of the named ADR. If a check makes a design assumption false, stop and ask (workflow `send_message` severity `warning`) instead of shipping something weaker.
12. If the only way to satisfy a requirement or review finding changes what the user sees or how the system behaves beyond DESIGN revision 5, do not make the change: ask with `send_message` severity `warning` (finding, change, user-visible effect, any behaviour-preserving alternative) and wait.

## Command prefixes (Windows PowerShell 5.1, `$env:WSL_UTF8=1` set first)

| Name | Expands to |
|---|---|
| `DEV` | `wsl.exe -d ossbot -u ossadmin --cd /mnt/d/Work/autonomous-oss-contributor --exec /usr/bin/env UV_PROJECT_ENVIRONMENT=/home/ossadmin/.venvs/ossbot /opt/ossbot/bin/uv run --frozen` |
| `ROOTDEV` | `wsl.exe -d ossbot -u root --cd /mnt/d/Work/autonomous-oss-contributor --exec /usr/bin/env UV_PROJECT_ENVIRONMENT=/root/.venvs/ossbot /opt/ossbot/bin/uv run --frozen` |
| `ROOT` | `wsl.exe -d ossbot -u root --cd /mnt/d/Work/autonomous-oss-contributor --exec` |
| `STREAM <script> <args>` | `(Get-Content -Raw <script>) -replace "`r","" \| wsl.exe -d ossbot -u root -- bash -s -- <args>` |
| `REDEPLOY` | `powershell -NoProfile -File d:\Work\autonomous-oss-contributor\scripts\bootstrap.ps1 -Only provision` |

Never use `wsl ... -- bash -c "..."` with embedded quotes. Dependency changes (pin with `==`): `wsl.exe -d ossbot -u ossadmin --cd /mnt/d/Work/autonomous-oss-contributor --exec /opt/ossbot/bin/uv lock`, then sync both dev venvs with the DEV and ROOTDEV environments but `uv sync --frozen` instead of `uv run --frozen` (e.g. `wsl.exe -d ossbot -u ossadmin --cd /mnt/d/Work/autonomous-oss-contributor --exec /usr/bin/env UV_PROJECT_ENVIRONMENT=/home/ossadmin/.venvs/ossbot /opt/ossbot/bin/uv sync --frozen`), and commit `uv.lock`.

Matrix check (bookkeeping, used at every close-out; prints the open rows that name a FEAT):

```powershell
$f='FEAT-005'; Get-Content -Encoding UTF8 docs\TRACEABILITY.md | ? { $_ -match '^\| [A-Z0-9]+-' } | % { $c=$_ -split '(?<!\\)\|'; [pscustomobject]@{id=$c[1].Trim();phase=$c[4].Trim();status=$c[7].Trim()} } | ? { $_.phase -match $f -and $_.status -in 'missing','rework' } | ft -AutoSize
```

## Planner decisions for this build (one line of rationale each)

- B1. The token-core deltas 1–9, the decision services and the non-blocking handler budget (`/kill` within 2 s) stay in FEAT-005, not in the Telegram FEATs. `/kill` latency is a kill-switch property, and the proposal and skill cards of FEAT-006/007/009 are built on these services; PLAN Decision 13 already fixed this and the consolidation review approved it.
- B2. New item 23a puts the Hermes capability-tier control plane (config default `self_improve`, fail-closed flag reader, per-tier allowlist table, `POST /v1/control/hermes-tier`, `ossctl hermes tier`) in FEAT-005 next to the egress-mode flag, which uses the same pattern. Applying a tier (rendering Hermes config, restart, probe, skill gate) needs Hermes installed and stays in item 36. This matches the requested order (safety rework first) without building anything twice.
- B3. Egress modes are data with a fixed shape: base lists carry no `web_public` for `research`/`hermes`; `modes.{allowlist,research,open}` maps a closed set of grantees (`research`, `hermes`, `agent-llm`, `catalog`) to added categories; pydantic `Literal` keys reject `fetch`, `validate`, `runner`, `gatekeeper`, and the renderer asserts the §11.5 invariants on top. Data alone can't widen a code-running profile, and the assertions catch a renderer bug.
- B4. A mode switch re-renders `/etc/ossbot/proxy/squid.conf` atomically and restarts `ossbot-proxy.service`. The conf is a read-only single-file bind mount, so an atomic rename is invisible to the running container and `squid -k reconfigure` would keep the old rules.
- B5. The diff ceiling counts every changed path toward `max_files` (excluded globs included), while `size.exclude_globs` drops paths only from the two line totals. A runaway agent rewriting hundreds of lockfiles must still hit the file ceiling.
- B6. `policy.check_diff(entries, policy)` loses its `target` parameter, and its caller `packaging.py:282` is updated. Nothing target-level may lower or raise the ceiling (DESIGN §12.6).
- B7. Backup and restore stay in FEAT-008 (the include list depends on the Hermes state layout from FEAT-007); FEAT-011 documents them in SETUP.md and refreshes `config/profile.example.yaml` with every key Phase 2 adds.
- B8. The scout role is added to `sandboxd/profiles.py` (`ResearchRole`, `KiroRole`, `ROLE_REPO_MODE`, `TRUST_TOOLS`) in item 30; today those literals stop at vetter/planner/reviewer/summarizer/coder.
- B9. The workflow runs one build loop per FEAT (coder, then reviewer, verdict file `docs/review-build-FEAT-00N.json`), then one Phase 2 gate loop (`docs/review-phase2.json`), then a finalize step. Each FEAT builds, tests and commits on its own, and a per-FEAT review catches problems before the next FEAT builds on them.
- B10. Reviewers do not commit. The next coder (or the finalize step) commits the review files left by the previous reviewer, by name, as `docs: record <FEAT> build review round N`.

---

## Stage 1: safety-relevant rework (FEAT-005)

Depends on Phase 1 (APPROVED). Order inside: ceiling, egress, tier control plane, token core, handler budget and redaction, NITs and no-exposure, close-out.

- [x] 22. Hard diff ceiling 80/3000/3000 (config-only) and size measurement.
      Rows: DES-FR-21, DES-AC-39, CR4-2d, CR4-2g, CR4-2h, CR4-2o, CR3-U13, CHAT-31, CHAT-32.
      What: `common/config.py` `DiffPolicy` defaults `max_files=80`, `max_changed_lines=3000`, new `max_test_lines=3000`; new strict models `SizePolicy` (`autonomous_max_class` ∈ xs…xl, `preferred_classes`, `overrun_tolerance=0.5`, `on_overrun` ∈ flag|reapprove, `exclude_globs`, `test_globs`), `DiscoveryFilters`, `ProposalPolicy`, `DiscoveryPolicy` with the §9.14 defaults, added to `PolicyConfig` as `size`, `discovery_filters`, `proposal`, `discovery`; a `PolicyConfig` validator rejects an `autonomous_max_class` whose bounds exceed the ceiling. New `common/size.py`: `SizeClass`, `CLASS_BOUNDS` (xs 20/2, s 80/4, m 250/8, l 500/15, xl 1000/30, xxl above), `split_numstat(entries, test_globs, exclude_globs) -> SizeMeasure(files, code_added, code_deleted, test_added, test_deleted)` and `classify(code_lines, files)`. Remove `max_changed_lines` from `TargetDefaults`, `RepoEntry`, `ResolvedTarget` and `TargetsConfig.resolve` (config.py:181, :197, :249, :275); `RepoEntry` gains `size_class: SizeClass | Literal["inherit"] | None` (autonomous preference only). `policy.check_diff(entries, policy)` (decision B6) reports `too_many_files`, `too_many_code_lines`, `too_many_test_lines` (decision B5); update `packaging.Packager.package` (packaging.py:282) and any user text that maps `too_many_lines`. `config/policy.yaml` and `config/targets.yaml` updated with comments kept.
      Files: `src/ossbot/common/{config.py,size.py}`, `src/ossbot/gatekeeper/{policy.py,packaging.py}`, `config/{policy.yaml,targets.yaml}`.
      Tests: update `tests/unit/gatekeeper/test_policy.py` (80/81 files, 3000/3001 code lines, 3000/3001 test lines, test lines never count as code, lockfile/vendored lines excluded but files counted), `tests/unit/common/test_config.py` (new blocks, validator, `max_changed_lines` rejected in targets by `extra=forbid`), `tests/unit/gatekeeper/test_package_verify.py` (new codes); new `tests/unit/common/test_size.py` (class edges, hypothesis split invariants) and `tests/unit/gatekeeper/test_ceiling_not_writable.py` (enumerates every token purpose in the migrations comment, every registered command and every FastAPI route; none writes `policy.diff`).
      Verify: `DEV pytest -m unit tests/unit/gatekeeper tests/unit/common` green; lint gate clean.

- [x] 23. Research egress (github.io and gists readable for research and Hermes) and egress modes with default `open`.
      Rows: DES-FR-18, DES-AC-33, DES-AC-34, ADR-0005, CR2-1a, CR2-1b, CR2-1d, CR2-1e, CR2-2a (FEAT-005 part), CR2-2b, CR2-2c, CR2-6b, CR2-6c, CHAT-23, CHAT-24, P1-15, P1-18.
      What: `config/egress.yaml`: new category `github_user_content: [.github.io, gist.github.com, gist.githubusercontent.com]` added to `sandbox_profiles.research` and `host_callers.hermes`; `web_public` removed from those base lists and granted per mode (decision B3): `mode_default: open`, `modes: {allowlist: {}, research: {research: [web_public], hermes: [web_public]}, open: {research: [web_public], hermes: [web_public], agent-llm: [web_public], catalog: [web_public]}}`. `always_denied.domains` drops `.github.io` and `gist.github.com`, keeps `uploads.github.com` and `checkpoint-api.hashicorp.com`, adds `telegra.ph`, `api.telegra.ph`, `edit.telegra.ph`; `cidrs` adds `::/128`; one why-comment per entry. `EgressConfig` gains `mode_default` and `modes` (Literal grantee keys). `ctl/render.py`: `squid_context(..., mode)` / `render_squid(..., mode)` and a `check_mode_invariants(snapshot)` that renders all three modes and raises `RenderError` if denied_dst, IP-literal patterns, DNS lines, fetch/validate lists or runner/gatekeeper lists differ, if fetch/validate ever get `web_public`, or if `agent-llm` and `catalog` differ. Control plane: `common/control_flags.py` `read_egress_mode(control_dir, default) -> EgressMode` (missing → snapshot default, unparseable or oversize → `allowlist`); `ControlService.set_egress_mode(mode, actor)` writes `/var/lib/ossbot/control/egress-mode` (`{mode, actor, ts}`) with `write_flag`, audits `egress_mode_changed`, FYIs; `POST /v1/control/egress-mode` (operator role; human via Telegram comes in item 47); `ossctl egress mode <m>` (TTY confirm) and `ossctl egress status`. Root apply: `scripts/bin/ossbot-egress-apply` (self-contained bash, pattern of `ossbot-resume`: read the flag, `python -I -m ossbot.ctl.render squid --mode <m>`, atomic write, `systemctl restart ossbot-proxy.service`, decision B4), `systemd/ossbot-egress.path` (PathChanged on the flag) + `ossbot-egress-apply.service`, added to `OSSBOT_ENABLE_UNITS` in `scripts/lib/services.sh` and installed by the `systemd` step. `ossbot-firewall --check` and `watchdog/tick.py` compare `/etc/ossbot/proxy/squid.conf` with the render for the current mode (drift → FAIL / anomaly). Doctor and `/status` show the mode. VERIFICATION.md threat-table rows 7/36 reworded (P1-18). ADR-0005 implementation addendum.
      Files: `config/egress.yaml`, `src/ossbot/common/{config.py,control_flags.py}`, `src/ossbot/ctl/{render.py,cli.py,doctor.py}`, `src/ossbot/gatekeeper/{control.py,api/control.py}`, `src/ossbot/gatekeeper/telegram/commands/builtin.py` (`/status` line), `src/ossbot/watchdog/tick.py`, `scripts/bin/{ossbot-egress-apply,ossbot-firewall}`, `systemd/ossbot-egress.{path,service}`, `scripts/lib/services.sh`, `tests/system/lib.sh`, `docs/VERIFICATION.md`, `docs/adr/0005-sandbox-isolation-and-egress.md`.
      Tests: `tests/unit/ctl/test_render.py` per mode with goldens `tests/unit/ctl/golden/squid.{allowlist,research,open}.conf` (replacing `squid.conf`; update deliberately), invariant-violation cases (a grant to `fetch` rejected by pydantic; a hand-built context giving fetch `web_public` rejected by the renderer); `tests/unit/common/test_control_flags.py` (new: egress-mode fail-closed cases); `tests/unit/gatekeeper/test_control.py` (set_egress_mode audit + FYI, role checks); `tests/unit/watchdog/test_tick_lifecycle.py` (drift); `tests/sandbox/test_profiles.py` github.io/gist case (research 200, fetch/validate 403 in every mode, agent-llm 403 outside `open`; local fixture host when offline); `tests/system/test_egress_surface.sh` (hermes 3141 CONNECT to `x.github.io.`/`gist.github.com.` allowed, code-running profiles 403, `telegra.ph` 403 for every caller); `tests/unit/test_systemd_units.py` covers the new unit.
      Verify: `DEV pytest -m unit tests/unit/ctl tests/unit/common tests/unit/gatekeeper tests/unit/watchdog` green; `REDEPLOY`; `ROOTDEV pytest -m sandbox` green; `ROOT bash tests/system/test_egress_surface.sh` green; `ROOT /opt/ossbot/bin/ossbot-firewall --check` exits 0 in `open`, again after `ROOT /opt/ossbot/app/.venv/bin/ossctl egress mode allowlist` (TTY confirm) and after switching back to `open`.

- [x] 23a. Hermes capability-tier control plane (allowlist vs `self_improve`), no Hermes needed.
      Rows (FEAT-005 part; status stays `missing` until item 36, the evidence column records this part, and the phase column becomes `P1-rework FEAT-005 (control plane) + P2 FEAT-007…`): CR2-3a, CR2-3b, CR2-3e, CR2-6a, ADR-0018.
      What: `PolicyConfig.hermes: HermesPolicy(capability_tier_default: Literal["locked","research","self_improve"] = "self_improve")` and `config/policy.yaml` `hermes.capability_tier_default: self_improve`. `common/control_flags.py` `read_hermes_tier(control_dir, default)` (missing → default; unparseable, oversize or unknown → `locked`). New `src/ossbot/gatekeeper/chat/__init__.py` and `chat/hermes_surface.py` with `TIER_TOOLSETS` (locked: `memory`, `todo`, `session_search`, `ossbot`; research: + `web` (only when item 33 confirms proxy routing, else equal to locked, a module constant set there); self_improve: + `skills`), `ALWAYS_DISABLED = (terminal, code_execution, file, browser, delegation, cronjob)` and `allowed_toolsets(tier)`. `ControlService.set_hermes_tier(tier, actor)` writes `/var/lib/ossbot/control/hermes-tier`, audits `hermes_tier_changed`, FYIs, and in the same transaction calls `ctx.decisions.skills.invalidate_all_open("tier_changed")` once item 24 lands (implement 23a after 24 if simpler; both are in FEAT-005). `POST /v1/control/hermes-tier` (operator), `ossctl hermes tier <tier>` (TTY confirm) and `ossctl hermes tier` (show). Doctor shows the tier.
      Files: `src/ossbot/common/{config.py,control_flags.py}`, `src/ossbot/gatekeeper/{control.py,api/control.py,chat/__init__.py,chat/hermes_surface.py}`, `src/ossbot/ctl/{cli.py,doctor.py}`, `config/policy.yaml`, `pyproject.toml` (import-linter: `ossbot.gatekeeper.chat` may not import `ossbot.gatekeeper.submit`/`github_writer`).
      Tests: new `tests/unit/gatekeeper/test_hermes_surface.py` (every tier keeps `ALWAYS_DISABLED` off, skills only in self_improve, research = locked when web routing is unconfirmed), `tests/unit/common/test_control_flags.py` (tier fail-closed), `tests/unit/gatekeeper/test_control.py` (tier change audited, FYI, skill approvals invalidated with `tier_changed`).
      Verify: `DEV pytest -m unit` green; lint gate clean.

- [x] 24. Token and approval core deltas 1–9 and the three decision services.
      Rows: TGRW-1, TGRW-2, TGRW-3, TGRW-4, TGRW-6, TGRW-7, CR3-S1, CR3-AC-29, CR3-AC-31, CR3-AC-46, CR3-AC-47, CR3-AC-57, CR3-AC-58, CR3-BA-1, CR3-BA-3, CR3-BA-4, CR3-BA-5, CR3-BA-17, CR3-M1 (deltas part), CR4-1f (service part), ADR-0003, ADR-0004, ADR-0008.
      What (DESIGN §14.18.4 is normative; the code today: `approvals.py` has a single `CARD_PURPOSES` tuple, `reissue(cid, *, tg_chat_id, actor, body_parts_total=1)`, no `disarm_expired`; `outbox.mark_sent(row)` takes no result; `CardPublisher` calls `ctx.approvals` directly): `approvals.py` `DECISION_PURPOSES`, `card_purposes(kind, has_interdiff)` minting view purposes multi-use (delta 3); `disarm_expired()` (delta 2); `create(..., expires_at=None)` with `bad_expiry`; `reissue(cid, *, tg_chat_id, actor, part_count, keep_expiry=False)` with `reissue_eligible()`, the confirmed-sibling refusal (`submitting`) and `nothing_to_resend` (deltas 5, 7); `Invalidated(approval_id, action_id: str | None, cid: str | None)`; `REASON_LABELS` additions. New `gatekeeper/decisions.py` (`DecisionService` protocol, `DecisionServices.all()/for_id()`), `gatekeeper/proposal_approvals.py` and `gatekeeper/skill_approvals.py` (ids from `common/ids.py` `new_proposal_approval_id()`/`new_skill_approval_id()`; two-tap, single-use, message-bound, binding recomputed at Approve, Confirm and before the effect; proposal and skill content fields are filled by items 29 and 36). Migration `migrations/0002_decisions.sql` per §14.20.2 (`proposal_approvals`, `skill_approvals`, `tg_views`, `tg_thread_seq`, `tg_outbox.result_message_id`, `bind_tokens_json`, `view_json`). `context.py`: `decisions`, `reissue`, `reissue_proposal`, `reissue_skill`, `card_recover` (no-op default); `reissue_cards` skips `action_id=None`. `app.housekeeping_once`: `expire_due` + `disarm_expired` on every service, then `ctx.card_recover()`. Fan-out (delta 9) at the four kill sites in `control.py` and both branches of `config_snapshot.py`; `set_dry_run` unchanged. `outbox.mark_sent(row, result)` binds tokens, writes `tg_views`, bumps `tg_thread_seq` and counts parts by id prefix in one transaction (delta 6). `telegram/bot.py` `CardPublisher` resolves through `ctx.decisions.for_id`, `_card_sent`/`_maybe_arm` single-transaction, `recover()` (steps 0–2) run in `TelegramFrontend._connect` and through `ctx.card_recover`. `state_machine.sweep_expired` uses the newest approval's window start and cancels `pending_approval` actions.
      Files: `src/ossbot/gatekeeper/{approvals.py,decisions.py,proposal_approvals.py,skill_approvals.py,context.py,app.py,control.py,config_snapshot.py,outbox.py,state_machine.py,migrations/0002_decisions.sql}`, `src/ossbot/gatekeeper/telegram/{bot.py,keyboards.py,handlers.py}` (call-site updates only), `src/ossbot/common/ids.py`.
      Tests: update `tests/unit/gatekeeper/{test_approvals.py,test_outbox.py,test_state_machine.py,test_control.py,test_snapshot.py}`, `tests/telegram/test_handlers.py`; new `tests/unit/gatekeeper/{test_card_purposes.py,test_disarm_expired.py,test_reissue_keeps_expiry.py,test_reissue_part_counts.py,test_reissue_eligibility.py,test_decision_fanout.py,test_card_recover.py,test_outbox_bind_atomic.py,test_dedupe_keys.py,test_proposal_approvals.py,test_skill_approvals.py,test_reissue_wrappers_ast.py}` (BA-1: AST test that only the `AppContext` wrappers call the services' `reissue`, BA-17).
      Verify: `DEV pytest -m "unit or telegram"` green, every existing Phase 1 approval test still passes; lint gate clean.

- [x] 25. Non-blocking update handlers (`/kill` handled within 2 s) and the redaction choke point.
      Rows: TGRW-5, TGRW-8, CR3-U14, CR3-AC-15, CR3-AC-59, CR3-AC-66, CR3-AC-67, CR3-BA-2, CR3-BA-15, CR3-BA-16, CR3-BA-18, CR3-BA-21, P1-4.
      What (DESIGN §14.18.5 normative; today `_confirm` awaits `self.preflight` inline in the single PTB update slot): `telegram/handlers.py` `Handlers.spawn(coro, kind, *, session_key=None, on_done=None)`, the kind index, the session map and one synchronous idempotent done callback; `cancel(kinds)`; `KILL_CANCELS`; `PREFLIGHT_TIMEOUT_S = 8.0`; `HANDLER_SLOW_S = 1.0` with a `handler_slow` WARNING; the Confirm preflight and `_run_submit` and every I/O-bound command or confirm effect go through `spawn`; `/kill` and `/pause` run their local effect inline first, then cancel `KILL_CANCELS`. `commands/registry.py` `@command(..., effect="local"|"io")`; `io` commands are spawned. New `gatekeeper/secret_scan.py` (`SecretScanner` built once per snapshot from gatekeeper-held secret values and patterns, held on `AppContext`); `policy.check_text(..., *, secret_values)` with every production caller passing it; `packaging` scans the patch with it. New `telegram/ui/__init__.py`, `telegram/ui/redact.py` (`redact`, `scan`, `draft_visible`). P1-4: `ApprovalService.set_text_override` runs `policy.check_text` (title single line 1–256, body ≤ 60 000, disclosure, confidentiality floor, secrets) and raises before storing anything. Minimal `tests/fakes/fake_hermes.py` stream for the budget tests.
      Files: `src/ossbot/gatekeeper/telegram/{handlers.py,commands/registry.py,commands/builtin.py,ui/__init__.py,ui/redact.py}`, `src/ossbot/gatekeeper/{secret_scan.py,policy.py,packaging.py,approvals.py,context.py}`, `tests/fakes/fake_hermes.py`.
      Tests: new `tests/telegram/test_handlers_nonblocking.py` (typed `/kill` handled ≤ 2 s while a 60 s fake stream and a slow preflight are held open; no `handler_slow`; cancel-before-start in one `getUpdates` batch runs the done callback), `tests/unit/gatekeeper/{test_secret_scan.py,test_redaction_paths.py,test_check_text_callers_ast.py}`, update `tests/unit/gatekeeper/{test_approvals.py,test_policy.py}` (edited title with a newline, a floor value or a secret refused, nothing stored).
      Verify: `DEV pytest -m "unit or telegram"` green; lint gate clean.

- [x] 26. Round-6 NITs P1-16, P1-17, P1-19 and the no-exposure guard (P1-15 and P1-18 land in item 23).
      Rows: DES-FR-22, DES-AC-42, CHAT-33, CR3-CF-1, CR3-S4, P1-16, P1-17, P1-19, ADR-0013.
      What: P1-16 `scripts/hyperv-firewall.ps1` requires exactly the two ossbot rules (Outbound, Allow, Enabled, TCP, 443/80) and re-creates drifted ones; `scripts/ossctl.ps1 doctor` validates their properties and flags any other Allow rule for the WSL VMCreatorId (PowerShell 5.1, CRLF). P1-17 `tests/unit/test_systemd_units.py` asserts `ProtectHome` ∈ {yes, tmpfs} for every non-root unit; `test_egress_surface.sh` and doctor assert `Linger=no` and no `/run/user/<uid>` for the five service uids. P1-19 `test_egress_surface.sh` arms `systemd-run --on-active=90` that restores `kernel.io_uring_disabled=2` and starts `ossbot-watchdog.timer`, cancelled after the restore. No exposure: new `tests/system/test_no_exposure.sh` (parse `ss -ltnup`; only loopback listeners plus documented bridge addresses with no published ports) and `tests/unit/test_no_tunnels.py` (no cloudflared, ngrok, tailscale funnel/serve, `setWebhook`, webhook URL or tunnel config in tracked files outside `docs/`).
      Files: `scripts/{hyperv-firewall.ps1,ossctl.ps1}`, `src/ossbot/ctl/doctor.py`, `tests/unit/{test_systemd_units.py,test_no_tunnels.py}`, `tests/system/{test_egress_surface.sh,test_no_exposure.sh,lib.sh}`, `docs/VERIFICATION.md` (threat-table row 41).
      Verify: `DEV pytest -m unit tests/unit/test_systemd_units.py tests/unit/test_no_tunnels.py` green; `REDEPLOY`; `ROOT bash tests/system/test_no_exposure.sh` and `ROOT bash tests/system/test_egress_surface.sh` green; `powershell -NoProfile -File scripts\ossctl.ps1 doctor` PASS for Hyper-V (WARN with the printed follow-up if the session is not elevated; never trigger UAC).

- [x] 27. Close-out FEAT-005 and Phase 1 re-verification against revision 5.
      Rows: CHAT-37 (FEAT-005 part), TGRW-15 (FEAT-005 part).
      What: `REDEPLOY` twice (second run only `unchanged`); run the full Phase 1 verification block of PLAN.md plus the new tests; set the closed rows to `done` with evidence and record the FEAT-005 part on shared rows (no `rework` left that names FEAT-005); point the code comments that cite pre-consolidation FEAT numbers (`common/control_flags.py`, `telegram/cards.py`, `telegram/keyboards.py`, `gatekeeper/approvals.py`) at current items; add "Phase 1 rework (FEAT-005)" to `docs/VERIFICATION.md`; set FEAT-005 `status: completed` with findings; commit.
      Verify: `DEV pytest -m "unit or telegram or acp or e2e"`; `ROOTDEV pytest -m sandbox`; every `ROOT bash tests/system/test_*.sh`; `powershell -NoProfile -File tests\system\test_kill.ps1`; `ROOT /opt/ossbot/app/.venv/bin/ossctl doctor`; the matrix check for FEAT-005 lists no `rework` row and no row whose phase names only FEAT-005; `git status --porcelain` empty.

---

## Stage 2: backend (FEAT-006, FEAT-007, FEAT-008)

Everything user-facing is reachable here through the gatekeeper API, `ossctl` and plain FYI messages; the Telegram views come in Stage 3.

### FEAT-006: pipeline with scout and gate 1, size budget, user-directed work, global model switch

Depends on FEAT-005. Independent of FEAT-007 (the runner reaches Kiro only through sandboxd).

- [ ] 28. Global model switch completion.
      Rows: DES-FR-7, DES-AC-13 (FEAT-006 part), ADR-0009, CHAT-12 (backend part), CR2-7f (role part), DES-V-9.
      What: verify §23 item 9 first (catalog JSON shape, id spelling). New `common/models.py`: `resolve(role) -> ResolvedModel` (indirection through `default` and role names, catalog-or-forced rule, `fallbacks`, `model_unavailable`, mtime cache, last-good copy). Roles `scout` (inherits `planner`) and `vision` added to `RoleName` and `config/models.yaml`. `ModelsAdmin.set_model` (exists) checked for ruamel round-trip, atomic replace keeping mode/group, audit and diff; `PUT /v1/models/{role}` (exists) accepts `force`. `ossctl model list|set <role> <id> [--effort] [--force] [--commit]`, `ossctl models refresh` (catalog job via sandboxd `list_models`). New-model detection: `model_catalog.notified` column (migration `0003_cr004.sql` shares the number with item 29; one file), one FYI per new id, one summary on the first fill.
      Files: `config/models.yaml`, `src/ossbot/common/{models.py,config.py}`, `src/ossbot/gatekeeper/{models_admin.py,api/models.py}`, `src/ossbot/ctl/cli.py`.
      Tests: new `tests/unit/common/test_models_resolve.py`, `tests/unit/gatekeeper/test_models_admin.py`; update `tests/unit/gatekeeper/test_catalog_upsert.py`.
      Verify: `DEV pytest -m unit` green (comments preserved, atomic write, unknown role and non-catalog id refused without `--force`, `scout` inherits `planner`, one FYI per new id).

- [ ] 29. Gatekeeper side of CR-004: states, proposal API and gate 1, claim after gate 1, size report, `/v1/work`, discovery preferences, outcomes.
      Rows: DES-FR-20, DES-FR-21, DES-AC-37, DES-AC-38, DES-AC-39, DES-AC-40, DES-AC-41, ADR-0003, CR3-U13, CR4-1e, CR4-1f, CR4-1g, CR4-1h, CR4-2b, CR4-2c, CR4-2f, CR4-2i, CR4-2l, CR4-2n, CR4-3c (backend part), CR4-5a, CR4-5b, CR4-5c, CHAT-27, CHAT-29, CHAT-30.
      What: new states and edges of §9.2 (`SCOUTING`, `PROPOSED`, `PROPOSAL_APPROVED`, `RESCOPE_REQUIRED`, decline states, the two human-only Undo edges) in `common/schemas.py` and `state_machine._LISTED`, with the worker-forbidden set extended; `common/rediscover.py` rules for Never/Skip/Later. `POST /v1/contributions/{cid}/proposal` (worker): validates the `Proposal` model (`extra=forbid`), canonicalizes, stores version and `proposal_sha256`, creates the `p_` approval and queues its card through the outbox. `ProposalApprovals` effects: `start` (enqueue the plan stage; refused while paused, killed, draining or workers-down), `adjust`, `smaller`, `skip`, `never_repo|never_kind` (after confirm), `snooze`; a new version invalidates with `new_proposal_version`. The claim guard moves to `PROPOSAL_APPROVED → FETCHING` honouring `proposal.claim_comment` (claims are still outbound actions behind gate 2; dry-run applies). Size accounting in `Packager.package`: `common/size.split_numstat` on the gatekeeper numstat, compare with the approved estimate and class, write `size_report` on the contribution, `RESCOPE_REQUIRED` only for `on_overrun: reapprove`, ceiling hit pauses the item for `👤` and autonomous work. `POST /v1/work` (operator; GitHub issue/PR/repo URL only, `source='user'`, notes) in new `api/work.py`, `ossctl work <url> [--notes]`. New `gatekeeper/discovery_prefs.py` (only writer of size/filter/per-repo preferences; refuses ceiling keys; audit, FYI, 60 s undo token; never touches approved proposals) with `GET/PUT /v1/discovery-prefs` in `api/prefs.py`, `ossctl size set|show`, `ossctl filters`. `outcomes` table and writer. Migration `0003_cr004.sql`.
      Files: `src/ossbot/common/{schemas.py,rediscover.py}`, `src/ossbot/gatekeeper/{state_machine.py,proposal_approvals.py,discovery_prefs.py,packaging.py,derive.py,contributions.py,app.py,api/contributions.py,api/work.py,api/prefs.py,migrations/0003_cr004.sql}`, `src/ossbot/ctl/cli.py`, `pyproject.toml` (contract: only `discovery_prefs` writes the prefs tables, via an AST test if import-linter cannot express it).
      Tests: update `tests/unit/gatekeeper/test_state_machine.py` (every new edge and actor), `tests/unit/common/test_rediscover.py`, `tests/unit/gatekeeper/{test_claim_live.py,test_package_verify.py}`; new `tests/unit/gatekeeper/{test_proposals.py,test_size_report.py,test_discovery_prefs.py,test_work_intake.py,test_outcomes.py}`.
      Verify: `DEV pytest -m unit` green (token bound to proposal hash and version; overrun flagged, RESCOPE only in `reapprove`; ceiling pauses `👤` work; prefs never change an approved proposal; `/v1/work` refuses non-GitHub URLs; claim happens only after gate 1).

- [ ] 30. Runner core, all stages including scout, ranking and filters, tests-required rule, prompts and agents.
      Rows: DES-FR-3, DES-FR-4, DES-FR-10 (agents, prompts), DES-FR-12, DES-FR-14 (CLI), DES-FR-17 (runner part), DES-FR-21, DES-I8, DES-AC-31 (runner part), DES-AC-41, DES-V-5, DES-V-5a, DES-V-5b, DES-V-6, DES-V-8, DES-V-21, DES-V-24, DES-V-33, CR1-3a2, CR1-3c, CR1-4b, CR4-1a, CR4-1b, CR4-1c, CR4-1d, CR4-2a, CR4-2d, CR4-2e, CR4-2j, CR4-2m, CR4-3a, CR4-3b, CR4-4, CR4-5e, CHAT-13, CHAT-27, CHAT-28, CHAT-32.
      What: re-verify §23 items 5, 5b, 6, 8, 21, 24, 33 first (Kiro tool ids for `--trust-tools`, Tool Search key, server- vs client-side web tools) and record them. `src/ossbot/runner/{main,scheduler,pipeline,github_read,scoring,llm,output_parse,prompts,gk_client,sbx_client,squidlog}.py` and `runner/stages/{discover,vet,scout,fetch,plan,implement,validate,review,package,watch,catalog,importer}.py`: `stage_runs` + 15 s heartbeat, interrupted re-queue (max 3), loop bounds, first-start order M11, `require_running` + `require_up` before each stage and at each heartbeat, monotonic timeouts with sleep detection, the real `resumed` count for `ossctl up`. Discovery ranking and filters for autonomous items only (`scoring.py`: size fit preferring the smallest class, responsiveness, merge rate, labels, freshness, interests, history), `proposals_per_cycle`, `max_per_day`, `max_concurrent_scouts`. Scout: shallow read-only snapshot + `research` job, `Proposal` output, refuses above `autonomous_max_class` for autonomous items, offers a smaller alternative; `👤` items skip ranking, caps, size and filters. Tests-required rule: coder and reviewer prompts; reviewer rejection when tests are missing (unless docs/config-only or no suite, reason on the card). Kiro headless always `--v3` (already in `sandbox/entrypoints/kiro.sh`); output parse = last fenced json block → pydantic, one repair retry; `model_actual`, `model_mismatch`. Decision B8: `sandboxd/profiles.py` gains `scout` in `ResearchRole`, `KiroRole`, `ROLE_REPO_MODE` and `TRUST_TOOLS`. Prompts `prompts/{vetter,scout,planner,coder,reviewer,summarizer,chat_system,watcher_reply}.md` with the web-research rule and disclosure; `.kiro/agents/ossbot-{vetter,scout,planner,coder,reviewer,summarizer}.json` (coder gets `web_search`/`web_fetch` only when the egress mode is `open` and §23 item 6 confirms they work there; otherwise research stays in the planner). `ossctl discover`, `ossctl contribution import`, `ossctl targets verify`. Console script `ossbot-runner` in `pyproject.toml`.
      Files: `src/ossbot/runner/**`, `prompts/**`, `.kiro/agents/ossbot-*.json`, `src/ossbot/sandboxd/profiles.py`, `sandbox/images/llm/Dockerfile` (bake the new agent and prompt files), `src/ossbot/ctl/cli.py`, `pyproject.toml`, `config/versions.lock.yaml`.
      Tests: new `tests/unit/runner/**` (scoring prefers smallest class; filters skipped for `👤`; parse with one repair retry; missing-tests rejection; `require_up`/kill/pause gates per stage; re-queue counting; sleep detection), update `tests/unit/sandboxd/{test_argv.py,test_jobs.py,test_kiro_settings.py}` for the scout role, `tests/unit/gatekeeper/test_control.py` over every runner stage including scout; re-record `tests/unit/dockerfilter/corpus/recorded.json` with `ROOTDEV python -m tests.unit.dockerfilter.record_corpus` if sandboxd argv changes.
      Verify: `DEV pytest -m unit` green; lint gate clean (runner never imports gatekeeper: existing contract).

- [ ] 31. e2e with fakes and the runner service.
      Rows: DES-FR-11 (e2e part), DES-AC-4, DES-AC-5, DES-AC-8, DES-AC-28, DES-AC-40, CR4-5d, CR1-6c2 (runner part).
      What: `tests/fakes/upstream_fixture.py` (hostile extras), `tests/bin/kiro-cli` stub (canned stream-json per agent incl. injection and a scout proposal), `tests/e2e/{conftest,test_e2e_dry_run,test_e2e_proposal,test_e2e_live_fake,test_e2e_dryrun_toggle,test_e2e_injection,test_e2e_import,test_e2e_first_start,test_e2e_fix_round_failure,test_e2e_resume,test_e2e_user_directed}.py` on FakeRuntime (`test_e2e_proposal`: discover → scout → proposal card → Start work → … → preview → approve DRY RUN; `test_e2e_live_fake`: live mode only against `fake_github`; `test_e2e_resume`: kill the runner mid-stage, restart, re-queue, no duplicate write) and `tests/e2e/test_e2e_docker.py` (marker `e2e_docker`, reuses `tests/sandbox/conftest.py`). `systemd/ossbot-runner.service` with the full non-root hardening pattern (context.json `contribution_requirements`), added to `OSSBOT_ENABLE_UNITS`, the `ossbot-kill` stop list and `tests/system/lib.sh`; `test_crash_safety.sh` and `test_firewall_failclosed.sh` gain the runner case.
      Files: `tests/e2e/**`, `tests/fakes/upstream_fixture.py`, `tests/bin/kiro-cli`, `systemd/ossbot-runner.service`, `scripts/lib/services.sh`, `scripts/bin/ossbot-kill`, `tests/system/{lib.sh,test_crash_safety.sh,test_firewall_failclosed.sh}`.
      Verify: `DEV pytest -m e2e` green; `DEV pytest -m unit tests/unit/test_systemd_units.py` green; `REDEPLOY`; `ROOTDEV pytest -m e2e_docker` green; `ROOT systemctl is-active ossbot-runner` = `active`; `ROOT bash tests/system/test_crash_safety.sh` and `ROOT bash tests/system/test_firewall_failclosed.sh` green.

- [ ] 32. Redeploy, live read-only checks, close-out FEAT-006.
      Rows: DES-FR-9b, DES-V-17, DES-V-18, NOTE-3 (sandbox part), NOTE-7.
      What: `REDEPLOY` (image rebuild ~10 min; commit `config/versions.lock.yaml` digests). Live, read-only, product code only: `ossctl models refresh` with the sandbox key (record id spelling; fix `models.yaml` through `ossctl model set`, proving the switch), `ossctl doctor --probes`, `ossctl kiro smoke` (headless `--v3` round-trip), `ossctl targets verify` (read token), catalog summary FYI to the owner only. Discovery stays read-only; dry-run ON; no claim comment. Record each check (date, command, outcome, no secret values) under "## Phase 2 live checks" in `docs/VERIFICATION.md`; update the matrix; FEAT-006 `completed` with findings; commit.
      Verify: FEAT-006 verification list in its FEAT file; matrix check for FEAT-006; `git status --porcelain` empty.

### FEAT-007: Kiro V3 ACP provider fork (V3 primary, V2 fallback), Hermes, tiers, multimodal assistant backend

Depends on FEAT-005 (egress, tier control plane, decision services, redactor) and FEAT-006 (`common/models.py`, `vision` role, research jobs).

- [ ] 33. Research first, recorded before coding.
      Rows: DES-V-5c, DES-V-10, DES-V-11, DES-V-12, DES-V-13, DES-V-28, DES-V-29, DES-V-30, CR2-3c.
      What: re-fetch the Kiro manifest (bump the pin if 3.x is out, then re-check `--v3` and `--agent-engine=v3`), the V3 ACP migration, ACP and auth pages; Hermes v0.22.2 at the pinned tag: skills loading and write paths, native web tools and `HTTPS_PROXY`, `/v1/responses` input parts and streaming, state layout, `setup-hermes.sh` non-dependency steps. Record in the ADR-0002 addendum and `config/versions.lock.yaml` (Hermes peeled commit). Set the `research` tier constant from item 23a accordingly.
      Verify: ADR-0002 addendum committed with sources and dates; `DEV pytest -m unit tests/unit/gatekeeper/test_hermes_surface.py` still green.

- [ ] 34. Vendored `hermes-kiro-provider` fork with a V3 adapter (primary) and the untouched V2 path (fallback).
      Rows: DES-FR-11 (acp part), DES-AC-20, ADR-0002, CHAT-5, CHAT-6.
      What: import `tobenwarrior/hermes-kiro-provider@02c4dd06585436751145961658a22b773f23a0ef` unmodified as its own commit at `vendor/hermes-kiro-provider/` (with `LICENSE`, `UPSTREAM_COMMIT`, `UPSTREAM_URL`); then add `kiro_acp/v3_core.py` (stdlib-only: `launch_spec`, `session_new_params`, `assert_surface`, permission auto-reject, NDJSON transport, `_kiro/auth/getAccessToken` with the result redacted from logs), `kiro_acp/v3.py` (Hermes glue, live model switch via `session/set_config_option`, `session/load` on drop, image content blocks when `promptCapabilities.image`), `kiro_acp/hermes_plugin.py` and `HERMES_KIRO_ACP_ENGINE=v3|v2` selection; import-free `kiro_acp/__init__.py`; non-editable uv path dependency (`uv lock`). `app deploy` already rsyncs `vendor/`. Prepare the upstream bundle for `ossctl contribution import` (the first contribution; still needs both gates; no live write).
      Files: `vendor/hermes-kiro-provider/**`, `pyproject.toml`, `uv.lock`, `tests/fakes/fake_acp_v3.py`, `tests/acp/**`.
      Tests: new `tests/acp/{test_v3_contract,test_mode_mismatch,test_hermes_probe_eval,test_model_switch,test_image_blocks,test_permissions_reject,test_auth_token_redacted,test_engine_select_v2}.py` (marker `acp`).
      Verify: `DEV pytest -m acp` green; `DEV pytest -m unit` green; lint gate clean (mypy config covers the fork's new files; the original V2 files are excluded from ruff/mypy as vendored).

- [ ] 35. Hermes install and wiring.
      Rows: DES-FR-1b.
      What: provision steps `kiro-cli-hermes` and `hermes` replace the named no-ops in `scripts/provision.sh` (new `scripts/lib/hermes.sh`; §15.4 script with staging, tag-object and commit checks, `refuse_link`, rollback, R5-N2 upgrade lock); `hermes/config/{config.yaml.j2,permissions.yaml}` rendered per tier by root with `O_NOFOLLOW`; `hermes/plugins/ossbot/` (status, queue, contribution, logs, request_discovery, notify, pause, kill, `request_sandbox_job`, feature registry); `.kiro/agents/ossbot-chat.json` (only `web_search`, `web_fetch`); units `ossbot-hermes.service` (full non-root hardening, `ProtectHome=tmpfs`) and `ossbot-hermes-probe.socket` + `ossbot-hermes-probe@.service`; `src/ossbot/ctl/hermes_probe.py` (`-I`, imports `kiro_acp` only from `/opt/ossbot/app/vendor/...`, adapter drift hash, pass rules of §15.2); `ossctl hermes upgrade|login|probe`, `ossctl kiro upgrade`; doctor Hermes checks replace the SKIP placeholders; gatekeeper `/v1/toolsets` surface check uses `allowed_toolsets(tier)`.
      Files: `scripts/{provision.sh,lib/hermes.sh,lib/kiro.sh,lib/services.sh,bin/ossbot-kill}`, `hermes/**`, `.kiro/agents/ossbot-chat.json`, `systemd/ossbot-hermes*.{service,socket}`, `src/ossbot/ctl/{hermes_probe.py,cli.py,doctor.py}`, `src/ossbot/gatekeeper/chat/hermes_surface.py`, `tests/system/lib.sh`.
      Tests: new `tests/unit/ctl/test_hermes_probe.py`, `tests/unit/ctl/test_hermes_render.py`, `tests/system/test_hermes_install_hygiene.sh` (planted symlinks refused, commit mismatch aborts with nothing replaced, failed sync rolls back); `tests/unit/test_systemd_units.py` covers the new units.
      Verify: `DEV pytest -m "unit or acp"` green; `REDEPLOY` twice; `ROOT runuser -u hermes -- /home/hermes/.local/bin/kiro-cli --version` and `ROOT runuser -u hermes -- /home/hermes/.local/bin/hermes --version` print the pinned versions; `ROOT bash tests/system/test_hermes_install_hygiene.sh` green; `ROOT /opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.hermes_probe --handshake-only` run and recorded in the ADR-0002 addendum.

- [ ] 36. Capability tiers applied and the root-enforced skill gate.
      Rows: DES-FR-19, DES-AC-25, DES-AC-35 (backend part), ADR-0018, CR2-3a, CR2-3b, CR2-3d (backend part), CR2-3e, CR2-3f, CR2-3g, CR2-6a, CHAT-25 (backend part).
      What: root `systemd/ossbot-hermes-tier.{path,service}` + `scripts/bin/ossbot-hermes-tier` (re-render `~hermes/.hermes/config.yaml` for the tier with `O_NOFOLLOW`, restart Hermes when not killed/paused, re-run the probe and surface check; relay reopens only when both pass); skill staging dir `~hermes/.hermes/skills-staging/`, root hasher (probe socket + timer) writing `skill_reviews` (migration `0004_hermes.sql`), `SkillApprovals` content binding (file hashes, declared tools, tier), root activation copying exactly the bound bytes into the read-only active dir (`root:hermes` 0750/0640), doctor drift check. If item 33 shows v0.22.2 cannot keep staging and active skills apart, stop and ask (`send_message` warning) instead of shipping `self_improve` ungated.
      Files: `systemd/ossbot-hermes-tier.{path,service}`, `scripts/bin/ossbot-hermes-tier`, `src/ossbot/gatekeeper/{skill_approvals.py,control.py,chat/hermes_surface.py,migrations/0004_hermes.sql}`, `src/ossbot/ctl/{doctor.py,skills_root.py}`, `scripts/lib/services.sh`.
      Tests: new `tests/unit/gatekeeper/test_skill_gate.py` (binding to bytes, tier above refused, Confirm refused while killed, `tier_changed` invalidation), `tests/unit/ctl/test_skills_root.py` (symlink/special-file refusal, exact-bytes copy), `tests/system/test_hermes_tier.sh` (switch locked → self_improve → locked: config re-rendered, probe re-run, toolsets match).
      Verify: `DEV pytest -m unit` green; `REDEPLOY`; `ROOT bash tests/system/test_hermes_tier.sh` green.

- [ ] 37. Multimodal assistant backend.
      Rows: DES-FR-13 (backend part), DES-AC-13 (chat part), DES-AC-36 (backend part), CR2-7a (backend part), CR2-7c, CR2-7d, CR2-7e, CR2-7f, CR2-7g, CR2-7i, CR2-7j, CR2-7l (backend part), CR3-AC-36, CR3-IH-4 (backend part), CR3-IH-9 (backend part), CR3-IH-10 (backend part), CR3-IH-11, CHAT-26 (backend part).
      What: `gatekeeper/chat/{backend,hermes_api,turns,media,imgmeta}.py`: `ChatBackend`, `HermesApiBackend` (`/v1/responses`, session keys, streaming deltas when available, cancel), relay gate (surface check + probe file younger than 2 h), `chat_turns`/`chat_sessions` (migration `0004_hermes.sql`); attachments via `getFile` through 3150 (cap `chat.attachments.max_mb` 20, allowlist images/PDF/text/audio, `/srv/ossbot/chat/<session>/` group `hermes` 0750/0640); `imgmeta.py` pure-Python metadata walker (JPEG APP1/EXIF/XMP, PNG `eXIf`/`tEXt`/`iTXt`/`zTXt`, WebP `EXIF`/`XMP `; HEIC refused with "send it as a photo"; pixel bytes identical; no Pillow, ADR-0020); image turns use the `vision` role; PDF text via a new sandboxd entrypoint `extract_text` (`--network none`, `pdftotext` from `sandbox-full`, ≤ 2 MiB, added to `JobEntrypoint`/`PROFILE_ENTRYPOINTS`); voice via Kiro audio input only if §23 item 30 confirms it, else the "Voice isn't supported yet" reply; per-feature SQLite store pattern in the plugin; optional reverse-image-search key hook in `scripts/setup-secrets.*` plus a skill stub (no provider); `POST /v1/chat/selftest` (operator) and `ossctl chat selftest`. `PolicyConfig.chat.attachments` in `config/policy.yaml`. Rendering, routing and streaming UI are Stage 3.
      Files: `src/ossbot/gatekeeper/chat/**`, `src/ossbot/gatekeeper/{api/chat.py,migrations/0004_hermes.sql}`, `src/ossbot/sandboxd/profiles.py`, `sandbox/entrypoints/extract_text.sh`, `sandbox/images/full/Dockerfile` (poppler-utils pinned), `scripts/setup-secrets.{sh,ps1}`, `hermes/plugins/ossbot/**`, `config/policy.yaml`, `src/ossbot/ctl/cli.py`.
      Tests: new `tests/unit/gatekeeper/chat/{test_backend.py,test_relay_gate.py,test_media.py,test_imgmeta.py,test_imgmeta_fuzz.py,test_turns.py}`, `tests/unit/sandboxd/test_extract_text.py`, `tests/sandbox/test_extract_text.py` (root), `tests/fakes/fake_hermes.py` extended.
      Verify: `DEV pytest -m "unit or acp"` green; `REDEPLOY`; `ROOTDEV pytest -m sandbox` green.

- [ ] 38. Redeploy, live checks, close-out FEAT-007.
      Rows: NOTE-3 (hermes part), NOTE-4, CR2-4, CR2-7l, CR3-IH-4.
      What: real V3 handshake recorded in ADR-0002; Hermes-key headless `--v3` round-trip (`ossctl kiro smoke --key hermes`); `ossctl hermes probe` passes; a real V3 ACP session through the fork via `ossctl chat selftest` (answer to the owner only); live chat model switch without restart; a current-info question returns a sourced answer and the proxy log shows (or the ADR documents the absence of) Kiro web calls via 3141; a `*.github.io` page and a gist fetched by research and by Hermes; a food photo returns an estimate. Key or account problems go under "User action needed" in `docs/FINAL_REPORT.md` (create it), never worked around. Matrix, VERIFICATION, FEAT-007 `completed`, commit.
      Verify: FEAT-007 verification list; matrix check for FEAT-007; `git status --porcelain` empty.

### FEAT-008: lifecycle CR-001 remainder (6a shutdown blocker, 6b event task), crash safety, backup and restore

Depends on FEAT-006 and FEAT-007 (runner and Hermes units, Hermes state layout).

- [ ] 39. Windows lifecycle script audit.
      Rows: DES-FR-17 (Windows part), CR1-5a.
      What: `scripts/status.ps1` shows every CR-001 field (lifecycle, kill, dry-run, stages, approvals, outbox, last poll + reconnects, last shutdown type, task states, egress mode, Hermes tier); `down.ps1`/`up.ps1` serialize on the named mutex `Local\ossbot-lifecycle` and log to `.ossbot-local\lifecycle.log`.
      Files: `scripts/{status.ps1,down.ps1,up.ps1,lib/common.ps1}`, `src/ossbot/gatekeeper/control.py` (`lifecycle_status` fields).
      Tests: update `tests/unit/gatekeeper/test_lifecycle.py`; new `tests/system/test_lifecycle_scripts.ps1` (two concurrent `down.ps1` serialize; status shows every field).
      Verify: `DEV pytest -m unit` green; `REDEPLOY`; `powershell -NoProfile -File tests\system\test_lifecycle_scripts.ps1` green; live `down.ps1 -Timeout 10` → `status.ps1` → `up.ps1` recorded in VERIFICATION.

- [ ] 40. 6a shutdown blocker.
      Rows: DES-AC-32, DES-V-31, CR1-6a.
      What: research and record `docs/adr/0021-windows-shutdown-hooks.md` (`WM_QUERYENDSESSION`, `ShutdownBlockReasonCreate/Destroy`, hidden window requirements, Fast Startup). `scripts/windows/ossbot-shutdown-blocker.ps1` (PowerShell 5.1, C# compiled at runtime from repo source, single instance, log in `.ossbot-local\`), started by the `ossbot-autostart` task, runs `down.ps1 -Kind blocker` before releasing.
      Files: `scripts/windows/ossbot-shutdown-blocker.ps1`, `scripts/windows/ShutdownBlocker.cs`, `scripts/bootstrap.ps1` (autostart action), `docs/adr/0021-windows-shutdown-hooks.md`.
      Tests: new non-destructive `tests/system/test_shutdown_blocker.ps1` (simulated `WM_QUERYENDSESSION` to its own window → `down` invoked, reason registered and destroyed). The real restart test is item 57.
      Verify: `powershell -NoProfile -File tests\system\test_shutdown_blocker.ps1` green.

- [ ] 41. 6b event task, bootstrap `shutdownhooks` phase, `-Uninstall`, `-Shortcuts`.
      Rows: DES-NFR-3, CR1-5d, CR1-6b, CR1-6d.
      What: Scheduled Task `ossbot-shutdown-hook` (System log, User32 event 1074 only, non-elevated, `down.ps1 -Timeout 20 -Kind hook`, PT2M limit), SpiralPool BSD-3 credit (and notice if code is adapted); `bootstrap.ps1` phase `shutdownhooks`, `-Uninstall` (removes only ossbot-* tasks, shortcuts and the documented host files), optional `-Shortcuts`; `ossctl.ps1 doctor` checks both tasks.
      Files: `scripts/{bootstrap.ps1,uninstall-host.ps1,ossctl.ps1}`, `scripts/windows/ossbot-shutdown-hook.xml` (or generated), `docs/adr/0021-windows-shutdown-hooks.md`.
      Tests: update `tests/system/test_bootstrap_zero.ps1`; new `tests/system/test_shutdown_hook_task.ps1` (`Start-ScheduledTask` runs `down` with kind `hook`; last-shutdown shows it).
      Verify: `powershell -NoProfile -File scripts\bootstrap.ps1` twice (second run only `unchanged`); `powershell -NoProfile -File tests\system\test_shutdown_hook_task.ps1` green; `scripts\ossctl.ps1 doctor` PASS for the tasks.

- [ ] 42. Telegram lifecycle root helper and full-stack crash safety.
      Rows: DES-AC-31, DES-AC-32, CR1-5c (root part), CR1-6c2.
      What: `scripts/bin/ossbot-lifecycle` + `systemd/ossbot-lifecycle.{path,service}` consume a gatekeeper-written request (`down` scope workers, `up`; regular file ≤ 4 KiB, ≤ 5 min old, always consumed, `up` refused while killed); `POST /v1/control/lifecycle-request` (operator now; human via Telegram in item 47). `test_crash_safety.sh` covers gatekeeper, sandboxd, dockerfilter, runner and Hermes; `test_lifecycle.sh` gains the workers-down case.
      Files: `scripts/bin/ossbot-lifecycle`, `systemd/ossbot-lifecycle.{path,service}`, `scripts/lib/services.sh`, `src/ossbot/gatekeeper/{control.py,api/control.py}`, `tests/system/{test_crash_safety.sh,test_lifecycle.sh,lib.sh}`.
      Tests: update `tests/unit/gatekeeper/test_lifecycle.py` (request file rules); the two system tests above.
      Verify: `DEV pytest -m unit` green; `REDEPLOY`; `ROOT bash tests/system/test_crash_safety.sh` and `ROOT bash tests/system/test_lifecycle.sh` green.

- [ ] 43. Backup and restore, close-out FEAT-008.
      Rows: DES-FR-15 (tooling part), DES-NFR-10 (tooling part), DES-AC-22, DES-V-26, ADR-0016, CHAT-18.
      What: `ctl/backup.py`, `ctl/restore.py`, `scripts/{backup,restore}.ps1`, `systemd/ossbot-backup.{service,timer}` (installed disabled), age pinned in `config/versions.lock.yaml`; include list = §18.8 + Hermes state, active skills, feature stores, `config/telegram.yaml`; chat attachments excluded by default; restore leaves killed + dry-run ON and invalidates all three decision services; output under gitignored `backups/`. Then matrix, VERIFICATION, FEAT-008 `completed`, commit.
      Files: `src/ossbot/ctl/{backup.py,restore.py,cli.py}`, `scripts/{backup.ps1,restore.ps1,lib/services.sh}`, `systemd/ossbot-backup.{service,timer}`, `config/versions.lock.yaml`, `.gitignore`.
      Tests: new `tests/unit/ctl/{test_backup.py,test_restore.py}`, `tests/system/test_backup_roundtrip.sh`.
      Verify: FEAT-008 verification list (incl. `ROOT bash tests/system/test_backup_roundtrip.sh`, `tests\system\test_kill.ps1`); matrix check for FEAT-008; `git status --porcelain` empty.

---

## Stage 3: Telegram presentation (FEAT-009, FEAT-010; DESIGN §14 / CR-003)

python-telegram-bot stays at 22.8 with the typed Bot API 10.1–10.3 adapter (`telegram/rawapi.py` over `do_api_request`); aiogram and pyTelegramBotAPI stay rejected and Pillow stays out of the gatekeeper (ADR-0020). Every handler follows the item 25 budget.

### FEAT-009: Telegram workspace M1

Depends on FEAT-005…008.

- [ ] 44. Component kit and plumbing.
      Rows: DES-AC-14 (kit part), DES-V-20, DES-V-32, ADR-0004, ADR-0020b, CR2-7b, TGRW-10, TGRW-11, TGRW-13, TGRW-14 (kit part), TGRW-15, CR3-M1, CR3-D1, CR3-D2, CR3-D3, CR3-D12, CR3-D13, CR3-D16, CR3-U1, CR3-U2, CR3-U6, CR3-U8, CR3-AC-1, CR3-AC-2, CR3-AC-3, CR3-AC-12, CR3-AC-13, CR3-AC-14, CR3-AC-25, CR3-AC-26, CR3-AC-37, CR3-BA-6, CR3-BA-7, CR3-V-1, CR3-V-4, CR3-V-12, CR3-V-14, CR3-V-15.
      What: `telegram/ui/{kit,blocks,classic,rich,sanitize,limits,icons,send}.py`; `telegram/rawapi.py` (typed 10.1–10.3 payloads; verify §14.23 items first); `escape.defuse_commands` / `sanitize.undefuse`; `config/telegram.yaml` + strict schema through the snapshot; topics with flat fallback; menu button and per-state command scopes; pinned dashboard; `telegram/routing.py` (the normative §14.10.4 table; non-command updates to the chat backend; GitHub links to link cards); `telegram/pending_input.py` (15 min TTL, matched only by `reply_to_message.message_id`, single consumption); migration `0005_telegram_ux.sql` (§14.20.2); import-linter contracts of §14.20.1; Phase 1 docstrings citing old §14.1–§14.5 pointed at current sections.
      Tests: new `tests/telegram/{test_kit_*.py,test_rawapi.py,test_routing.py,test_pending_input.py,test_topics_fallback.py,test_scopes.py,test_dashboard.py}` and goldens under `tests/telegram/golden/`; `tests/fakes/fake_telegram.py` gains the 10.x methods.
      Verify: `DEV pytest -m "unit or telegram"` green; `DEV lint-imports` green.

- [ ] 45. PR review surface S1–S12 (the PR review flow on Telegram before approval).
      Rows: DES-FR-5, DES-AC-14, ADR-0019 (S1–S12 part), TGRW-12, CR3-D4, CR3-D5, CR3-D6, CR3-D7, CR3-U3, CR3-U4, CR3-AC-4, CR3-AC-5, CR3-AC-6, CR3-AC-7, CR3-AC-8, CR3-AC-9, CR3-AC-10, CR3-AC-11, CR3-AC-23, CR3-AC-30, CR3-AC-32, CR3-AC-44, CR3-AC-45, CR3-AC-52, CR3-AC-53, CR3-AC-56, CR3-AC-60, CR3-BA-11, CR3-BA-13, CR3-F6-1, CR3-F6-6, CR3-V-5, CR3-V-6.
      What: `telegram/review/*` and the new card renderer replacing the minimal Phase 1 card in `cards.py`: S1 per kind with the size line (`+142 / −30 code · +210 test`, estimate vs actual) and `👤` marker; S2 explorer; S3 file views and diffs; `.patch` and HTML diff documents; S4 comments and draft review (`review_comments`); S5/S6 interdiff with `/package` interdiff; S7 edit title/body/comment (policy check first, item 25); S8 approve/confirm (`confirm_text`); S9 submitted; S12 stale; `/queue` ordering. The PR title is never cut (256-char golden); update mockups `docs/ux/01-pr-preview-card.svg` and `03-approve-confirm.svg` to show the full title.
      Tests: new `tests/telegram/{test_s1_cards.py,test_explorer.py,test_file_view.py,test_documents.py,test_review_comments.py,test_interdiff.py,test_edit_text.py,test_approve_confirm.py,test_stale.py,test_queue.py,test_title_never_cut.py,test_kit_tokens_survive.py,test_kit_token_resolution.py,test_reissue_keeps_draft.py,test_reissue_siblings.py}`; goldens.
      Verify: `DEV pytest -m "unit or telegram or e2e"` green (e2e preview path renders the new S1).

- [ ] 46. CR-004 presentation: proposal cards (gate 1), versions, rescope and ceiling cards, `/work` and link cards, `/size`, `/filters`.
      Rows: DES-FR-20, DES-AC-37, DES-AC-38, DES-AC-39, DES-AC-40, DES-AC-41, CR3-D18, CR3-D19, CR3-D22, CR3-U13, CR3-AC-38, CR3-AC-39, CR3-AC-40, CR3-AC-41, CR3-AC-43, CR3-AC-50, CR3-AC-51, CR3-AC-54, CR3-AC-61, CR3-AC-62, CR3-AC-63, CR3-BA-8, CR3-BA-12, CR4-1e, CR4-2b, CR4-2i, CR4-2k, CR4-2l, CHAT-27, CHAT-29, CHAT-30.
      What: §14.11.15–§14.11.19 and §14.14.4 on the item 29 backend: proposal card (problem, approach, areas, estimate and class, tests, risks, confidence, smaller alternative) with Start work / Adjust / Smaller / Skip / Later / Never and Undo; deterministic version diff; RESCOPE and ceiling cards (ceiling read-only); `/work <url> [notes]` and forwarded/pasted GitHub link cards with `🛠 Work on this`; `/size` (`xs`…`xl`, per-repo `inherit`, `xxl` never selectable, ceiling shown read-only); `/filters` (4 pages) via `discovery_prefs`.
      Tests: new `tests/telegram/{test_proposal_card.py,test_proposal_versions.py,test_proposal_undo.py,test_rescope_card.py,test_ceiling_card.py,test_work_link_cards.py,test_size_picker.py,test_filters_picker.py}`; `tests/unit/gatekeeper/test_ceiling_not_writable.py` re-run covers the new purposes.
      Verify: `DEV pytest -m "unit or telegram or e2e"` green.

- [ ] 47. Controls, notifications and states.
      Rows: DES-FR-6, DES-FR-17 (Telegram part), DES-AC-31 (Telegram part), DES-AC-35, CR1-5c, CR2-2a, CR2-3a, CR2-3d, CR2-7k (M1 part), CHAT-12, CHAT-25, TGRW-14, CR3-D11, CR3-D14, CR3-D15, CR3-U7, CR3-AC-16, CR3-AC-17, CR3-AC-21, CR3-AC-33, CR3-AC-34, CR3-AC-49, CR3-F6-5.
      What: the full §14.14.1 command set with pickers and confirmation patterns: `/drain /undrain /down /up` (item 42 helper), `/egress` (go class, two-tap, item 23 API), `/hermes tier` (two-tap, item 23a/36), `/skills` + skill card (§14.15), `/model` + new-model card, `/reload`, `/repos`, `/limits`, `/logs`, `/settings`, `/quiet`, `/notify`, `/new`; priorities P0–P3, quiet hours, coalescing and batching (never for rows with buttons), offline and killed states (§14.16), short-description updates.
      Tests: new `tests/telegram/{test_commands_full.py,test_egress_cmd.py,test_hermes_tier_cmd.py,test_skill_card.py,test_model_cmd.py,test_lifecycle_cmds.py,test_priorities.py,test_quiet_hours.py,test_coalesce.py,test_offline_states.py}`.
      Verify: `DEV pytest -m "unit or telegram"` green.

- [ ] 48. Close-out FEAT-009.
      What: `REDEPLOY`; hermetic suites; live owner-only checks (topics created or flat fallback, dashboard pinned, a dry-run proposal and preview card from the fake upstream rendered in the real chat, `/kill` then `/resume` from Telegram); matrix, VERIFICATION, FEAT-009 `completed`, commit.
      Verify: FEAT-009 verification list; matrix check for FEAT-009; `git status --porcelain` empty.

### FEAT-010: Telegram M2–M4 and v1.x

Depends on FEAT-009.

- [ ] 49. Streaming assistant UX.
      Rows: DES-FR-13, DES-AC-36, CR2-7a, CR2-7h, CR2-7k, CR2-7l, CHAT-26, TGRW-9, CR3-D8, CR3-D9, CR3-D10, CR3-D21, CR3-U10, CR3-U12, CR3-AC-18, CR3-AC-19, CR3-AC-20, CR3-AC-22, CR3-AC-35, CR3-AC-48, CR3-AC-55, CR3-BA-14, CR3-BA-22, CR3-F6-2, CR3-M2, CR3-V-2, CR3-V-3, CR3-V-7, CR3-V-8, CR3-V-13, CR3-V-16, CR3-IH-9, CR3-IH-10.
      What: drafts with Stop (`sendRichMessageDraft`/`sendMessageDraft`, `can_stop`), `ALLOWED_UPDATES` gains `stopped_message_generation` authorized by `auth.authorized_chat_only()`; `chat/{stream,drafts,md2blocks,followups,answer_view}.py` (flat mode and Reviews wrap drafts too); sources and footer; follow-ups; albums (1.5 s grouping) and reactions; `/new /title /memory`; feature cards and Hermes notifications (U12); assistant-unavailable states; PDF and voice UX of §14.12.5 on the item 37 backend. Every stream runs as a spawned `chat_turn`.
      Tests: new `tests/telegram/{test_stream_drafts.py,test_stop.py,test_md2blocks.py,test_followups.py,test_albums.py,test_answer_view.py,test_assistant_unavailable.py,test_pdf_voice_ux.py}`; `test_handlers_nonblocking.py` extended with Stop during a stream.
      Verify: `DEV pytest -m "unit or telegram"` green.

- [ ] 50. Rich views and status.
      Rows: CR3-AC-42, CR3-AC-64, CR3-BA-9, CR3-M3, CR4-3c, CR3-IH-1, CR3-IH-2, CR3-IH-3.
      What: rich explorer/file/interdiff/tests/assistant views behind `auto` with classic fallback; S10/S11 live status card with own-PR activity and drafted replies; reminders (`/remind`, `ossbot_reminder_create`); daily and weekly digest with merge rate by size class and the §14.19 item 1 stats (followers, stars, merges, streak) from gatekeeper GitHub reads; migration `0006_telegram_m2.sql` (`digest_runs`, `reminders`, `review_mirror`, `telegraph_pages`).
      Tests: new `tests/telegram/{test_rich_fallback.py,test_status_card.py,test_reminders.py,test_digest.py}`, `tests/unit/gatekeeper/test_outcomes_digest.py`.
      Verify: `DEV pytest -m "unit or telegram"` green.

- [ ] 51. Review mirror, opt-in, off by default.
      Rows: ADR-0019 (C part), CR3-AC-24, CR3-U5, CR3-U9 (mirror part), CR3-M4, CR3-V-9.
      What: `gatekeeper/review_mirror.py`, `gatekeeper/review_mirror_writer.py` (own import-linter contract), `ossctl review-mirror enable|disable|status|purge`, optional key in `setup-secrets`. No live mirror write during the build (zero live GitHub writes); tested against `fake_github`.
      Tests: new `tests/unit/gatekeeper/{test_review_mirror.py,test_review_mirror_writer.py}`; `test_import_contracts.py` updated.
      Verify: `DEV pytest -m unit` green; `DEV lint-imports` green.

- [ ] 52. Telegraph articles, off by default.
      Rows: CR3-D17, CR3-U9, CR3-U11, CR3-AC-27, CR3-AC-28, CR3-AC-65, CR3-AC-68, CR3-BA-10, CR3-BA-19, CR3-BA-20, CR3-F6-3, CR3-F6-4, CR3-V-11, CR3-v1.x, CHAT-36, CR3-IH-18.
      What: `gatekeeper/telegraph.py`, `gatekeeper/telegraph_writer.py` (own contract), gatekeeper-only `telegraph` egress category rendered only while `telegraph.enabled: true` (every other caller keeps the always-denied entries from item 23), `ossctl telegraph setup|rotate`, `/articles`; opt-in assistant articles only. No live Telegraph publish during the build unless the user enables it.
      Tests: new `tests/unit/gatekeeper/{test_telegraph.py,test_telegraph_writer.py}`, `tests/unit/ctl/test_render.py` case for the enabled category, `tests/telegram/test_articles.py`.
      Verify: `DEV pytest -m "unit or telegram"` green; `DEV lint-imports` green.

- [ ] 53. Close-out FEAT-010.
      What: `REDEPLOY`; hermetic suites; live owner-only checks (a streamed sourced answer, Stop, a food photo through Telegram, an album); matrix, VERIFICATION, FEAT-010 `completed`, commit.
      Verify: FEAT-010 verification list; matrix check for FEAT-010; `git status --porcelain` empty.

---

## Stage 4: docs, hygiene, CI, final verification (FEAT-011)

Depends on everything above.

- [ ] 54. README, AGENTS.md, SETUP.md (with backup/restore), ARCHITECTURE.md, steering, `profile.example.yaml`.
      Rows: DES-FR-10, DES-FR-15, DES-FR-16, DES-NFR-10, ADR-0015, CR1-8, CR2-5, CR4-6, CR3-docs, CHAT-15, CHAT-16, CHAT-17.
      What: `README.md` (what it is, safety model with both gates, mermaid architecture, quickstart, daily use, Telegram tour, egress modes and tiers, how the bot picks work and the size preference vs the hard ceiling, user-directed work, AI disclosure, license TBD); `AGENTS.md` (architecture map, invariants incl. lifecycle, two gates, ceiling not Telegram-raisable, no exposure; how to add a command, feature, target, stage, model role and Telegram view per §14.18.3); `docs/SETUP.md` (fresh-install DR runbook, credentials, BotFather topics, `ossctl backup`/`restore` and `backup.ps1`/`restore.ps1`, doctor); `docs/ARCHITECTURE.md`; `.kiro/steering/{product,tech,structure,safety-invariants,web-research}.md`; `.kiro/agents/harness-dev.json`; `ossctl docs commands [--check]` generating the README command table. Decision B7: `config/profile.example.yaml` gains every Phase 2 profile key with generic placeholders.
      Tests: new `tests/unit/ctl/test_docs_commands.py` (`--check` fails on drift), `tests/unit/common/test_profile_example.py` (the example loads with `ProfileConfig` and holds no owner value).
      Verify: `DEV pytest -m unit` green; `DEV ossctl docs commands --check` exits 0.

- [ ] 55. Secret hygiene.
      Rows: DES-NFR-11, DES-AC-17, DES-AC-23, DES-V-23, CHAT-20.
      What: `.gitleaks.toml` (extends defaults, the §12.6 patterns, allowlist only for test fixtures), `.githooks/pre-commit` (gitleaks protect + `ossctl lint personal`), `scripts/install-hooks.ps1`, `src/ossbot/ctl/lint_personal.py`; AC 17 proven in a throwaway clone under `.ossbot-local/hooktest/`.
      Tests: new `tests/unit/ctl/test_lint_personal.py`.
      Verify: `DEV pytest -m unit` green; `ROOT gitleaks git --redact --config .gitleaks.toml .` exits 0; `DEV ossctl lint personal` exits 0.

- [ ] 56. CI workflow (optional to run, required to exist and be tested locally).
      Rows: DES-AC-24, DES-V-27, CHAT-21.
      What: `.github/workflows/ci.yml` (`permissions: contents: read`, SHA-pinned actions, lint, hermetic tests, gitleaks, PSScriptAnalyzer, docs check, profile example check). Nothing is pushed, so it does not run on GitHub during the build.
      Tests: new `tests/unit/test_ci_workflow.py` (permissions, every `uses:` pinned by 40-hex SHA, no secrets in `pull_request_target`).
      Verify: `DEV pytest -m unit tests/unit/test_ci_workflow.py` green.

- [ ] 57. Final verification and report.
      Rows: DES-FR-11, DES-AC-3, NOTE-2, NOTE-9, CHAT-34, CHAT-37.
      What: run "Required overall verification" and the "Phase 2 verification" block of PLAN.md; full `ossctl doctor --probes` and `scripts\ossctl.ps1 doctor`; the real Windows restart test of 6a/6b (ask the user to restart the laptop with `send_message` severity `warning`, then record the observed shutdown types); every matrix row `done` or `deferred`; write `docs/FINAL_REPORT.md` (summary, doctor results, live checks, user actions needed); FEAT-011 `completed`; commit.
      Verify: every command of both blocks passes; the matrix check with `$f='.'` lists no row; `git status --porcelain` empty.

---

## Workflow structure that runs this plan

- Build loops, one per FEAT in order FEAT-005 → FEAT-011, each a `repeat` (max 10 iterations, `onMaxIterations: pause`) of a coder step then a reviewer step. The reviewer is last and writes `d:\Work\autonomous-oss-contributor\docs\review-build-FEAT-00N.json` last, every iteration, with `"verdict": "APPROVED"` or `"CHANGES_REQUESTED"`; the loop stops when that file's `verdict` is `APPROVED`. Iteration 1 implements the FEAT; later iterations fix every finding of `docs\review-build-FEAT-00N.md`/`.json`.
- Phase 2 gate loop (max 4, `onMaxIterations: pause`): fixer then reviewer over the whole Phase 2 diff (`e0ecf11..HEAD`) and the evidence in the FEAT files, verdict `d:\Work\autonomous-oss-contributor\docs\review-phase2.json`.
- Finalize step: commits the remaining review files, confirms `git status --porcelain` is empty, and reports. Nothing is pushed.

## Working rules for every build step (orchestrator, added after code-feat-005 ran out of context)

Coder steps can run out of context and stop mid-task, leaving uncommitted work. Follow these rules in every build step:

1. **Start by checking for leftovers.** Run `git status --porcelain` before anything else. If it isn't empty, a previous step stopped mid-work. Read those changes, then finish, test and commit them, or deliberately revert them with a note in VERIFICATION.md. Never silently overwrite, stash or discard them.
2. **Commit in small units.** Commit after each plan item or sub-item once its tests pass. Don't hold large uncommitted batches. Also update TRACEABILITY.md and VERIFICATION.md as you go, not only at the end.
3. **Keep context lean.** Don't paste huge files or full test logs into context. Use grep/targeted reads and `tail` the logs.
4. **Reviewers:** never approve while the tree is dirty, or while committed work lacks test evidence. Instead, return CHANGES_REQUESTED with a "finish the WIP" finding.
5. **Pushes to origin are the owner's.** Amsal pushes `main` to the private remote himself, whenever he likes, so origin/main moving forward is expected and is not a finding. Workflow steps still never push, force-push or rewrite history. Treat it as a problem only if origin/main contains commits that are not in local main, or if history was rewritten.

## Addition: GitHub MCP server, read-only (user request, 2026-10-18)

Add `github/github-mcp-server` as a read-only GitHub tool for **scout**, **self-review** and **Hermes**. Owner FEAT: the one that owns Kiro agent configs and the scout stage. If that FEAT is already closed, the next open pipeline FEAT. Add TRACEABILITY rows.

- **Setup:**
  - local `stdio` binary, not the hosted remote server, pinned in versions.lock:
    - **Release:** **v2.0.2**, published 2026-10-08. An immutable GitHub release; tag commit `eb47a99`, signed and verified.
    - **Asset:** `github-mcp-server_Linux_x86_64.tar.gz`
    - **sha256:** `815e27d931c796af0c97f24fd211e14f8a4607d4528377719a5ad2352086b0b0` (from the release API asset digest)
    - **Install:** provision downloads it via the github_read egress, verifies the sha256 and installs it root-owned 0755 into the sandbox-llm image and the Hermes host path. Upgrade only via a versions.lock bump.
  - flags `--read-only` and `--toolsets repos,issues,pull_requests,actions` (actions only for CI run/log reads), plus explicit `--tools` for search
  - verify the exact tool and toolset names against the pinned release
- **Token:** reuse the existing `GITHUB_READ_TOKEN` (fine-grained, "Public repositories (read-only)"). The user decided this (2026-10-18); no new secret.
  - sandboxd hands it only to MCP-enabled research jobs (scout/self-review). Hermes gets it through its own env file.
  - Add its sha256 to the output key-scan, so a leaked read token is detected like a Kiro key.
  - It never reuses the write token, and is never present in fetch/validate sandboxes.
  - Accepted trade-off: if it ever has to be revoked, the runner's discovery reads stop too, until a new token is set.
  - Two layers protect writes: the `--read-only` flag, and a token that can't write.
- **Where it runs:**
  - in the scout and self-review jobs, under the `research` profile; `api.github.com` is already reachable there
  - for Hermes as a plugin-provided MCP tool under the capability tier, egress via 3141
  - the gatekeeper and its write paths never use it
- **Lockdown mode stays off for scout,** because scout must read issue and comment text from non-collaborators. That content is treated as untrusted, as today: quoted, marked 🤖 and never followed as instructions.
- **What scout uses it for:**
  - maintainer and merge-latency signals, recent merged PRs, CI status and logs
  - CONTRIBUTING and the AI policy
  - competing PRs and linked issues
  - cross-repo code search
  - `get_file_contents` for targeted reads, which helps on repos too big to snapshot (`snapshot_too_large`)
- **Safety probes:**
  - the M11/coder_surface probes assert that only read tools are exposed (no create/update/delete/merge/push ids)
  - a startup check fails closed if any write tool appears
  - the key scan covers MCP outputs too
- **Tests:**
  - tool-surface allowlist test with a fake MCP listing
  - "write tool listed → fail closed" test
  - live read check via doctor: `get_me` = Amsal1, read a public repo
6. **Shell commands must not trigger approval prompts** (user rule). Inline PowerShell with `$variables`, `$(...)` subexpressions, `-replace`, base64 one-liners or long chained pipelines needs manual approval each time and stalls the run.
   - **Use instead:** short, plain commands. For anything multi-line or with variables, put the script in a file in this workspace, e.g. `.ossbot-local/` (gitignored) or `scripts/dev/` (committed, if reusable). Then run it by reference: `wsl -d ossbot -u root -- bash /mnt/d/Work/autonomous-oss-contributor/scripts/dev/<name>.sh`, or stream it over stdin with `Get-Content <file> -Raw | wsl -d ossbot -u root -- bash -s`.
   - **Prefer** the existing helpers: `scripts/*.ps1`, `ossctl`, `.ossbot-local/*.sh`.
   - **Reviewers:** follow the same rule when re-running checks.
7. **Forbidden shell shapes** (enforced by `.kiro/hooks/guard-shell.json` → `scripts/dev/guard-shell.ps1`): here-strings (`@'`, `@"`), `@py…`, `python -c`/`py -c`, base64 or `-EncodedCommand` payloads, `Invoke-Expression`/`iex`, and `$(...)`. Write code with the file tool into `.ossbot-local/` or `scripts/dev/`, then run it by path. If you see "Blocked by guard-shell", rewrite the command that way; never work around the guard.
