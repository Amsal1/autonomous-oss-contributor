# ADR-0014: Review integrity: gatekeeper-derived artifacts, mode-bound approvals, config snapshots, audit anchors
Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §7.9, §12.4, §12.5, §12.7, §14.4

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

## Consequences
More gatekeeper code (git plumbing, derivation) inside the TCB, covered by bare-repo unit tests including hostile git config and fabricated bases.
