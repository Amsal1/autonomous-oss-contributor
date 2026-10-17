# ADR-0014: Review integrity: gatekeeper-derived artifacts, mode-bound approvals, config snapshots, audit anchors
Status: Accepted (amended in design rev 2 and rev 3) · Date: 2026-10-17 · Design: DESIGN.md §7.9, §9.11, §9.13, §12.4, §12.5, §12.7, §14.4

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

## Consequences
More gatekeeper code (git plumbing, derivation) inside the TCB, covered by bare-repo unit tests including hostile git config and fabricated bases.
