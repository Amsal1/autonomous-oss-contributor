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

## Implementation addendum (FEAT-002, 2026-10-17)
- Checked on docs.github.com (REST `repos/forks`): `POST /repos/{owner}/{repo}/forks` accepts body `name` (new name for the fork) and `default_branch_only`, answers 202 and forks asynchronously (wait up to 5 minutes). The gatekeeper sends `{name, default_branch_only: true}` and polls `GET /repos/<login>/<name>` every 3 s for up to 5 min. Docs now show `X-GitHub-Api-Version: 2026-03-10`; the gatekeeper pins `2022-11-28`, which stays supported for at least 24 months after its successor (re-check in FEAT-008).
- Gatekeeper reads (identity, live checks, reconciliation) and writes use the write token through proxy 3150 (`github_read.make_client`); only `gatekeeper.submit` can reach the write calls (import-linter, `unmatched_ignore_imports_alerting = error`).
- `GET /user` login and `X-OAuth-Scopes` (`public_repo` or `repo`) and the live fork/push/PR behaviour remain **pending live probe (needs GITHUB_WRITE_TOKEN)**; until then every submit is refused with `no_write_token` (PLAN Decision 5).

## Implementation addendum (FEAT-006 review round 2, 2026-10-18): DESIGN §23 items 17 and 18, read-only part
- Item 17, read-only part. The write token now exists. `ossctl doctor` (full) sends `GET /user` with it and checks the login and `X-OAuth-Scopes`: `public_repo` is present and `repo`, `workflow` and `admin:*` are absent (the result is in VERIFICATION "Phase 2 live checks"). Per the GitHub docs, `public_repo` limits a classic token to public repositories ([scopes for OAuth apps](https://docs.github.com/apps/building-oauth-apps/understanding-scopes-for-oauth-apps)), and scopes never add permissions beyond the user's own. Forking, pushing to the fork, opening a PR and commenting are all writes to public repositories. Content was rephrased for compliance with licensing restrictions.
- Item 17, write part (sufficiency in practice, fork creation latency, secondary rate limits) stays pending. It needs a live write, and the build makes none (hard rule 2). It is checked at the first owner-approved submit after the owner turns dry-run off, and recorded by FEAT-011 under "User action needed" (TRACEABILITY DES-V-17).
- Item 18 (the commit email is verified on `Amsal1` and not linked to `<work-login>`) cannot be read with either token, because GitHub's email endpoints need the `user:email` scope, which neither token has. It also has no product check yet. GitHub attributes a commit to the one account on which its email is verified, so the first PR's commit attribution answers it, as does the owner's Settings → Emails page. Pending, FEAT-011 "User action needed" (TRACEABILITY DES-V-18).
