# ADR-0014: Review integrity: gatekeeper-derived artifacts, mode-bound approvals, config snapshots, audit anchors
Status: Accepted (amended in design rev 2, rev 3 and rev 4) · Date: 2026-10-17 · Design: DESIGN.md §7.9, §9.11, §9.13, §12.4, §12.5, §12.7, §14.4

## Context
Design review H4, H6, M2, M4, M5, M12: in v1 the diff the human approved came from an attacker-writable sandbox volume; `base_sha` was not proven to be upstream history; dry-run state at approval time was not bound; title/body edits could be lost and submit could race package; security config was read from a Windows-writable mount; the audit chain could be recomputed by its own writer.

## Decision
- Per-contribution-rev bare mirror in the gatekeeper; fresh upstream fetch; require `base_sha` ancestor of upstream `base_ref`, exactly one commit, parent == base, identity == author == committer == sign-off. All git runs with no system/global config, `core.hooksPath=/dev/null`, `protocol.file.allow=never`, `--no-ext-diff --no-textconv`.
- The gatekeeper generates numstat, patch, commit message, and author lines itself; only these are shown (card, View diff, `.patch`). Sandbox renderings are compared; mismatch fails the contribution and pauses.
- Binding v2 adds `patch_sha256`, `commit_message_sha256`, `upstream_base_tip`, and `dry_run` recomputed from current state at Approve, Confirm, and execution. Any dry-run toggle or new config snapshot invalidates every open approval in the same transaction and re-issues cards. Confirm text states LIVE or DRY RUN.
- `/package` only in `PACKAGING`; submit works from one immutable hashed snapshot; human title/body overrides persist across revs.
- PR body and commit message are shown verbatim (escaped `<pre>`); Approve appears only after every part is delivered.
- Policy, targets, egress, profile load only at start or on a human-confirmed reload into an ext4 snapshot; a hard-coded floor for workflow paths and monotonic DB floors for forbidden logins and confidentiality strings (ADR-0015), so a reload can never silently weaken them.
- Root watchdog anchors the audit head into a `chattr +a` root-owned file every 5 minutes and on outbound writes; `audit verify` checks anchors.

## Revision 2 amendments (design review round 2, M9/M10/M12/M13/N3/N4)
- `upstream_base_tip` in the binding is the value stored at `/package` and is never refreshed. Confirm and submit fetch into `refs/upstream-live/*` only for the ancestry check, so upstream moving on never supersedes an approval.
- Mirrors:
  - A per-upstream cache bare repo holds only single-branch `refs/upstream/*` and is fetched incrementally.
  - Per-rev mirrors use `objects/info/alternates` and contain only their own refs, so the fabricated-base check is unchanged.
  - Superseded and terminal mirrors are deleted.
  - Backups exclude cache, mirrors and derived artifacts.
- `push_update`:
  - The gatekeeper chooses the kind at `/package` (iff `pr_number` is set).
  - Verification requires fork tip == `old_head_sha` == stored `head_sha`, exactly one commit on top, the same identity rules, and the incremental patch bound in the approval.
  - Submit is a plain fast-forward push followed by `SUBMITTED→WATCHING`. No force push in v1.
- Live checks and rate-limit counters are defined per action kind. Our own PR never counts as a competing PR.
- Title, body and comment text are normalized once at storage, so stored, hashed and posted bytes are identical.
- `/approve` re-send rotates message-bound tokens in one transaction.
- Approval cards exist for every public action kind (`comment`, `issue_claim`, `push_update`), with verbatim bytes and mode-stating confirm.
## Revision 3 amendments (design review round 3, M1/M3/M7)
- M1: the package job commits on the branch `ossbot-change` and bundles `refs/heads/ossbot-change ^<round_base>`. The gatekeeper accepts a bundle only if `git bundle list-heads` is exactly that one ref, and fetches it to `refs/ossbot/r<rev>`. A detached-HEAD bundle advertises only `HEAD`, which a `refs/heads/*` fetch ignores.
- M7: one fixed format-patch command (`ossbot.common.gitcmd.FORMAT_PATCH`: `--no-signature --no-stat --full-index`, pinned diff config, no external diff or textconv) is used by both sides. The tamper check compares `git patch-id --stable` and the numstat set, not raw bytes, so a git-version difference is not an anomaly. `patch_sha256` hashes the gatekeeper's own output of that command.
- M3: operator imports upload bundle bytes; the gatekeeper never opens a caller-named path. Before a contribution exists, it requires a single `refs/heads/*` head, prerequisites == `{base_sha}`, `base_sha` in upstream history, and the head descending from it. The imported change then goes through fetch, `unbundle`, validate, review, and the same package re-authoring and derived preview as every other change.

## Revision 4 amendments (design review round 4, R4-M1/R4-N1/R4-N6)
- R4-M1: git applies the `file` transport check to bundle paths, so a bundle fetch under `protocol.file.allow=never` fails (verified on git 2.55). `bundle verify` and `list-heads` are unaffected.
  - The gatekeeper fetches bundles only with `BUNDLE_FETCH_ARGS = GIT_ARGS + ["-c", "protocol.file.allow=always"]`, in two places: the `/package` fetch and the import-validation fetch. Both use a path the gatekeeper built itself.
  - `unbundle.sh` adds the same scoped override to its one fetch.
  - Every other git call keeps `never`. Tests run the verifier with the production args.
- R4-N1: the unbundle job requires a single head equal to the mode's `bundle_ref`: `refs/heads/ossbot-change` for fix rounds, and the gatekeeper-stored `import_head_ref` for imports.
- R4-N6: numstat uses one fixed command, `ossbot.common.gitcmd.NUMSTAT`, with the same pinned diff config as `FORMAT_PATCH` (`diff.algorithm=myers`, `diff.renames=false`, `core.quotePath=true`). Both sides run it, so the numstat-set comparison does not depend on built-in defaults.

## Consequences
More gatekeeper code (git plumbing, derivation) inside the TCB, covered by bare-repo unit tests including hostile git config and fabricated bases.

## Implementation addendum (FEAT-002, 2026-10-17): DESIGN §23 item 19 on git 2.43.0
Live probe in the `ossbot` distro (Ubuntu 24.04 `git 2.43.0`, the gatekeeper's git and the `sandbox-full` base image's git; both are the noble package), with `GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null` and the hardened `-c` flags:
- `git commit --no-verify -F <file> --trailer "Signed-off-by: N <E>"` works; no `interpret-trailers` fallback is needed. `git interpret-trailers --parse` on `git log -1 --format=%B` prints exactly the `Signed-off-by:` line.
- `git bundle create <f> refs/heads/ossbot-change ^<base>` writes a v2 bundle whose header is one prerequisite line `-<base> <subject>` and one ref line; `git bundle list-heads` prints exactly `<head> refs/heads/ossbot-change`. A bundle created from a detached HEAD lists only `<sha> HEAD` (M1 confirmed).
- `bundle list-heads` and `bundle verify` work under `protocol.file.allow=never`; `verify` in a repo without the prerequisite fails with "Repository lacks these prerequisite commits".
- A bundle fetch under `protocol.file.allow=never` fails with `fatal: transport 'file' not allowed`; the scoped `-c protocol.file.allow=always` placed after the `never` (last `-c` wins) succeeds, together with `transfer.fsckObjects=true` and `--no-recurse-submodules` (R4-M1 re-confirmed on 2.43).
- R5-N1 confirmed: with the scoped `always`, `git fetch <directory>` fetches from that directory as a local repository. The gatekeeper therefore copies the bundle (opened component by component with `O_NOFOLLOW`, `S_ISREG`, at most 50 MB) into `/srv/ossbot/gatekeeper/work/<cid>-r<rev>.bundle` (0600), hashes the copy and runs `list-heads`/`verify`/`fetch` only on it.
- Every flag of `FORMAT_PATCH` and `NUMSTAT` is accepted. `--no-signature` drops the `-- \n2.43.0` trailer that plain `format-patch` emits. With `diff.renames=false` a rename shows as a delete plus an add in numstat.
- `git patch-id --stable` reads a patch from stdin with the working directory outside any repository.
- Fetching the round base from `/work/repo/.git` by SHA with the scoped `always` works (the SHA is advertised as HEAD or `refs/ossbot/prev/r<rev>`).
- The sandbox image and the gatekeeper run the same git package today; `test_patch_compare_tolerates_git_version` covers a future version skew (signature and trailing bytes only).

Entrypoint interface decided here (used by sandboxd in FEAT-003): `package.sh --round-base <sha> --package-ts <iso> --name <name> --email <email>` and `unbundle.sh --mode fix_round|import --bundle-ref <ref> --checkout-sha <sha> --rev <n>`. Both read `/io/input`, write `/io/output/result.json` (`{"ok": bool, "error_class": str|null, ...}`) and exit non-zero on failure. `OSSBOT_FORMAT_PATCH`/`OSSBOT_NUMSTAT` must be set in the environment (baked into the image from `ossbot.common.gitcmd`); `OSSBOT_WORK`, `OSSBOT_IO`, `OSSBOT_PKG_ROOT` and `OSSBOT_NUMSTAT2JSON` only relocate the fixed paths for tests.

Submission details decided here: the push_update fork-tip re-check at submit and the reconciliation read use `GET /repos/<fork>/git/ref/heads/<branch>` (a read) instead of a git fetch; a missing `GITHUB_WRITE_TOKEN` refuses the submit before step 0 (approval invalidated `no_write_token`, nothing consumed, contribution unchanged); the push uses `https://<login>@github.com/<fork>.git` and an askpass helper that reads the token from an inherited memfd, so the token never appears in argv, URL, environment or logs.
