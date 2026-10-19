# FEAT-008 build review, round 2: round 1 fixes for backup/restore symlink safety, paused-only resume, restore fail-closed and the lifecycle helper

Round 2 checks the five fix commits after `ea43f6a` against the six round 1 findings, plus a regression pass over FEAT-008 from `903ad32`. All three blocking findings are fixed. Backup and restore now refuse an item root with a symlink in its path and a Hermes home that is not a real directory of the hermes user. Restore stops before decrypting when it wrote no kill flag or a unit did not stop. `ossbot-resume --paused-only` starts no unit. The three NITs are fixed as suggested. Re-run and passing: the lint gate, shellcheck, `test_backup_roundtrip.sh` (45/45, including the new workers-down case), `test_lifecycle.sh` and gitleaks. The hermetic bar also passed, apart from one load-sensitive FEAT-006 runner test that passes when re-run.

Watch for: one narrow residue of F008-R1-1. SQLite files are still opened by path. A compromised hermes uid that swaps `.hermes` and swaps it back while the online backup runs can get another root-readable SQLite DB into the archive under a Hermes name. A later restore of that archive then hands the DB to the hermes uid (likely; NIT, it needs a won race plus a restore of that exact archive).

**Verdict**: APPROVED (0 HIGH, 0 MEDIUM, 1 NIT)

## High-level view

The symlink defence has three layers. `path_problem` refuses any item root whose `realpath` differs from the path. `Layout.hermes_home_problem` requires a real directory owned by `hermes`. `_open_exact` checks each non-SQLite file's fd against the path. Restore runs `check_targets` once the units are stopped and again at the start of `install`, and nothing moves on a refusal. With the hermes uid stopped and every root moved aside whole, no deeper symlink survives into the write phase. The one gap is the SQLite branch of the backup copy: `sqlite3.connect` reopens the file by path after the fd check.

When restore fails closed, it restarts the gatekeeper and proxy and exits 1 before `decrypt`. A paused-only resume during a workers down now leaves runner, Hermes and sandboxd stopped, and the live roundtrip proves it. The lifecycle helper renames the request into the root-only `/var/lib/ossbot/lifecycle` before any check or read. That makes "always consumed" a single rename.

<details>
<summary>Issues (1)</summary>

1. **F008-R2-1 (NIT, likely) SQLite copies in the backup still open by path.** `_copy_file` checks the SQLite magic through `_open_exact`, then `sqlite3.connect(f"file:{src}?mode=ro")` reopens by path, and `_refuse_moved(src)` checks only afterwards. A hermes uid that swaps `.hermes` for a symlink and back while the copy runs (inotify on its own home sees root's open) can make root copy another SQLite DB, such as the gatekeeper DB. The archive files it under a `hermes:` name with the hermes owner, and a later restore installs it readable by hermes. Non-SQLite files have a similar, smaller gap: `_meta` takes owner and mode from an `lstat` by path after the copy. Fix: once sqlite has opened the file, check that the main-DB fd it opened (the new `/proc/self/fd` entry) has the dev/ino of the `_open_exact` fd, or take the dev/ino of the opened file before and after and compare them. Take `_meta` from `fstat` of the verified fd. Optionally, have restore force `hermes:` entries to the hermes owner. Files: `src/ossbot/ctl/backup.py`, `src/ossbot/ctl/restore.py`, `tests/unit/ctl/test_backup.py`.

</details>

<details>
<summary>Details</summary>

### Edges of the symlink and fail-closed fixes

For F008-R1-1, the `realpath` test covers more than the Hermes home. It also checks the `imports/<cid>` and `artifacts` roots under other service uids, at backup and at restore. `check_targets` refuses a missing Hermes home and asks for provisioning first, so a fresh-machine restore never creates `.hermes` as root. The hermes uid can block a restore by planting a symlink. That is fail-closed, and the error names the path.

For F008-R1-3, a non-zero `kill_exit` with the flag present (for example a PARTIAL container kill) still continues. That matches the DESIGN §18.8 note: the flag must exist and the units must be stopped.

### SQLite copy race (F008-R2-1)

```
root backup                                   hermes uid (inotify on ~/.hermes)
_open_exact(.hermes/x.db) -> magic ok   ---> IN_OPEN/IN_CLOSE seen
                                              mv .hermes .h; ln -s /var/lib/ossbot/<gk dir> .hermes
sqlite3.connect(".hermes/x.db") -> opens <gk dir>/x.db   (x.db named after a root-only DB)
source.backup(target)
                                              rm .hermes; mv .h .hermes
_refuse_moved(.hermes/x.db) -> ok; hermes_identity() unchanged (same inode came back)
_meta(lstat(.hermes/x.db)) -> hermes owner recorded
```

The archive is still encrypted to the owner, so the backup alone leaks nothing to hermes. The leak comes only when the owner restores that archive: `install` writes the copied DB into `/home/hermes/.hermes/x.db`, owned by hermes. Only SQLite files can be read this way, because the magic check runs on the real file and sqlite must accept the target. The gatekeeper DB (chat turns, approvals, ui_tokens, audit) is the realistic target. The precondition is the same as R1-1: code execution as hermes, which ADR-0018's tool allowlist makes unlikely. That, plus the race, puts it at NIT rather than MEDIUM. I did not run a live probe of this race. The confidence comes from the code order in `_copy_file` and `Collector._file`.

### Test coverage

New unit tests cover a symlinked Hermes home at backup and at restore, a Hermes home with another owner, a missing home, symlinked `imports`/`artifacts` parents, a file opened through a swapped directory (plain and SQLite, swapped before the open) and restore with a failed kill or stop. The roundtrip system test gained the workers-down backup case.

Not tested: a swap during the SQLite online copy (F008-R2-1). `test_bootstrap_zero.ps1` is still not run (it needs a throwaway distro; the findings say so).

</details>

## What I verified and how

- Scope: `git diff ea43f6a..HEAD` (5 commits, 14 files) read against the six round 1 findings, then the FEAT-008 diff from `903ad32` again for regressions. DESIGN §18.8 steps 1 and 5 and the ADR-0016 addendum describe the new refusals, and none of them goes beyond revision 5. Round 1 had already checked items 39 to 41 (status fields, mutex, 6a/6b in PowerShell 5.1, non-elevated, ADR-0021 with the SpiralPool credit, `-Uninstall` scope). None of those files changed since.
- Hermetic bar: `DEV pytest -m "unit or telegram or acp or e2e" -n auto` gave 2943 passed and 1 failed in 673 s. The machine was under heavy load. The failure was `tests/unit/runner/test_rescope_dispatch.py::test_dispatch_sees_old_items_behind_many_terminal_ones`, a FEAT-006 test that FEAT-008 does not touch (`src/ossbot/runner` and `tests/unit/runner` have no FEAT-008 changes). The file passed alone (11/11), and so did `tests/unit/runner -n auto` (150/150). That gives 2944 total, matching the recorded 2944 passed. It is not a FEAT-008 finding; the Phase 2 gate may want to look at its load sensitivity.
- Lint gate: ruff check clean; `ruff format --check` 342 files; mypy 169 files clean; lint-imports 6 contracts kept. `ROOT shellcheck -x` clean on `ossbot-lifecycle`, `ossbot-resume`, `test_backup_roundtrip.sh` and `test_crash_safety.sh`.
- Deployment: `/opt/ossbot/app/src` is identical to the repo's `src` (`diff -rq -x __pycache__`). The deployed `ossbot-lifecycle` and `ossbot-resume` match the repo. All six units were active before the system tests.
- Live: `ROOT bash tests/system/test_backup_roundtrip.sh` passed all 45 checks (workers-down backup, tamper refusal, restore killed + dry-run ON, no open approval in the three decision services, audit verify, `+a` anchors, identical Hermes memories and SOUL.md, resume). It used the throwaway identity, and the moved-aside state is in `/srv/ossbot/restore-previous-20261019-171416` by design. `ROOT bash tests/system/test_lifecycle.sh` passed every check (all refusals consumed, symlink target untouched, up refused while killed, root and 409). Afterwards `/var/lib/ossbot/lifecycle` was empty, `ossbot-lifecycle.path` was active and there was no kill, pause or lifecycle flag. I did not re-run `test_crash_safety.sh` (only skip turned into fail) or `test_shutdown_blocker.ps1` (unchanged since round 1, which re-ran it).
- Matrix: no TRACEABILITY change in the fix commits. Round 1's row check still holds (10 rows done; the 9 open rows name FEAT-009 item 47 or FEAT-011 items 54/57 and record the FEAT-008 part).
- Safety: dry-run ON after both suites. No GitHub write path changed. No secret file was read. `gitleaks git --redact --log-opts=--all` scanned 170 commits with no leaks. `backups/` and `.ossbot-local/` are untracked (my scratch script there is deleted). No UAC, and no host change outside the documented exceptions. I1 to I13 hold in the changed code. The round 1 fixes make restore fail closed in two more places.
- Git: all 12 FEAT-008 commits are by Amsal Khan <md.amsalkhan@gmail.com> as author and committer. `git status --porcelain` was empty before this review's files. The 5 fix commits are not on `origin/main` (`ea43f6a`, the owner's push pattern). This review pushed nothing.

<details>
<summary>File map (round 1 fixes)</summary>

- `src/ossbot/ctl/backup.py`: `path_problem`, `Layout.hermes_home_problem`/`hermes_identity`, `_open_exact`, the Hermes-home recheck after collection.
- `src/ossbot/ctl/restore.py`: `check_targets` (also the first step of `install`).
- `src/ossbot/ctl/cli.py`: restore refuses before decrypting on a missing kill flag or a failed stop, and runs `check_targets` after the stop.
- `scripts/bin/ossbot-resume`: `--paused-only` starts no unit.
- `scripts/bin/ossbot-lifecycle`: the request is renamed into root-only `/var/lib/ossbot/lifecycle` first.
- `tests/unit/ctl/{test_backup,test_restore,backup_helpers}.py`, `tests/system/{test_backup_roundtrip,test_crash_safety}.sh`: new cases.
- `docs/DESIGN.md` §18.8 notes, `docs/adr/0016-*` addendum, `docs/VERIFICATION.md`, `FEAT-008.json` findings.

Full diff: `git -C d:\Work\autonomous-oss-contributor diff ea43f6a..cfe0d63` (round 1 fixes) and `903ad32..cfe0d63` (FEAT-008).

</details>
