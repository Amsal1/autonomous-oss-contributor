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
- GitHub MCP inside a scout job: a research-profile scout made 9 successful `@github/...` read calls.
- Hermetic suites: 3521 passed at f753cb5 (`-n auto`); the owner's GitHub CI run's two failures were
  runner artifacts and are fixed; CI is now manual.
- Windows restart (CR-001 6a/6b): not run during the build (a restart would stop the build itself);
  an earlier real shutdown showed 6a does not drain yet. See "Restart test".

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
- **Owner live checks O1–O8 and the restart test R1** below: their TRACEABILITY rows are `deferred`
  ("owner live check after build") until you run them.

## Owner live checks

Each check names its TRACEABILITY rows, the steps and the expected result. The code paths are already
covered by hermetic tests; these confirm the real Bot API, your Telegram clients or a first live write.
Record each one when done (no secret or personal value in the text):

```powershell
powershell -NoProfile -File scripts\dev\record-owner-check.ps1 -Check O1 -Rows CR3-V-4 -Result "what you saw"
```

It sets the rows to `done` (or `-Status missing` if the result was wrong), appends the result to their
evidence, recomputes the Totals line and adds a row to VERIFICATION "Owner live checks (after the
build)". Commit `docs/TRACEABILITY.md` and `docs/VERIFICATION.md` afterwards.

**O1 Topics** (CR3-V-4, part of DES-V-32). Steps: turn topics on in @BotFather (SETUP "Telegram
topics"), then `scripts\bootstrap.ps1 -Only provision` or restart the gatekeeper, then `/start`.
Expected: 🚦 Control, ✅ Reviews, 📈 OSS feed and 💬 Assistant appear, the dashboard is pinned in
Control. Note whether a "General" topic exists and whether the pin shows only in Control.

**O2 Rich views on each client** (CR3-V-1, CR3-V-14, CR3-V-15, CR3-V-16). Steps: on Android, iOS and
desktop, open a preview card's explorer and a file diff (`/review`), quote-reply one line of a rich
file view, ask Hermes for an answer with a table and a code block in a topic and in flat mode, and look
at a 2-button row with long labels. Expected: `details`, tables, `pre language=diff` and anchors
render; a quote-reply on a rich file view becomes a line comment (if not, note the client); labels are
cut with `…`, never wrapped into each other; an answer that cannot nest a block falls back to classic.

**O3 Streaming and Stop** (CR3-V-2, CR3-V-3). Steps: ask Hermes a long question in a topic, then ask
another and tap ⏹ Stop after a few lines. Expected: a draft appears within 1 s and updates at most once
a second without "Too Many Requests" in `journalctl -u ossbot-gatekeeper`; Stop ends the stream within
2 s with "⏹ Stopped by you" and the partial text ending in `…`.

**O4 Small rendering items** (CR3-V-5, CR3-V-6, CR3-V-12). Steps: wait for a 🎉 message (merged PR or
`/digest`), open an `.html` diff document from a preview card, and look at a classic message that
mentions a command (for example the `/approve` hint). Expected: the effect plays (or the message just
arrives, no error); the `.html` opens in a browser or viewer; the command text is not a tappable link.

**O5 Hermes API** (CR3-V-7). Steps: during O3, note whether Stop also ended the Hermes run
(`journalctl -u ossbot-hermes` shows the run cancelled). Expected: no Hermes run keeps going after Stop.

**O6 Photos** (CR2-7l, CR3-IH-4, CR3-V-8, CR3-V-13). Steps: send a food photo (compressed), then an album
of 2–3 photos, then a photo sent as a file. Expected: a 🤖 answer with a protein estimate; the album
gets one answer; the answer never quotes location or camera data. Voice notes answer "Voice isn't
supported yet".

**O7 Opt-in mirror and Telegraph** (CR3-V-9, CR3-V-11). Only if you want them: `ossctl review-mirror
enable` with a private repo of yours, and `ossctl telegraph setup` then Publish on a long answer.
Expected: enable passes its probes; the article opens as Instant View; Unpublish blanks it.

**O8 First live submit** (DES-V-17, DES-V-18). Steps: verify the commit email in GitHub Settings →
Emails, `/dryrun off` when you trust the previews, approve one PR. Expected: fork, push and PR succeed
with the classic `public_repo` token; the PR's commit shows your account as author; note how long the
fork took.

## Restart test

**Real-shutdown finding (2026-10-20, before this test).** Windows was shut down or restarted at
03:08 UTC with ossbot running. `.ossbot-local\shutdown-blocker.log` shows the blocker (6a) got
`WM_QUERYENDSESSION` and created the reason "ossbot: stopping safely…", but its `down.ps1` child exited
at once with `-1073741502` (`0xC0000142`, a process that cannot initialise because the session is
ending). The blocker then destroyed the reason and let the session end. The event task (6b) ran and
ended `0xC000026B` (the same cause at logoff). The next start reported `last shutdown unclean`; crash
safety held (no lost or duplicated work). So on a real shutdown 6a does not drain yet. This needs a
fix in FEAT-008's blocker (for example a helper process started at logon that already holds the
`wsl.exe` session and only waits for the signal, instead of starting `powershell.exe` from the
handler) and then this test. The blocker that ran had been started by the 2026-10-19 simulated test,
not by the logon task; a logon-started blocker may behave differently, which R1 also answers.

**R1 Restart** (CR1-6a, CR1-6b, DES-V-31, DES-AC-32). Steps:
1. With ossbot up (`scripts\status.ps1`: `shutdown blocker running`, `shutdown hook Ready`), save your
   work and choose Start → Power → **Restart**. Do it when no ossbot build is running on the laptop.
2. Note: (a) whether the blocking screen lists "ossbot: stopping safely…"; (b) whether the restart
   continues on its own, without "Restart anyway"; (c) about how long it waited.
3. After logging in again, wait 2 minutes, then run
   `powershell -NoProfile -File scripts\dev\restart-evidence.ps1`. It prints the tails of
   `.ossbot-local\shutdown-blocker.log` and `lifecycle.log`, the hook task's last result and
   `status.ps1`'s `last shutdown`.
4. Repeat once with **Shut down** (Fast Startup is on here).

Expected: (a) yes; (b) yes; (c) up to about 90 s; the blocker log shows `down.ps1 exit=0`,
`lifecycle.log` a `down kind=graceful caller=blocker` with `end exit=0`, and `last shutdown graceful`
(or `hook` if 6b won the race). Record it:

```powershell
powershell -NoProfile -File scripts\dev\record-owner-check.ps1 -Check R1 -Rows CR1-6a,CR1-6b,DES-V-31,DES-AC-32 -Result "restart: (a) yes (b) yes (c) 15 s; last shutdown graceful; shut down: ..."
```

If the result matches the finding above (an immediate `down.ps1` exit code and `last shutdown
unclean`), record it with `-Status missing` and open the 6a fix.

## Owner decisions pending

- Reverse image search provider (SerpAPI, Bing Visual Search, Google Lens, ...): the key hook and a
  skill stub exist; nothing is wired until a provider is chosen (DESIGN §15.6, TRACEABILITY CR3-IH-11).
- Whether ossbot should come back up by itself when a Windows shutdown is cancelled after it went
  down (ADR-0021: today it stays down until `scripts\up.ps1` or the next logon).
- The 6a fix for the real-shutdown finding ("Restart test"): it changes FEAT-008's blocker and needs a
  real restart to verify, so it is a follow-up for you to schedule, not part of this build.
- The license (README "License: TBD"), before making the repository public.
