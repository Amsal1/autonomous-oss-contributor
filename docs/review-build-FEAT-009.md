# Review: FEAT-009 build, round 3

Scope: cd46f63, c5ce99c, 2d30db7, b85e4c3, d05d4a7 (HEAD) against FEAT-009.json, phase2-build-plan.md items 46-48 and Working rules 1-9, DESIGN rev 5 §14.13-§14.16 and §14.14.1, TRACEABILITY.md. Tree clean at d05d4a7 before this review. origin/main is c5ce99c, an ancestor of local main, so nothing was rewritten. I re-ran no suites. Evidence read: the commit messages and VERIFICATION.md FEAT-009 section (hermetic `-m "unit or telegram or e2e" -n auto` 3248 passed at the 2d30db7 tree, lint.ps1 clean, one redeploy, doctor PASS 45; b85e4c3 targeted tests 18 passed).

Verdict: CHANGES_REQUESTED (3 MEDIUM, 1 NIT).

## Prior findings (round 2)

- F009-R2-1 (items 47/48 not delivered): partly resolved. Item 47 is built (commands/{common,lifecycle,admin,personal,review}.py, notify.py, skill card, new-model card, killed and Back-online states) with the listed test files. `/reject` is still missing (F009-R3-2). Item 48 is only partly done (F009-R3-3).
- F009-R2-2 (ask-first claim outside the actions path): resolved. `action_requests.request_action` is now shared by `POST /v1/actions` and the 🙋 button. It does require_running, the target check, normalize_text, the `<!-- ossbot:action=<id> -->` marker and policy.check_text with secret values. CLAIM_TEMPLATE carries the AI-assistance line, the same bytes as fetch.py. An open claim is reused, so fetch's `guard_failed:claim` request gets back the gate-1 claim and no second one is created.
- F009-R2-3 (rescope Continue refusals/binding): resolved. `refusal_toast` runs at both taps. `continue_rescope` re-checks `decisions.proposals.refusal()` and the state, and compares `rescope_binding` {rev, measured_class, sha256(report)}. The human decision goes into the evidence (`decided_by`). The edge stays G, as documented.
- F009-R2-4 (Make it smaller target): resolved. In RESCOPE_REQUIRED the target is now approved_class, xs included.
- F009-R2-5 (forbidden-path warning): resolved. Paths now render as `k.Code(k.Text(...))` with 🤖.
- F009-R2-6/7/8 (NITs): resolved. Gate-1 expiry copy and `⛔ Not decidable`; the expired toast; the Working-on-it reply chain; Validate shows ✅ facts plus 🤖 plan.

## WIP disposition (rule 1)

The WIP was committed, not discarded. VERIFICATION.md lists the leftovers at cd46f63 (context.py, outbox.py, ui/send.py, telegram/notify.py), and c5ce99c finished, wired, tested and committed them ("nothing reverted"). This is acceptable.

## Findings

### F009-R3-1 MEDIUM: an anomaly that pauses is sent as a P2 FYI and is held during quiet hours (U7)
DESIGN §14.13.1 lists "anomaly → pause/kill" as P0 (always rings, ignores quiet hours, never batched), and U7 says P0 is never silenced. The two code paths:

- `anomaly.py:87-90`: `action == "pause"` calls `control.pause(reason, "anomaly")`.
- `control.py:161`: that sends `notify(f"⏸ Paused by {actor}: {reason}")`.

`notify.classify` (notify.py:57-67) gives P0 only to texts that start with 🛑 or 🚨, so "⏸ Paused by anomaly: secret_in_text: …" becomes `(2, True)`. `Notifier.decide` then writes it silently as `held` until quiet hours end, or until a 2-minute batch window closes, and merges it into "📈 N updates". The same applies to every other pause rule in the §13.4 table (gatekeeper unhealthy 4 ticks, >120 kiro invocations/h, the secret escalation).

Consequence: the owner gets no sound and no message for hours after the system paused itself on a security signal. test_priorities only covers 🛑/🚨 texts.

Fix: make the anomaly pause a P0 notify. Either `Control.pause` takes a priority, with `actor == "anomaly"` mapping to P0, or anomaly.py sends its own `🚨` alert with `priority=0` and the pause FYI stays deduplicated. Add a test: with quiet hours on, `anomaly.report(Signal.SECRET_IN_TEXT, …)` twice leaves a pending, ringing P0 row in Control.

### F009-R3-2 MEDIUM: `/reject` is not implemented
Item 47's carried detail lists `/reject`. DESIGN §14.14.1 has `/reject [cid] [reason]` (config: picker → reasons `[Not valuable] [Wrong approach] [Repo unsuitable (7d)] [Other…]` → confirm). The FEAT-009 AC says "Every §14.14.1 command exists with its picker and safety class".

There is no `@command("reject")` anywhere in src (registered commands: builtin, review, personal, admin, lifecycle, work_links, size_pickers), and test_commands_full's `M1` set leaves it out. TRACEABILITY DES-FR-6 is marked done "the §14.14.1 M1 set" and defers only /digest /memory /title /remind /articles to FEAT-010.

Consequence: a typed `/reject c_…` answers "Unknown command". Rejecting is possible only from a card that is still on screen.

Fix: add `/reject [cid] [reason]` in commands/review.py, reusing the card's reason flow (`_reject_reasons`, `REJECT_REASONS`, the 7-day repo cooldown) behind a config confirm. Add it to `M1`, with a test that nothing is rejected without the confirm tap.

### F009-R3-3 MEDIUM: item 48 live check incomplete while FEAT-009 is marked completed
The FEAT-009 AC requires "Live owner-only checks: topics created once, dashboard pinned, a dry-run proposal card and preview card from the fake upstream rendered in the real chat; recorded in docs/VERIFICATION.md". The verification list requires bootstrap twice, the second run only `unchanged`.

VERIFICATION.md records the live start only. Three dashboards were pinned; the fix b85e4c3 was not redeployed, so the live system still has the bug. The proposal and preview cards in the real chat, and the second redeploy, are listed as "still open". Yet FEAT-009.json is `completed`.

Delivering a dry-run proposal card and a preview card to the real chat goes through product code (the e2e fake upstream / import path into the deployed gatekeeper) and the read-only outbox peek. An agent can do this; it does not need the owner's hands. Only the visual confirmation and typed `/kill`/`/resume` do.

Fix:
- Run the round's redeploy at HEAD (it carries b85e4c3), then the second redeploy (only `unchanged`).
- Confirm one dashboard via outbox_peek.
- Deliver one dry-run proposal card and one PR preview card from the fake upstream to the real chat, and record the outbox rows `sent` with their message ids.
- Then either get the owner's visual confirmation (workflow `warning`) or set FEAT-009 back to not completed with the owner-only items named as blocked on the owner.

Record the results in VERIFICATION.md.

### F009-R3-4 NIT: command confirms minted before a kill still act after it
handlers.py routes `CONFIRMS` taps (egress_confirm, hermes_tier_confirm, model_confirm, reload_confirm, repo_confirm) with only `use_token`, without checking `control.view().state == "killed"`. An `/egress open` confirm panel opened just before `/kill` still widens egress when tapped within its 5 minutes. §14.16 says only /status /resume /logs /help /start work while killed. Refuse go/config confirm effects while killed, with the killed reply.

## Checked and fine

- Outbox.enqueue never holds rows with buttons, tokens or an approval, nor P0/P1 rows, whatever the policy answers. P0 rings in prepare. Toggles mute and never drop. Coalescing edits only button-less sendMessage rows.
- `/down` gives a picker then a confirm (io). `/up` is refused while killed. `/undrain` asks for a confirm. `/egress`, `/hermes tier`, `/model` and `/repos` widen only after a confirm, styled danger. Skill activation is two-tap danger. `/reload` shows a redacted diff and binds its confirm to the snapshot hash.
- `/logs` reads only under `logs_dir` (resolved, is_relative_to), redacts, then escapes.
- Killed reply with reason, since and [Resume][Logs]; the killed command menu matches.
- The dashboard fix (b85e4c3) is correct: no second send while a dashboard sendMessage is pending.
