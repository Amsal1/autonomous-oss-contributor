# ossbot v1: final report

Written by FEAT-011 (Stage 4, PLAN item 57). Evidence for every line is in `docs/VERIFICATION.md`
(newest first); requirement status is in `docs/TRACEABILITY.md`.

## Summary

ossbot v1 is built and deployed on this laptop in dry-run: the safety core (gatekeeper, two-tap
approvals bound to content and mode, policy and hard ceiling, audit with root anchors, kill/pause and
the boot gate, Docker filter, per-uid firewall and squid egress, split coder, safety-probe gates), the
pipeline with scout and both gates, user-directed work and the size budget, Kiro headless and the V3
ACP fork, Hermes with capability tiers and the gated skill flow, the global model switch, the graceful
lifecycle with the Windows shutdown hooks and crash safety, encrypted backup and restore, the Telegram
workspace (topics, dashboard, review flow, streaming assistant, notifications, digest, status card,
opt-in review mirror and Telegraph), and the open-source docs, hygiene hooks and a manual CI workflow.
Dry-run has stayed ON for the whole build and nothing was ever written to GitHub.

## Doctor (2026-10-20, at 352962b after the redeploy)

- `ossctl doctor --probes` (root, in the distro): PASS 46, no FAIL, WARN or SKIP.
- `scripts\ossctl.ps1 doctor` (Windows): PASS 45.
- Notable checks: safety probes passed for the current tuple; Hermes commit, tier `self_improve`, tool
  surface and Hermes-side Kiro probe; GitHub MCP read (`get_me` = the owner login); Telegram `getMe`
  200 and user id set; the gatekeeper running with dry-run ON.

## Live checks in FEAT-011

- AC 17: the pre-commit hook refused a fake `ghp_` token and an owner value in a throwaway clone.
- gitleaks over the whole history with `.gitleaks.toml`: no leaks.
- `ossctl lint personal`: no owner value in a tracked file outside `docs/`.
- GitHub MCP inside a scout job: see "GHMCP-2" in VERIFICATION FEAT-011.
- Windows restart (CR-001 6a/6b): see "Real restart" in VERIFICATION FEAT-011.

## User action needed

Key and account problems found by the live checks are listed here and never worked around. No
credential problem was found: the bot token, both Kiro keys, the read token (also for the GitHub MCP
server) and the write token's identity check all pass doctor.

- **Topics.** The gatekeeper logs `telegram flat mode: topic mode is off in @BotFather`. Turn topics
  on (and allow users to create topics) in @BotFather to get the Control, Reviews, OSS feed and
  Assistant topics (SETUP.md "Telegram topics").
- **Unknown Telegram user.** The gatekeeper rejected updates from a Telegram user that is not the
  owner (logged as `telegram_unauthorized`, nothing answered). Nothing to do unless that was you on a
  second account.
- **Before `/dryrun off`.** The first owner-approved live submit is the first real fork, push and PR:
  check the commit attribution of that PR and that the commit email is verified on the GitHub account
  (DESIGN §23 items 17 and 18; not checkable read-only with either token).
- **Owner-only live checks** (Telegram clients and the real Bot API; the code paths are covered by
  hermetic tests): rich-message rendering and quote-replies, draft rate and drafts in topics, the real
  `stopped_message_generation`, topic behaviour, message effect ids, `.html` documents, the U+2060
  command marker, label truncation, rich/classic edit switching, nested rich blocks, EXIF on compressed
  photos, a food photo and an album through Telegram (TRACEABILITY CR3-V-*, CR2-7l, CR3-IH-4).
- **Opt-in features.** The review mirror and Telegraph stay off until you enable them
  (`ossctl review-mirror enable`, `ossctl telegraph setup`); their first live write answers the
  remaining mirror and Telegraph questions (CR3-V-9, CR3-V-11).

## Owner decisions pending

- Reverse image search provider (SerpAPI, Bing Visual Search, Google Lens, ...): the key hook and a
  skill stub exist; nothing is wired until a provider is chosen (DESIGN §15.6, TRACEABILITY CR3-IH-11).
- Whether ossbot should come back up by itself when a Windows shutdown is cancelled after it went
  down (ADR-0021: today it stays down until `scripts\up.ps1` or the next logon).
- The license (README "License: TBD"), before making the repository public.
