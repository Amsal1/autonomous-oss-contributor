# FEAT-008 build review, round 1: Windows shutdown hooks, root lifecycle helper, crash safety, and age-encrypted backup and restore

FEAT-008 finishes CR-001 and adds recovery. `status.ps1` now shows every CR-001 field. `down.ps1`/`up.ps1` serialise on `Local\ossbot-lifecycle`. A per-user shutdown blocker (6a) and a User32 1074 task (6b) are installed by bootstrap and removed by `-Uninstall`. A root path unit runs workers-scope `down`/`up` requests that the gatekeeper writes. The crash-safety test now covers the runner and Hermes. `ossctl backup`/`restore` are age-encrypted, with Windows wrappers and a disabled weekly timer. Every suite I re-ran matches the recorded evidence.

Watch for: restore runs as root and writes through `/home/hermes/.hermes`, a path the hermes uid can replace with a symlink. Hermes-authored files then land anywhere, and the originals are moved aside (confirmed by a hermetic probe; HIGH). The weekly backup lifts its pause with `ossbot-resume --paused-only`, which also starts runner, Hermes and sandboxd and so silently undoes a workers `/down` (confirmed, MEDIUM). Restore keeps going after a failed kill or a failed unit stop (confirmed, MEDIUM).

**Verdict**: NEEDS_CHANGES (verdict file `CHANGES_REQUESTED`; 1 HIGH, 2 MEDIUM, 3 NIT)

## High-level view

The Windows side has no open finding. The 6a blocker is a C# 5 window compiled at runtime from the repo. It is off-screen but visible, because ADR-0021's research found that hidden windows cannot block shutdown. The second action of `ossbot-autostart` starts it, with `PT0S`. The 6b task is built from XML (User32 1074 only, InteractiveToken + LeastPrivilege, PT2M). ADR-0021 credits SpiralPool for the idea, and no code was copied. `-Uninstall` removes only the ossbot-* tasks, the blocker, the four shortcuts and `.wslconfig`. Whether Windows waits on a real restart is still FEAT-011 item 57, as planned.

The root lifecycle helper consumes the request before any check and applies every rule the plan names. The gatekeeper also refuses `up` while killed. One NIT: the helper checks the file, then reads it again by path.

Backup and restore follow §18.8 at the core: anchors before the DB, online SQLite copies, audit verify on the copy, always encrypted, plaintext only in a 0700 ext4 staging dir, tamper refused before anything moves. A restore leaves the system killed with dry-run ON and all three decision services invalidated. The problems are at the edges. The Hermes item root trusts a path the hermes uid owns. The backup's own resume does more than remove its pause. And restore does not stop when its kill or unit stop fails.

<details>
<summary>Issues (6)</summary>

1. **F008-R1-1 (HIGH, confirmed by probe) Root backup/restore follow a symlinked `~hermes/.hermes`.** Restore writes the archive's Hermes files, owned by hermes, into whatever directory `.hermes` points to, and moves that directory's same-named files aside. Backup picks up files outside the Hermes home, for example the secrets, in an archive marked `includes_secrets: false`. Fix: refuse when the Hermes home (or any item root) is a symlink, resolves elsewhere, or is not a directory of the expected owner. Re-check after Hermes is stopped. Add unit tests for both directions. Files: `src/ossbot/ctl/backup.py`, `src/ossbot/ctl/restore.py`, `tests/unit/ctl/test_backup.py`, `tests/unit/ctl/test_restore.py`.
2. **F008-R1-2 (MEDIUM, confirmed in code) The backup's paused-only resume restarts the workers stopped by a workers `/down`.** `ossbot-resume --paused-only` removes `paused`, then runs the start loop (runner, Hermes, sandboxd, probe socket) on every enabled, inactive unit. Fix: with `--paused-only`, start nothing (a pause stops no unit), or at least skip the worker units while a lifecycle flag exists. Add a test. Files: `scripts/bin/ossbot-resume`, `tests/system/test_backup_roundtrip.sh` or `tests/system/test_lifecycle.sh`.
3. **F008-R1-3 (MEDIUM, confirmed in code; consequences likely) Restore keeps going after its kill or unit stop failed.** `kill_exit` and `stop_failed` are only reported. Decrypt, move-aside and install still run, even if the gatekeeper or Hermes did not stop or no `killed` flag was written. Fix: refuse (nothing moved, units restarted in killed mode) unless the `killed` flag exists and `stop_failed` is empty. Add a unit test with a failing `_systemctl`/helper. Files: `src/ossbot/ctl/cli.py`, `tests/unit/ctl/test_restore.py` (or `test_backup_cli.py`).
4. **F008-R1-4 (NIT, confirmed) The Hermes crash case can SKIP and the suite still passes.** If no chat turn starts (relay closed), `test_crash_safety.sh` records a skip, although the AC requires the Hermes case to pass. Fix: fail when the relay should be open (tier and probe OK), or make the run fail if this case was skipped. Files: `tests/system/test_crash_safety.sh`.
5. **F008-R1-5 (NIT, confirmed pattern; exploitable only by a compromised gatekeeper) The lifecycle helper checks the request, then reads it by path.** The `-L`/`-f` check and the `stat`/`head` reads are separate path lookups in a gatekeeper-writable directory. Fix: first rename the request into a root-only dir (rename does not follow symlinks), then check and read the moved inode. That also makes "always consumed" atomic. Files: `scripts/bin/ossbot-lifecycle`.
6. **F008-R1-6 (NIT, confirmed) Format count in VERIFICATION.** The close-out row says `ruff format` checked 350 files. At HEAD, `ruff format --check src tests` reports 342. Fix: correct the number. Files: `docs/VERIFICATION.md`.

</details>

<details>
<summary>Details</summary>

### Hermes home as a root write target (F008-R1-1)

`Layout.hermes_names()` lists `os.listdir(hermes_home)` and follows a symlinked `.hermes`. `item_root("hermes:<name>")` maps to `hermes_home / name`. `_move_aside`, `mkdir(parents=True)` and the `O_NOFOLLOW` open in `install` protect only the last path component. `ossbot-hermes.service` has `ReadWritePaths=/home/hermes`, so the hermes uid can rename `.hermes` and put a symlink in its place. ADR-0018 removes Hermes's file and terminal tools, so this needs code execution as hermes (a compromised plugin, update or skill script). The design still treats that uid as untrusted, and this path turns such execution into root writes on the owner's next restore.

A hermetic probe confirmed both directions. It was a scratch pytest file in the gitignored `.ossbot-local/`, built on the FEAT's own helpers and deleted afterwards. First, a backup was taken with an extra Hermes file `evil.service`. Then `.hermes` was replaced by a symlink to a stand-in for a root-owned directory, and restore's `unpack`+`install` was run. `evil.service`, `SOUL.md`, `config.yaml`, `memories/`, `skills/` and `state.db` were all written into the symlink target. In the other direction, with `.hermes` pointing at the secrets dir, the backup recorded `hermes:gatekeeper.env` while the manifest said `includes_secrets: false`.

```
hermes uid:  mv ~/.hermes ~/.h; ln -s /etc ~/.hermes        (any time before a restore)
root restore: _move_aside(/home/hermes/.hermes/<name>) -> moves /etc/<name> aside
              install -> /etc/<name> written with Hermes-authored bytes, owner hermes
```

Names are limited to what was backed up, but the hermes uid chose those names and their content when the backup ran (any `[A-Za-z0-9][A-Za-z0-9._-]*` not in `HERMES_EXCLUDE`, e.g. `passwd` or `bash.bashrc`). The archive is still encrypted to the owner, so the backup side leaks nothing to Hermes. It does break the meaning of the `includes_secrets` flag and of the §18.8 include list. The `artifacts/<cid>`, `imports/<cid>` and `chat` roots sit under directories owned by other service uids and deserve the same check.

### Backup resume undoes a workers down (F008-R1-2)

`_backup_pause` pauses through the API whenever ossbot is neither paused nor killed. A workers `down` writes only the lifecycle flag and stops runner, Hermes and sandboxd, so the backup pauses in that state too. `_backup_resume` then calls `ossbot-resume --offline --via backup --paused-only`. After `rm -f paused`, that path exits early only while killed. Otherwise it falls through to `start_unit` for gatekeeper, proxy, dockerfilter, sandboxd, runner, Hermes and the probe socket, and `start_unit` starts any enabled, inactive unit. The lifecycle flag stays `down`, so sandboxd admits nothing and the relay gate returns `workers_down`. But the units the owner stopped are running again, and `status.ps1` and the dashboard no longer match DESIGN §13.5 ("scope workers stops only runner, Hermes and sandboxd"). The weekly timer can do this unattended. A pause stops no unit, so the paused-only path has nothing to start. The full resume has the same start loop, but that is Phase 1 behaviour and out of scope.

### Restore's fail-closed gap (F008-R1-3)

DESIGN §18.8 step 1 is "writes `killed` ... and stops all ossbot units". `restore()` records `report["kill_exit"]` and `report["stop_failed"]` and then decrypts, moves aside and installs regardless. If `systemctl stop ossbot-gatekeeper` failed, the running gatekeeper keeps its file descriptors on the DB that was moved into `restore-previous-<ts>/`. It keeps serving the old state while `finalize_db` writes `killed`/`dry_run=1` into the new DB. If `ossbot-kill` failed before writing the flag, the restored DB says killed, but the flag the workers and the Docker filter read is missing. Reconcile then repeats its "kill flag missing" alert, the spam already noted for FEAT-009. Neither case was exercised live, and both start from a failure the code already detects.

### Test coverage

The unit tests cover the backup and restore contract (include list, encryption, recipients, WAL copies, tamper cases, the roundtrip state), the paused-only resume rules and the lifecycle-request rules. Each item also has a system test.

Not tested: a symlinked Hermes home (or other item root) in backup or restore, a backup during a workers down, restore with a failed kill or stop, and `test_bootstrap_zero.ps1` (it needs a throwaway distro; the findings say so).

</details>

## What I verified and how

- Scope: `git diff 903ad32..HEAD` (7 commits, 56 files), read against build-plan items 39 to 43, FEAT-008.json, DESIGN §13.5 and §18.8, ADR-0016 (addendum) and ADR-0021. Every item and AC is implemented with no scope cut. The two deliberate wording deviations (blocker kind `graceful` with `-Caller blocker`; restore's soft kill with gatekeeper and proxy restarted) are recorded in DESIGN, ADR and the findings, and stay within revision 5.
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e" -n auto` gave 2934 passed, the same as the findings.
- Lint gate: ruff check clean; ruff format clean (342 files); mypy clean (169 files); lint-imports 6 contracts kept. `ROOT shellcheck -x` on the 8 changed shell files was clean. The Windows PowerShell 5.1.26100 parser found 0 errors on the 15 changed `.ps1` files, all CRLF. The two non-ASCII bytes in `common.ps1` predate this FEAT (line 192).
- Deployment: `bootstrap.ps1 -Only provision` printed only `unchanged` (the deployed copy is HEAD). `scripts\ossctl.ps1 doctor`: Windows PASS for the autostart task, shutdown blocker (window visible), hook task and fast startup; distro PASS 45; `dry_run True`.
- Live: I re-ran `tests\system\test_shutdown_blocker.ps1`. All checks passed (reason created and destroyed, graceful down, `up.ps1`, last shutdown `graceful`). I did not re-run `test_backup_roundtrip.sh`, `test_lifecycle.sh` or `test_crash_safety.sh`. Their recorded evidence is detailed and consistent with the code, and the issues above come from code paths those suites do not exercise.
- Probe: a hermetic scratch test (gitignored `.ossbot-local/`, deleted afterwards) confirmed F008-R1-1 in both directions.
- Matrix: 19 rows name FEAT-008. Done: DES-NFR-3, DES-AC-22, DES-V-26, ADR-0016, CR1-5a, CR1-5d, CR1-6c2, CR1-6d, CHAT-7, CHAT-18. The 9 open rows each name a later owner (FEAT-009 item 47, FEAT-011 items 54/57) and carry "FEAT-008 part done" evidence. Totals line: deferred 28, done 271, missing 212, rework 11 (522 rows).
- Safety: dry-run ON (doctor and the blocker test's `up`). No GitHub write path changed. The new endpoints are operator-only. `gitleaks git --redact --log-opts=--all` scanned 164 commits with no leaks. `backups/` and `.ossbot-local/` are ignored and untracked. No secret file was read. No UAC prompt; host changes stay within the documented exceptions (ossbot-* tasks, Start-menu shortcuts only with `-Shortcuts`, `.wslconfig` via bootstrap). Apart from issues 1 to 3, I1 to I13 hold in the changed code.
- Git: all 7 commits are by Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `git status --porcelain` was empty before this review's files. `origin/main` is at `96fbfd0`, 2 commits behind local `main` and with no divergence. That is the owner's push pattern (working rule 5). This review pushed nothing.

<details>
<summary>File map</summary>

- `scripts/status.ps1`, `down.ps1`, `up.ps1`, `lib/common.ps1`: one-screen status, lifecycle mutex and log, blocker/hook/shortcut helpers.
- `scripts/windows/ShutdownBlocker.cs`, `ossbot-shutdown-blocker.ps1`: the 6a window and its launcher.
- `scripts/bootstrap.ps1`, `uninstall-host.ps1`, `ossctl.ps1`: `shutdownhooks`/`shortcuts` phases, `-Uninstall`, doctor checks.
- `scripts/bin/ossbot-lifecycle`, `systemd/ossbot-lifecycle.{path,service}`, `scripts/lib/services.sh`: root lifecycle helper.
- `src/ossbot/gatekeeper/{control.py,api/control.py,api/audit.py}`, `common/{paths,schemas}.py`: lifecycle request, status fields, `backup_created`, paused-only reconcile exception.
- `src/ossbot/ctl/{backup,restore,cli}.py`, `scripts/{backup,restore}.ps1`, `systemd/ossbot-backup.{service,timer}`, `scripts/provision.sh`, `config/versions.lock.yaml`: backup/restore and the pinned age.
- `scripts/bin/ossbot-{kill,resume}`: container wait, `--paused-only`, probe socket start.
- `src/ossbot/gatekeeper/chat/hermes_api.py`: every transport error is `HermesUnavailable`.
- `docs/adr/0021-*`, `docs/adr/0016-*`, `docs/DESIGN.md`, `docs/TRACEABILITY.md`, `docs/VERIFICATION.md`, plan files: research, addenda, evidence.
- Tests: `tests/unit/ctl/*`, `tests/fakes/fake_age.py`, `tests/unit/gatekeeper/{test_lifecycle,test_decision_fanout,test_authz}.py`, `tests/unit/gatekeeper/chat/test_backend.py`, `tests/unit/test_systemd_units.py`, `tests/system/*`.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 903ad32..af099ef`.

</details>
