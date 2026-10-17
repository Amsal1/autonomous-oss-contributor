# ADR-0006: GitHub auth, fork-and-PR flow, and commit identity

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §12.4-12.6, §18.3

## Context
GitHub documents that fine-grained PATs cannot contribute to public repos where the user is not a member. The work account `@<work-login>` must never be used. User decision (2026-10-17): identity `Amsal1` / `md.amsalkhan@gmail.com` for this repo and all upstream commits, DCO sign-off with the same.

## Decision
- Write: classic PAT on `Amsal1` with only `public_repo` (no `workflow`; workflow-file changes are forbidden by policy), held only by the gatekeeper.
- Read: fine-grained PAT on `Amsal1`, "Public repositories (read-only)", held by the runner for API reads (5000 req/h). Sandboxes clone anonymously and hold no GitHub token.
- Flow: reuse an existing `Amsal1` fork if `parent == upstream` (langfuse and n8n forks already exist), else create one; push the approved SHA to `ossbot/<issue>-<slug>-<cid6>`; open the PR upstream with `maintainer_can_modify: true`.
- Commits are created by a deterministic package job with the fixed identity and `Signed-off-by: Amsal1 <md.amsalkhan@gmail.com>`; the gatekeeper re-verifies every commit. Token login must equal `Amsal1` before any write; `<work-login>` triggers kill.
- Repos requiring signed commits are skipped in v1.

## Consequences
Classic PAT is broad across Amsal1's public repos; mitigated by the gatekeeper being the only holder and by policy. **Verify:** `md.amsalkhan@gmail.com` is verified on `Amsal1`; scopes header; some DCO projects may expect a real name (identity is config-only to change).
