# Design review: docs/DESIGN.md (Draft v2, revision 4)

> **Historical (2026-10-18).** This is the round-5 review that APPROVED revision 4. The current spec is DESIGN.md revision 5 (consolidation of CR-001…CR-004, NOTE-live-secrets and the chat decisions); its requirements and status are in `docs/TRACEABILITY.md`.

Reviewer: design-review step (fresh context, round 5) · Date: 2026-10-17
Scope:
- DESIGN.md, with a close reading of every section revision 4 touched: §0 facts, 7.3, 7.5, 8.4 T2, 9.8, 9.11, 9.13, 10.8, 10.9, 12.2, 12.4, 12.5, 12.5a, 15.2, 15.4, 19, 22.1, 22.6, §23 items 13 and 19, and §27.0a.
- The revision-4 amendments in ADR-0002, 0003, 0014 and 0017.
- The round-4 review and verdict (R4-M1, R4-M2, R4-N1..N6).
- Everything checked against the ten verbatim user messages.

Live checks this round:
- A local git 2.55.0 experiment in a temp dir under the gitignored `.ossbot-local/`, removed afterwards. It confirmed three things:
  - a fetch from a local repo path by bare SHA works under the default protocol v2, which is what `package.sh` does;
  - `-c protocol.file.allow=never -c protocol.file.allow=always` allows the fetch, and the reverse order refuses it with `transport 'file' not allowed`, so the last `-c` wins;
  - plain `never` refuses a fresh local fetch.
- Nothing was pushed or fetched from any remote. No builds or tests were run, because there is no code yet.

Verdict: **APPROVED** (0 HIGH, 0 MEDIUM, 3 NIT)

## Overall assessment

Revision 4 resolves all 8 round-4 findings. Both MEDIUMs are fixed correctly and consistently across DESIGN, §19, §22, §23 and the ADRs:
- **R4-M1.** A scoped `protocol.file.allow=always` now applies to the bundle fetch argv only, on gatekeeper-built paths. `never` stays the default everywhere else, and there is a test that the production args are used.
- **R4-M2.** The root probe imports a stdlib-only `kiro_acp.v3_core` from the root-owned `/opt/ossbot/app/vendor/` copy and runs under `python -I`. It compares the hermes-side copy by hash (`adapter_drift`), and `adapter_sha256` is part of the probe key.

The four stated deviations are justified:
- the `/opt/ossbot/app/vendor` location;
- the stdlib-only `v3_core` split, which also closes the "import Hermes base classes as root" route;
- `uv sync` running after the move, because venvs are not relocatable;
- `probe.sh` accepted in every profile, with the key bound to the (profile, `Cmd`) pair.

The trust boundary still holds in the design text:
- Approval is deterministic, Telegram-only and two-tap. The mode is recomputed at Approve and at Confirm, and now (R4-N2) at every write checkpoint.
- Restart reconciliation is reads only. No path resumes a write without a fresh approval.
- No hermes-writable byte runs as root.
- The Docker filter independently refuses any Kiro run outside the probe-gated (profile, `Cmd`) pairs.
- The Kiro key stays confined to research, agent-llm and catalog containers, plus the hermes uid with its own revocable key.

I found three new items. Each is defence-in-depth hardening with no demonstrated path across the trust boundary, so they are NITs and the design is ready for implementation.

---

## Resolution of round-4 findings

| ID | Status | Evidence in revision 4 |
|---|---|---|
| R4-M1 | Resolved | **Gatekeeper.** §12.5 defines `BUNDLE_FETCH_ARGS = GIT_ARGS + ["-c","protocol.file.allow=always"]`. It is used only for the `/package` fetch (step 3) and the import-validation fetch (9.13 step 2). `bundle_path` is gatekeeper-built from `cid` and the integer `rev`, and a mismatched `artifact_dir` returns 400. `list-heads` and `verify` keep plain `GIT_ARGS`. A fetch failure returns 422 `bundle_invalid`.<br>**Sandbox.** `unbundle.sh` (9.8, and 9.13 step 4 for import) and `package.sh` (fetch from `/work/repo/.git`) carry the same scoped override, and 9.11 states `never` stays the default.<br>**Docs and tests.** 8.4 T2, §0, §19 and §23 19 are updated, and ADR-0014 is amended. New tests: `test_bundle_fetch_needs_scoped_override`, `test_gitops_args.py` (every other call has effective `never`), `test_unbundle_script_real_bundle` (both modes), and `test_package_verify.py` on production args. My git 2.55 run confirms that the last `-c` wins and that the scoped override is sufficient. |
| R4-M2 | Resolved | **Probe process.** §15.2, §7.5, §7.3 and ADR-0002: the probe runs `/opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.hermes_probe`, cwd `/`. It imports `kiro_acp` only from `/opt/ossbot/app/vendor/hermes-kiro-provider/` (root:root 0755/0644, installed by `app deploy`), and nothing under `/home/hermes` is ever imported or put on `sys.path`. `launch_spec`, `session_new_params` and `assert_surface` are in stdlib-only `v3_core.py`, and the Hermes glue is never imported by root.<br>**Drift check.** The hermes-side copy is hashed with `O_NOFOLLOW`, regular files only and sorted paths. A symlink, special file or mismatch gives `adapter_drift`, which is handled as a failed probe (soft kill, alert). `adapter_sha256` is in the probe key.<br>**Executables.** `kiro-cli` runs only via `runuser -u hermes --`, and the NDJSON stream is parsed as untrusted data.<br>**Tests.** `test_hermes_probe_paths.py` checks module origin, `sys.path`, that no `hermes*` module is loaded, that `PYTHONPATH` is ignored, and the drift cases. |
| R4-N1 | Resolved | 9.8 and 9.13 step 4: a single head equal to the runner-supplied `bundle_ref` from gatekeeper state (`refs/heads/ossbot-change` for `fix_round`, `import_head_ref` for `import`). §19 row and unbundle tests in both modes. |
| R4-N2 | Resolved | §12.5 `control_checkpoint(snapshot)` requires `running` and `control_state.dry_run == snapshot.dry_run` before steps 4–6, before every retry, and before the 12.5a push. A failure gives `interrupted` with cause `mode_changed`, then `SUBMIT_FAILED` and a fresh card. Also in §12.4 and ADR-0003, with test `test_dryrun_on_mid_submission_stops_write`. |
| R4-N3 | Resolved | §12.2 `POST /v1/models/catalog` (worker): 1–200 entries, the `--force` regex, `name` ≤ 200, no `forced` field, `forced` never changed, and stale ids omitted from GET unless forced. Referenced from 10.9 step 2 and 16.3. Test `test_catalog_upsert.py`. |
| R4-N4 | Resolved, with a residual race noted in R5-N2 | §15.4: clone and verify in root-only `/var/lib/ossbot/staging`, `chown -R --no-dereference`, `refuse_link` on every path involved, `stat` owner/type check on `$H`, `mv -T $D $D.prev; mv -T $S $D`, `uv sync` as `hermes` at the final path, and rollback to `$D.prev`. Hermes is stopped, all `hermes` processes are killed, and `pgrep` is re-checked. System test `test_hermes_install_hygiene.sh`. |
| R4-N5 | Resolved | §10.8 `Cmd` row binds the entrypoint per profile. `KIRO_API_KEY` is allowed only for (research, kiro), (agent-llm, kiro) and (catalog, list_models). `stack` is refused, and `stage-in` is `/bin/true` only. Four deny-corpus cases (22.1), and ADR-0017 is amended. |
| R4-N6 | Resolved | §9.11 `OSSBOT_NUMSTAT` (`diff.algorithm=myers`, `diff.renames=false`, `core.quotePath=true`, `--no-ext-diff --no-textconv --no-color`), defined once in `ossbot.common.gitcmd` and baked into the image. The gatekeeper uses it in step 5. Tests `test_numstat_pinned` and `test_gitcmd_image_env.py`. |

---

## New findings

### HIGH

None.

### MEDIUM

None.

### NIT

**R5-N1. The gatekeeper bundle fetch, now running with `protocol.file.allow=always`, reads from an ossbox-writable directory. A swap after the sha256 check could make the fetch read a local repository instead of a bundle.**
Where: §12.5 (the `BUNDLE_FETCH_ARGS` paragraph and step 3); §7.3 (`/srv/ossbot/artifacts/<cid>/<rev>/` is ossbox:ossart 0750).

The path is gatekeeper-built, but its directory belongs to `ossbox`. `git fetch <path>` treats a regular file as a bundle and a directory as a local repository, which means running `upload-pack` against that repository's own config. Under revision 3's blanket `never`, the directory case was refused. The scoped `always` re-enables it.

So a compromised sandboxd (a lower trust tier, without the write token) could replace `change.bundle` with a repository directory between the sha256 check and the fetch. The gatekeeper's git would then run against attacker-chosen repo config. I know of no repo-level key that makes `upload-pack` execute code:
- `uploadpack.packObjectsHook` is honoured only from protected config;
- `core.hooksPath` and `core.fsmonitor` are overridden on the command line.

Integrity is also unaffected. Every check, the derived preview and the binding come from the gatekeeper's own mirror, and the bundle is re-hashed at Confirm. This is therefore hardening, not a hole.

Fix: run all bundle commands on a gatekeeper-private copy, and never recurse into submodules.
```python
# 12.5 step 3 and 9.13 step 2: copy once, then operate only on the copy
fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
st = os.fstat(fd)
if not stat.S_ISREG(st.st_mode) or st.st_size > 50 * 2**20:
    raise Http422("bundle_invalid")
priv = Path(f"/srv/ossbot/gatekeeper/work/{cid}-r{rev}.bundle")   # gatekeeper 0700 tree
copy_fd_to(fd, priv, mode=0o600)
if sha256_file(priv) != req.sha256s["change.bundle"]:
    raise Http422("bundle_invalid")
# list-heads / verify / fetch all use `priv`
run(["git", *BUNDLE_FETCH_ARGS, "fetch", "--no-tags", "--no-recurse-submodules", str(priv),
     f"refs/heads/ossbot-change:refs/ossbot/r{rev}"], env=GIT_ENV, cwd=mirror)
```
Also add `--no-recurse-submodules` to the fetches in `unbundle.sh` and `package.sh`, which run in a hostile work tree. Add one case to `test_gitops_args.py`: a directory at the artifact path gives 422 `bundle_invalid`.

**R5-N2. A process can still run as `hermes` during the Hermes upgrade swap window, so "the `refuse_link` checks cannot be raced" is not fully true.**
Where: §15.4 install script and the "Install hygiene" paragraph; §15.2 probe triggers; §27.0a "R4-N4 order and race".

Three gaps:
- The gatekeeper can trigger `ossbot-hermes-probe` hourly, or on demand, during an upgrade. The probe launches `runuser -u hermes -- /home/hermes/.local/bin/kiro-cli`, a hermes-writable binary, while root is renaming paths inside `~/.hermes`.
- `ossctl hermes login` can also start a `hermes` process in that window.
- The rollback branch runs `pkill` then `sleep 1` but has no `pgrep` re-check, and the `uv sync` it follows runs hermes-uid build code.

The names root moves are fixed (`hermes-agent`, `.prev`, `.failed`), and `$H` is type-checked, so I see no concrete root escalation. It is still a gap in a stated guarantee.

Fix:
- `provision.sh` takes `flock /run/ossbot/hermes-upgrade.lock` for the whole step and stops `ossbot-hermes-probe.socket` until the step ends. `ossbot.ctl.hermes_probe` and `ossctl hermes login` take the same lock non-blocking; while it is held the probe is inconclusive ("Hermes is upgrading") and does not kill.
- Repeat the `pgrep -u hermes` refusal after the `pkill` in the rollback branch.
- Add a case to `test_hermes_install_hygiene.sh`: a probe triggered mid-upgrade is refused.

**R5-N3. Importing `kiro_acp.v3_core` first runs `kiro_acp/__init__.py`, and the design does not say how the vendor path reaches root's `sys.path` under `python -I`.**
Where: §15.2 code-origin bullets; §18.2 `app deploy`.

A Hermes provider package's `__init__.py` typically does the provider registration, which imports Hermes base classes. Root's venv has no Hermes, so the import fails closed and the `test_hermes_probe_paths.py` check "no `hermes*` module is loaded" would catch it. This is an implementability gap, not a hole. `-I` also ignores `PYTHONPATH`, so the vendor tree must reach `sys.path` some other way, which is unstated.

Fix: state both rules.
- The fork keeps `kiro_acp/__init__.py` import-free, and Hermes registration moves to `kiro_acp/hermes_plugin.py`, which the plugin manifest names as its entry point.
- `pyproject.toml` declares `vendor/hermes-kiro-provider` as a uv path dependency, installed non-editable into `/opt/ossbot/app/.venv`.
- Add to `test_hermes_probe_paths.py`: `kiro_acp.__file__` lies under `/opt/ossbot`, and importing `kiro_acp.v3_core` loads no module outside the stdlib, `ossbot` and `kiro_acp`.

---

## Verified assumptions

- **git 2.55.0, local, this round:**
  - `-c protocol.file.allow=never -c protocol.file.allow=always fetch <local path> <sha>` succeeds, and the reverse order fails with `transport 'file' not allowed`. The last `-c` wins, which is the premise of `BUNDLE_FETCH_ARGS` and of `$G -c protocol.file.allow=always`.
  - A fetch of an unadvertised SHA from a local repo (`package.sh`: `fetch /work/repo/.git "$ROUND_BASE"`) succeeds under the default protocol v2.
  - Plain `never` refuses a fresh local fetch, so the override in `package.sh` is required.
  - Round 4's results (bundle fetch refused under `never`; `bundle verify` and `list-heads` unaffected) are recorded in §0 and §23 19 exactly as measured.
- **R4-M1, design-internal:**
  - `BUNDLE_FETCH_ARGS` appears only at 12.5 step 3 and 9.13 step 2, and both paths are gatekeeper-built.
  - `test_gitops_args.py` pins every other call to an effective `never`.
  - The display and the binding still come from the gatekeeper mirror (12.4, 12.5 steps 4–6).
- **R4-M2, design-internal:**
  - `/opt/ossbot/app` is root:root 0755 with files 0644 (7.3).
  - The unit's `ExecStart` uses the root venv with `-I` (7.5).
  - Hermes-owned files are only hashed, and hermes-owned executables run only via `runuser -u hermes --`.
  - The probe result file stays root:gatekeeper 0640.
- **R4-N2:** the checkpoint is required before fork, push, PR, every retry and the `push_update` push. Reconciliation is still reads only. Neither a restart nor `/resume` re-enters steps 4–6.
- **R4-N5:** the filter's key rule is keyed on the (profile, `Cmd`) pair, so `probe.sh` in any profile never receives the key, and `catalog` + `kiro.sh` is refused.
- **ADRs:** the revision-4 amendments in 0002, 0003, 0014 and 0017 match DESIGN §9.8, 9.11, 9.13, 10.8, 12.2, 12.5 and 15.2–15.4.
- **User intent** (unchanged by revision 4, re-checked against the touched sections):
  - Isolation: everything is under `D:\Work\autonomous-oss-contributor`. The VHD is in `.wsl\`, only the repo is mounted, and interop is off. New paths (`/opt/ossbot/app/vendor`, `/var/lib/ossbot/staging`) are inside the distro.
  - Gating: Telegram-only two-tap approval is the sole human gate, with kill and pause at soft, full and hard levels. Everything else is autonomous.
  - Runtime: the dedicated `ossbot` distro, Kiro CLI V3 headless for the pipeline, and Hermes over V3 ACP behind the single bot.
  - Models, disclosure and vetting: the global model switch (`models.yaml` plus a catalog, now with a defined upsert route), the AI disclosure block, and maintainer/contributor vetting (§9.5).
  - Identity: DCO `Amsal Khan <md.amsalkhan@gmail.com>`, login `Amsal1`.
  - Docs: README, AGENTS.md and SETUP.md (FR-16, §6.1).

## Unverified or wrong assumptions

- **Unspecified, low risk (R5-N1):** whether the gatekeeper copies the bundle before fetching it. As written, it fetches in place from an ossbox-owned directory.
- **Unspecified (R5-N3):** how `kiro_acp` reaches root's `sys.path` under `-I`, and whether `kiro_acp/__init__.py` stays import-free. I could not inspect the vendored fork, because `vendor/` does not exist in the repo yet.
- **Not re-checked this round, correctly tagged [VERIFY] in §23:**
  - item 13: whether `v3_core` can be stdlib-only; whether `uv sync --frozen` at the final path yields a working entry point; Tool Search `off` parsing; the peeled tag commit;
  - item 19: the last-`-c`-wins and bundle-transport behaviour on the image's and the gatekeeper's git versions, and `patch-id --stable` equality across them;
  - item 11: V3 ACP `currentModeId` and custom `modeId` selection;
  - item 14: Docker 29 copy-up ownership and `exec` tmpfs;
  - item 5: Kiro Tool Search ids and `/tmp` exec needs.
- **Carried from earlier rounds, still tagged:** Telegram Bot API limits; GitHub PAT behaviour; Docker firewall backend and embedded DNS; drvfs fstab escaping; `instanceIdleTimeout=-1`; V3 ACP auth options; `--list-models` schema; squid attachment limits; `age -d -i -`.
