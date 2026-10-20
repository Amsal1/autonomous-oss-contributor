# ossbot v1: final report

Phase 2 is finished. The Phase 2 gate (round 1, `docs/final-gate.md` and `docs/final-gate.json`) is
APPROVED at 6391fae with no HIGH or MEDIUM findings. Evidence for every line here is in
`docs/VERIFICATION.md` (newest first) and requirement status in `docs/TRACEABILITY.md`. Dry-run stayed
ON for the whole build and nothing was ever written to GitHub.

## What was built

ossbot v1 is deployed on this laptop in the `ossbot` distro. Each feature passed its own build review
(`docs/review-build-FEAT-*.md`) before the gate.

| Feature | What it delivered | Build review |
|---|---|---|
| FEAT-005 | Phase 1 safety core brought to DESIGN rev 5: the gatekeeper, two-tap approvals bound to content and mode, policy and hard ceiling, hash-chained audit with root anchors, kill/pause and boot gate, Docker filter, per-uid firewall, squid egress with the `allowlist`/`research`/`open` modes, split coder, safety-probe gates | APPROVED, round 2 |
| FEAT-006 | Pipeline backend: runner, scout and gate 1 proposals through to submit and watch; user-directed `/work`; size classes and the budget; the global model switch (`config/models.yaml`) | APPROVED, round 5 |
| FEAT-007 | Kiro V3 ACP fork of hermes-kiro-provider (vendored), Hermes v0.22.2 with capability tiers and the gated skill flow, assistant backend | APPROVED, round 4 |
| FEAT-008 | Graceful lifecycle (`up`/`draining`/`down`), Windows autostart, shutdown blocker (6a) and event task (6b), crash safety, encrypted backup and restore | APPROVED, round 2 |
| FEAT-009 | Telegram workspace: UI kit, Bot API 10.3 adapter, topics, pinned dashboard, PR review surface, gate-1 cards, `/work`, `/size`, `/filters`, controls, notifications | APPROVED, round 4 |
| FEAT-010 | Streaming assistant with Stop, rich views with classic fallback, status card, reminders, digest, opt-in private review mirror and Telegraph (both off by default) | APPROVED, round 2 |
| FEAT-011 | User and harness docs, `ossctl docs commands`, `ossctl lint personal`, gitleaks pre-commit hook, manual CI workflow, final verification | APPROVED, round 1 |

### Verification at the gate (2026-10-20, VERIFICATION "Phase 2 gate (round 1)")

| Check | SHA | Result |
|---|---|---|
| Hermetic suites (`unit or telegram or acp or e2e`, `-n auto`) | 43dd6fb | 3529 passed in 48 s |
| Sandbox and Docker e2e (root) | 43dd6fb | 19 passed |
| Lint gate (ruff, format, mypy, lint-imports) after the NIT fixes | 7d6d7e3 | clean; mypy 240 files; 11 contracts kept |
| Tests affected by the NIT fixes (ctl, runner, CI workflow) | 7d6d7e3 | 257 passed (after fixing 22 test-side failures) |
| shellcheck and PSScriptAnalyzer 1.25.0 | 7d6d7e3 | clean, 0 errors |
| System: egress surface, crash safety, lifecycle, no exposure | 7d6d7e3 | PASS 241, 20, 47, 20 |
| System: kill | 7d6d7e3, then 557b9d1 | cases 1-5 PASS; case 6 found a real defect (resume did not re-enable autostart), fixed in 557b9d1, re-run green |
| Doctor `--probes` and Windows doctor | 557b9d1 | PASS 46 and PASS 45, no FAIL, WARN or SKIP |
| gitleaks over the history | 557b9d1 | 209 commits, no leaks |
| `ossctl lint personal --staged` | 7d6d7e3 | 804 files, 0 hits |
| Traceability matrix | 6391fae | 522 rows: 469 done, 53 deferred, 0 missing or rework |

Only the tests affected by the NIT fixes re-ran after 43dd6fb. The gate reviewer checked that no other
test calls the changed functions and that the new parameters default to the old behaviour.

## Live checks

All of these ran against the real services, in dry-run, with no GitHub write:

- Redeploy (`scripts\bootstrap.ps1 -Only provision`): 13 s end to end with `images: unchanged`. A
  redeploy that does not rebuild images is restarts only. The slow redeploys earlier in the build were
  the image rebuilds.
- GitHub MCP in a container (GHMCP-2, 29c305c): research-profile scout `scout-54e27f891fe8` made 9
  completed `@github/...` read calls through the Go binary with the configured read secret. At the
  gate, doctor ran the same binary and secret again (`get_me` returned the owner login, a public repo
  root listed). The server exposes no write tool.
- Safety probes passed for the deployed CLI/image/agent/settings tuple (doctor).
- Telegram `getMe` 200; kill and lifecycle FYIs reached only the configured user.
- Pre-commit hook (AC 17, in a throwaway clone): refused a fake `ghp_` token and an owner value,
  allowed a clean commit.
- Earlier phases (VERIFICATION, per feature): live food-photo answer through Hermes, Kiro V3 ACP
  round-trips against kiro-cli 2.32.0, `ossctl targets verify` 73 PASS, the simulated shutdown blocker
  and the 6b task.

## Deferred items for your approval

The 53 deferred rows each carry their reason in TRACEABILITY. Grouped:

- **Out of v1 scope by DESIGN §6.1** (DES-DEF-1…13, ADR-0012, DES-V-7, DES-V-22): stack validation,
  single-container coder, dual Telegram mode, proactive no-issue discovery, `sandbox-android`, backup
  timer on by default, Hermes headless fallback 2, `WslcRuntime`, Langfuse, Mini App, and the scout
  clone variant. In each case the schema accepts only the v1 value and a test checks it.
- **Owner-only live checks** (CR3-V-1…16 except V-10, DES-V-32, CR2-7l, CR3-IH-4, DES-V-17, DES-V-18).
  They need your Telegram clients, the real Bot API or a first live write. Steps are under "Owner live
  checks" (O1-O8).
- **Real Windows restart** (CR1-6a, CR1-6b, DES-V-31, DES-AC-32, PV1-A1). A restart would have stopped
  the build, and an earlier real shutdown showed v1's 6a did not drain (F011-N1). Post-v1 the drain
  goes through a channel opened at logon; the simulated test passes. Steps are under "Restart test"
  (R1). You decided on 2026-10-20 to defer it. The gate asks you to confirm that v1 ships with it
  open.
- **Your decisions**:
  - CR3-IH-2 / F010-N1: what Re-attempt later means on a closed-PR card.
  - F005-R2-3: whether to refuse `extra_patterns` wider than the redaction hold-back.
  - CR2-3f-research: a separate research job. Deferred because Hermes' own Kiro session already has
    web search.
  - CR3-IH-7 and CR3-V-10: notifications inbox. Needs a `notifications`-scope token.
  - CR3-IH-13: needs a Google OAuth secret.
  - CR3-IH-16: Mini App. Conflicts with the no-exposure rule.
- **Later features** (CR3-IH-5, 6, 8, 12, 14, 15, 17): listed in PLAN "Deferred".

Other decisions still open:

- Reverse image search provider: the key hook and skill stub exist, DES-DEF-12.
- Whether ossbot should come back up by itself after a cancelled Windows shutdown (ADR-0021: today it
  stays down until `scripts\up.ps1` or the next logon).

The license is decided (post-v1): Apache-2.0 with a NOTICE file that keeps the credit (README
"License").

## Day-to-day use

- **Up, down, status.** ossbot starts with Windows and stops with it.
  - By hand: `scripts\up.ps1`, `scripts\down.ps1`, `scripts\status.ps1`. Inside the distro the same is
    `ossctl up|down|status`.
  - On Telegram: `/up`, `/down`, `/drain`, `/undrain` and `/status`.
  - `down` drains running work (up to 120 s), re-queues the rest and never invalidates approvals.
  - `/pause` and `/kill` (or `scripts\kill.ps1`) are separate from lifecycle and survive a reboot. Only
    `/resume` or `scripts\resume.ps1` undoes them.
  - After pulling changes, redeploy with `scripts\bootstrap.ps1 -Only provision`.
- **`/work <issue, PR or repo URL> [notes]`**, or forward a GitHub link and tap 🛠 Work on this, or run
  `ossctl work <url>`. The item is marked 👤 and skips ranking, caps, filters and the size preference.
  It still passes both gates, the forbidden paths, the repo's AI policy, the CLA check, the
  one-open-PR-per-repo rule and the hard ceiling.
- **`/size`** sets the size preference for autonomous picks only (default maximum `s`, ≤ 80 non-test
  lines). Use `/repos <repo> size <class>` per repo and `/filters` for the kinds of work. The hard
  ceiling (80 files, 3000 code lines, 3000 test lines) changes only in `config/policy.yaml` plus a
  confirmed `/reload`.
- **`/model`** opens a picker that switches a production role's model, after a confirm tap. Changes
  are checked against the Kiro catalog, audited and announced. The reviewer must differ from the coder.
  `test` and `test_vision` change only in `config/models.yaml` or with `ossctl model set test …`.
  `ossctl model list` shows what each role resolves to.
- Reviewing: proposal cards (gate 1, Start work) and preview cards (gate 2, Approve then Confirm) arrive
  in ✅ Reviews, or in the one chat while Threaded mode is off. The mode label always says DRY RUN or LIVE.

## Turning dry-run off

Do it only when all of these hold:

- You have checked several DRY RUN previews and trust them.
- The commit email in your profile is verified in GitHub Settings → Emails (DES-V-18). Neither token
  can check this read-only.
- Doctor is all PASS.

`/dryrun off` asks for a confirm tap. Every dry-run toggle invalidates all open approvals, so approve
again afterwards in LIVE mode. `/dryrun on` takes effect at once.

The first live PR candidate is the V3 ACP adapter for
`tobenwarrior/hermes-kiro-provider` (CHAT-6). The target is in `config/targets.yaml` with
`discovery: false`. The PR text, including the AI disclosure, is in
`docs/upstream/hermes-kiro-provider-v3.md`.

1. Build the bundle: `bash scripts/dev/kiro-provider-bundle.sh` in the distro. It clones upstream at
   `vendor/hermes-kiro-provider/UPSTREAM_COMMIT`, commits with the identity from `git var
   GIT_AUTHOR_IDENT` plus Signed-off-by, and writes nothing to GitHub. It prints the import command.
2. Import with `ossctl contribution import --repo tobenwarrior/hermes-kiro-provider --base <sha>
   --from-bundle … --title-file … --body-file …`, as printed in step 1.
3. Pass gate 1, then go through gate 2 once in DRY RUN. The audit should record "would fork/push/open
   PR" with the exact payload.
4. Run `/dryrun off`, approve and confirm in LIVE. This is check O8: fork, push and PR with the
   `public_repo` token, and the commit shows your account as author. Record it with
   `record-owner-check.ps1 -Check O8`.
5. Turn dry-run back on with `/dryrun on` if you want every later PR to start in DRY RUN.

## Known limitations

- **Shutdown drain (6a).** v1 did not drain on a real Windows shutdown: its `down.ps1` child exited
  `0xC0000142` and the next start reported `last shutdown unclean`. Crash safety held, with no lost or
  duplicated work. Fixed in code post-v1: the blocker drains through a channel opened at logon
  (ADR-0021 addendum), and the simulated test passes. A real restart is still unproven (R1 pending).
  6b is retired.
- **Threaded mode and the pin.** Telegram runs in flat mode until Threaded mode is on in @BotFather.
  `pinChatMessage` cannot name a thread: the dashboard is pinned only once it is in 🚦 Control, and a
  client may still list that pin in "All" (O1, ADR-0020 addendum). The pinned dashboard is edited in
  place; `/status` replies are fresh copies and are not pinned.
- **Commands in All** (O1, PV1-A3, fixed and re-checked 2026-10-21). Typed in "All" with Threaded
  mode on, a command makes Telegram create a topic named from the text. The bot answers in All and
  deletes that topic within about 2 s (9884df9, ADR-0020 addendum post-v1 2026-10-21). In 🚦 Control,
  the other system topics and topics you named it answers in place. Client cache: the Telegram client
  may keep showing the deleted topic until you leave and reopen the chat; it is gone on the server.
  If a topic ever stays (no pair within 2 s, or Telegram refuses the delete), send commands in
  🚦 Control.
- **Telegram on real clients.** Rich views and photo albums are covered by hermetic tests only (O2, O4,
  O6). Streaming and Stop were checked live (O3, O5). Voice notes answer "Voice isn't supported yet".
- **Cancelled shutdown.** After a cancelled Windows shutdown ossbot stays down until `up`.
- **CI** is `workflow_dispatch` only, so run it by hand from Actions.
- **Redeploys** are fast (about 13 s) unless `sandbox/` changes and the images rebuild.
- **Redaction.** The hold-back assumes bounded `extra_patterns` widths (F005-R2-3).
- **Closed-PR card.** It lacks Re-attempt later and Forget (F010-N1).
- **Re-checks at the gate.** GHMCP-2 was not re-run in a scout job at the gate, because a scout job is
  opus-priced. Doctor re-checked the same binary and secret.
- **Windows.** The isolation script and doctor cover the WSL boundary. The Hyper-V firewall WARNs if
  elevation was declined.

## User action needed

No credential problem was found. The bot token, both Kiro keys, the read token (also used by the GitHub
MCP server) and the write token's identity check all pass doctor.

- **Threaded mode.** Done (O1). If the gatekeeper logs `telegram flat mode: Threaded mode is off in
  @BotFather`, turn it on again (SETUP.md "Telegram Threaded mode").
- **Unknown Telegram user.** Updates from a user that is not you were rejected and logged as
  `telegram_unauthorized`. Nothing to do unless that was you on a second account.
- **Before `/dryrun off`**, see above.
- **Owner live checks O1-O8 and restart test R1**, below.
- **Push `main`** when you are ready. The build never pushes.

## Owner live checks

Each check names its TRACEABILITY rows, the steps and the expected result. Hermetic tests already cover
the code paths. These checks confirm the real Bot API, your Telegram clients or a first live write.
Record each one when done, with no secret or personal value in the text:

```powershell
powershell -NoProfile -File scripts\dev\record-owner-check.ps1 -Check O1 -Rows CR3-V-4 -Result "what you saw"
```

The script:

- sets the rows to `done`, or to `missing` with `-Status missing`;
- appends the result to their evidence;
- recomputes the Totals line;
- adds a row to VERIFICATION "Owner live checks (after the build)".

Commit `docs/TRACEABILITY.md` and `docs/VERIFICATION.md` afterwards.

**O1 Threaded mode** (CR3-V-4, part of DES-V-32). Done 2026-10-20: Threaded mode on, the four threads
were created, but the dashboard pin showed in "All", not in Control. Fixed post-v1 (item 4): the
dashboard is pinned only once its send result is in Control, and re-sent into Control once otherwise
(ADR-0020 addendum). `pinChatMessage` cannot name a thread, so a client may still list the pin in "All".
**Re-check 2026-10-20 (open):** the dashboard is pinned in Control and edited in place
(`dashboard_pinned thread=125003`), and `/status` answers with the same icon card everywhere. One gap
remains: `/status` sent in "All" creates a new empty topic named "/status", and the card lands in All
("Known limitations").
**First fix 2026-10-21 (did not work):** commit 4724ac7 looked for the implicit-topic flag on the
command itself. The live log showed that a command typed in All arrives as two updates instead: the
`forum_topic_created` service message of a new thread (message N), then the command with no thread
(message N+1, about 300 ms later). So nothing was deleted.
**Fixed and re-checked 2026-10-21 (done):** commit 9884df9 pairs the two updates (same chat and sender,
N+1, within 2 s, topic name equals the command or `is_name_implicit`) and deletes only that thread
after the reply in All (ADR-0020 addendum). Live: `/status` in All, several times. Each time the card
landed in All and the "/status" topic was deleted within about 2 s (`implicit_topic_match ...
action=delete`, then `stray_topic_deleted`, no `stray_topic_delete_failed`). The client may keep
showing the deleted topic until the chat is reopened ("Known limitations").
- If a "/status" topic stays, read `scripts\ossctl.ps1 logs gatekeeper -Since 30m -Grep "tg_thread_fields|implicit_topic|stray_topic"`:
  `implicit_topic_pending` with no `implicit_topic_match` means the pair did not complete (no partner
  within 2 s); `stray_topic_delete_failed` gives Telegram's reason. Send commands in 🚦 Control then.
- If the pin also shows in "All", note the client: that is the client's view of a Control pin.

**O2 Rich views on each client** (CR3-V-1, CR3-V-14, CR3-V-15, CR3-V-16).
- Steps, on Android, iOS and desktop:
  1. Open a preview card's explorer and a file diff (`/review`).
  2. Quote-reply one line of a rich file view.
  3. Ask Hermes for an answer with a table and a code block, once in a thread and once in flat mode.
  4. Look at a 2-button row with long labels.
- Expected:
  - `details`, tables, `pre language=diff` and anchors render.
  - A quote-reply on a rich file view becomes a line comment. If not, note the client.
  - Labels are cut with `…`, never wrapped into each other.
  - An answer that cannot nest a block falls back to classic.

**O3 Streaming and Stop** (CR3-V-2, CR3-V-3). Done 2026-10-20: the draft streamed, Stop ended it with
partial text, and no Too Many Requests was logged.
- Steps: ask Hermes a long question in a thread. Then ask another and tap ⏹ Stop after a few lines.
- Expected:
  - A draft appears within 1 s and updates at most once a second.
  - `scripts\ossctl.ps1 logs gatekeeper -Since 15m -Grep "Too Many Requests|429"` prints no lines.
  - Stop ends the stream within 2 s with "⏹ Stopped by you", and the partial text ends in `…`.

**O4 Small rendering items** (CR3-V-5, CR3-V-6, CR3-V-12).
- Steps:
  1. Wait for a 🎉 message (a merged PR or `/digest`).
  2. Open an `.html` diff document from a preview card.
  3. Look at a classic message that mentions a command, for example the `/approve` hint.
- Expected:
  - The effect plays, or the message just arrives with no error.
  - The `.html` opens in a browser or viewer.
  - The command text is not a tappable link.

**O5 Hermes API** (CR3-V-7). Re-opened 2026-10-20: Stop interrupted the Hermes turn, but the Kiro ACP
request kept running ("no sockets found; in-flight request may keep running"). Post-v1 item 2 adds
`KiroV3Client.cancel()` (ACP `session/cancel`, ADR-0002 addendum). **Done 2026-10-20 (close-out):**
`ossctl hermes probe --cancel-check` on roles.test passed (`stopReason cancelled` 0.01 s after the
cancel), and your Stop in the real chat behaved as expected. The journal shows `Interrupted during API
call` and no "in-flight request may keep running".
- Steps (to re-check): ask a long question, tap ⏹ Stop, then run
  `scripts\ossctl.ps1 logs hermes -Since 15m -Grep "interrupt|cancel|kiro_acp turn"`.
- Expected: an interrupt line within a few seconds of the Stop, and no "no sockets found; in-flight
  request may keep running". The provider's `kiro_acp cancel: session/cancel sent` and
  `kiro_acp turn: … stop=cancelled` are INFO lines; the journal shows them only when Hermes logs INFO
  there. As root, `ossctl hermes probe --cancel-check` proves the cancel path directly.

**O6 Photos** (CR2-7l, CR3-IH-4, CR3-V-8, CR3-V-13).
- Steps: send a food photo (compressed), then an album of 2-3 photos, then a photo sent as a file.
- Expected:
  - A 🤖 answer with a protein estimate.
  - The album gets one answer.
  - No answer quotes location or camera data.

**O7 Opt-in mirror and Telegraph** (CR3-V-9, CR3-V-11). Only if you want these features.
- Steps: run `ossctl review-mirror enable` with a private repo of yours. Then run
  `ossctl telegraph setup` and tap Publish on a long answer.
- Expected: enable passes its probes, the article opens as Instant View, and Unpublish blanks it.

**O8 First live submit** (DES-V-17, DES-V-18).
- Steps: follow "Turning dry-run off" above, with the hermes-kiro-provider V3 PR.
- Expected: fork, push and PR succeed with the classic `public_repo` token, and the PR's commit shows
  your account as author. Note how long the fork took.

## Restart test

**Real-shutdown finding (2026-10-20).** Windows shut down at 03:08 UTC with ossbot running.
`.ossbot-local\shutdown-blocker.log` shows the following:

- The blocker (6a) got `WM_QUERYENDSESSION` and set the reason "ossbot: stopping safely…".
- Its `down.ps1` child exited at once with `0xC0000142`. That code means a process cannot initialise
  because the session is ending.
- The blocker then let the session end.
- The 6b task ended with `0xC000026B`.
- The next start reported `last shutdown unclean`.

The same happened on the restarts at 14:38 and 14:46 UTC. Windows refuses to initialise any new
process in a session that is ending; the earlier "passes" were cancelled shutdowns only.

**Post-v1 fix (ADR-0021 addendum).** No process is started at shutdown any more. At logon the blocker
opens a drain channel, one hidden `wsl.exe -d ossbot -u root --exec ossctl drain-listen` child that
it keeps (and restarts while the distro runs). On `WM_QUERYENDSESSION` it writes `down 90 blocker` to
that channel and waits for `drained`. 6b (the User32 1074 task) is retired, because it hit the same
refusal (`0xC000026B`); bootstrap removes it. The simulated test passes; only a real restart proves it.

**R1 Restart** (CR1-6a, CR1-6b, DES-V-31, DES-AC-32).

Steps:

1. After the post-v1 redeploy, close and restart the blocker once so the new code runs:
   `scripts\bootstrap.ps1 -Uninstall -Only shutdownhooks`, then `scripts\bootstrap.ps1 -Only shutdownhooks`
   (this also removes the retired 6b task).
2. Run `scripts\status.ps1`. It should show `shutdown blocker  running (pid N, idle, channel up pid M)`
   and `shutdown hook  retired (control channel)`. Make sure no ossbot build is running, then choose
   Start → Power → **Restart** with ossbot up.
3. Note three things:
   - (a) whether the blocking screen lists "ossbot: stopping safely…";
   - (b) whether the restart continues without "Restart anyway";
   - (c) how long it waited.
4. Log in again and wait 2 minutes. Then run
   `powershell -NoProfile -File scripts\dev\restart-evidence.ps1`.
5. Repeat once with **Shut down**. Fast Startup is on.

Expected:

- (a) yes, (b) yes, (c) usually 10-20 s; at most about 120 s (the 90 s drain plus the blocker's 30 s
  slack, ADR-0021 addendum).
- The blocker log shows `channel: sent down 90 blocker`, `channel: ack`, `channel: drained exit=0`
  and `drain exit=0; block reason destroyed=True`, with no `started pid` or `spawn refused` line.
- `lifecycle.log` shows `down kind=graceful caller=blocker pid=…: start timeout=90 (channel)` and
  `end exit=0`.
- `status.ps1` shows `last shutdown graceful (marker …)`.
- If it shows `unclean (no shutdown marker: …)`, the VM ended before the drain finished: record it with
  `-Status missing` and attach the restart-evidence output.

Record it:

```powershell
powershell -NoProfile -File scripts\dev\record-owner-check.ps1 -Check R1 -Rows CR1-6a,CR1-6b,DES-V-31,DES-AC-32 -Result "restart: (a) yes (b) yes (c) 15 s; last shutdown graceful; shut down: ..."
```

If the result matches the finding above, record it with `-Status missing` and schedule the 6a fix.
