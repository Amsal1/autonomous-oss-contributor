# ADR-0006: GitHub auth, fork-and-PR flow, and commit identity
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §2, §9.11, §12.4-12.6, §18.3

## Context
GitHub documents that fine-grained PATs cannot contribute to public repos where the user is not a member. The work account `@<work-login>` must never be used. User decision (2026-10-17): every upstream commit uses author == committer == `Signed-off-by` == `Amsal Khan <md.amsalkhan@gmail.com>`; the GitHub login stays `Amsal1`. DCO checkers compare the sign-off with the author, so these are separate concepts from the API identity.

## Decision
- Identity fields in the gitignored `config/profile.yaml` (ADR-0015): `github_login` (owner: `Amsal1`), `commit_name` (`Amsal Khan`), `commit_email` (`md.amsalkhan@gmail.com`), `signoff: true`, `forbidden_logins` (`<work-login>`, kept in a monotonic DB floor). Loaded only through the config snapshot.
- Write: classic PAT on `Amsal1` with only `public_repo` (no `workflow`; workflow edits are forbidden by a hard-coded floor), held only by the gatekeeper and used only through proxy port 3150.
- Read: fine-grained PAT on `Amsal1`, "Public repositories (read-only)", held by the runner. Sandboxes clone anonymously.
- Commits are created by the deterministic package job in a fresh repo with the identity above and `Signed-off-by: Amsal Khan <md.amsalkhan@gmail.com>`. The gatekeeper verifies author == committer == last sign-off == identity, single commit on an upstream base, before showing anything.
- Flow: reuse an `Amsal1` fork whose parent is the upstream (langfuse and n8n forks exist), else create one; push the approved SHA to `ossbot/<issue>-<slug>-<cid6>`; open the PR with `maintainer_can_modify: true`. Token login must equal `Amsal1` before any write; `<work-login>` triggers kill.
- This harness repo uses repo-local git config with the same name/email and normal commits (no `GIT_AUTHOR_*`/`GIT_COMMITTER_*` overrides). A future private remote for it uses a separate credential, never the gatekeeper PAT.
- Repos requiring signed commits are skipped in v1.

## Consequences
Classic PAT is broad across Amsal1's public repos; mitigated by the gatekeeper being the only holder, proxy-limited egress, and policy. **Verify:** `md.amsalkhan@gmail.com` is verified on `Amsal1` and not linked to `<work-login>`; `X-OAuth-Scopes`; fine-grained PAT limitation still current.
