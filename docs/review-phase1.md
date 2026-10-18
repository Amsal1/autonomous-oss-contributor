# Phase 1 review (round 1): safety core, sandbox isolation, provisioning foundation

Phase 1 (FEAT-001..004 plus fix 1e79583) delivers the deterministic gatekeeper, the two-tap Telegram decision path, the git TCB and submission, the Docker filter, sandboxd with the split coder, squid plus iptables egress, provisioning from zero with WSL confinement, and the kill/resume/watchdog chain. I re-ran every verification command in the bar inside `ossbot` and all of them pass. The second bootstrap run reports only `unchanged`. Two gaps keep the safety bar from being met. Every service uid can query the WSL DNS resolver directly through the loopback ACCEPT rule, which is an open DNS exfiltration channel the egress tests do not cover. Kills that bypass the gatekeeper (watchdog, `ossbot-kill`, `kill.ps1`) leave armed and confirmed approvals alive. The user's change request CR-001 (graceful up/down/drain) is added as a required finding.

Watch for: DNS to `10.255.255.254` from ossrunner/hermes/gatekeeper/ossbox/ossnobody (confirmed, live probe); armed approvals surviving out-of-band kills (confirmed); CR-001 lifecycle not implemented (confirmed, new requirement).

**Verdict**: NEEDS_CHANGES

## High-level view

The gatekeeper's binding, token and checkpoint rules hold against DESIGN §9.2/§12.4 for every trigger the gatekeeper itself sees. The exception is kill. Only the gatekeeper's own kill path invalidates approvals. The root paths (watchdog, `ossbot-kill`, boot gate) write the flag file, and reconcile only mirrors the state. An armed approval therefore survives the kill, and a Confirm tap after a quick resume still submits.

The host egress layer has a hole that the live resolver placement exposed. ADR-0005 records that the WSL resolver sits on `lo`, but `OSSBOT-OUT` still accepts all `-o lo` traffic for the five service uids. That contradicts §11.4 ("No user gets direct UDP 53"). Neither `test_egress_per_uid.sh` nor doctor's per-uid probe looks at the resolver, so both report PASS.

CR-001 adds a graceful lifecycle (up/drain/down) next to kill. Its gatekeeper state, sandboxd drain, systemd stop timeouts, ossctl and Windows scripts, and crash-safety test belong in Phase 1. Stage re-queue, the Telegram commands and the docs depend on FEAT-005/006/008.

<details>
<summary>Issues (5)</summary>

1. **P1-1 HIGH: DNS exfiltration via the loopback resolver** (`src/ossbot/ctl/render.py:200,204`; `tests/system/test_egress_per_uid.sh:15`; `src/ossbot/ctl/doctor.py:239`). Every service uid gets real DNS answers from `10.255.255.254:53` because of `-o lo -j ACCEPT`. Add per-uid REJECT rules for UDP/TCP 53 to the rendered nameservers (and 127.0.0.53) before the lo ACCEPT, and extend `--check`, the goldens, `test_egress_per_uid.sh` and doctor.
2. **P1-2 MEDIUM: out-of-band kill keeps approvals alive** (`src/ossbot/gatekeeper/control.py:238`; flag writers `src/ossbot/watchdog/tick.py:95`, `scripts/bin/ossbot-kill`, `scripts/bin/ossbot-boot-gate`). `ControlService.reconcile()` mirrors a kill flag written by root (watchdog, `ossbot-kill`, `kill.ps1`, boot gate) without calling `invalidate_armed_confirmed`. Invalidate in the same transaction when a new kill flag is observed, including at startup, and add tests.
3. **P1-3 MEDIUM: CR-001 graceful lifecycle (user change request)** (`docs/plan/CR-001-lifecycle-up-down.md`; `src/ossbot/gatekeeper/control.py`, `src/ossbot/sandboxd/jobs.py`, `systemd/*.service`, `scripts/`, `src/ossbot/ctl/cli.py`). Implement `docs/plan/CR-001-lifecycle-up-down.md`: up/draining/down distinct from kill, drain timeout 120 s, `TimeoutStopSec` ≥ drain + 30 s, re-queue interrupted stages, never auto-resume public writes, `scripts/{up,down,status}.ps1`, `ossctl up|down|status`, crash-safety e2e (SIGKILL mid-stage), sleep/hibernate handling, a long-poll reconnect test. Commit the CR file with the implementation and update DESIGN.md. Telegram `/drain /undrain /down /up` and the README/SETUP/AGENTS docs may move to Phase 2 (FEAT-005/008); record the split in PLAN.md.
4. **P1-4 NIT: human text edits skip the policy text check** (`src/ossbot/gatekeeper/approvals.py:273`). `ApprovalService.set_text_override` stores edited titles/bodies without `policy.check_text`. There is no caller in Phase 1, so FEAT-005 must run the check before storing an edit.
5. **P1-5 NIT: callback/token regexes accept a trailing newline** (`src/ossbot/gatekeeper/telegram/keyboards.py:10`, `src/ossbot/common/ids.py:19`). `CALLBACK_RE` and `TOKEN_RE` use `$` with `.match`, so `"t:<22>\n"` passes the format check. It fails closed today (no DB row), but `fullmatch` would make §14.2 exact.

</details>

<details>
<summary>Details</summary>

### Service uids resolve names through the WSL resolver (P1-1, HIGH, confirmed)

`src/ossbot/ctl/render.py:200,204` renders, for every service uid, `-m owner --uid-owner <u> -o lo -j ACCEPT` before the final REJECT. FEAT-003 established by live probe that WSL places its resolver `10.255.255.254/32` on `lo`. Traffic to it therefore leaves via `lo` and matches the ACCEPT. A reviewer probe (raw UDP query for `example.com` A, sent as each uid) got a 61-byte answer for all five accounts:

```
ossrunner  udp53->10.255.255.254: ANSWER 61
hermes     udp53->10.255.255.254: ANSWER 61
gatekeeper udp53->10.255.255.254: ANSWER 61
ossbox     udp53->10.255.255.254: ANSWER 61
ossnobody  udp53->10.255.255.254: ANSWER 61
```

Any process running as these uids can tunnel data out over recursive DNS. That includes Hermes in Phase 2 (LLM-driven, holds a Kiro key) and ossbox (holds the sandbox `KIRO_API_KEY`). This bypasses squid and the per-uid port matrix entirely. `tests/system/test_egress_per_uid.sh:15,35` only probes `1.1.1.1:53`, and doctor's "per-uid egress: only owned ports" check (`doctor.py:239`) has the same blind spot, so both report PASS. No service needs host-side name resolution: every HTTP client goes through the proxy by IP literal, and the proxy's own DNS is covered by `OSSBOT-IN`.

Required fix: for each service uid, render `-d <NS>/32 -p udp --dport 53 -j REJECT` and the TCP equivalent ahead of the `-o lo` ACCEPT, for every nameserver from `/etc/resolv.conf` (and 127.0.0.53 if present). Alternatively, restrict the lo ACCEPT to `-d 127.0.0.0/8`. Then update `--check` normalization and the golden `iptables.rules`, and add resolver probes (UDP and TCP, the resolv.conf address) to `test_egress_per_uid.sh` and the doctor per-uid check. The resolver-change drift detection already exists and keeps the rule in sync.

### Kill invalidation only on the gatekeeper's own path (P1-2, MEDIUM, confirmed)

§12.4 says "Kill invalidates `armed|confirmed` approvals". `ControlService.kill()` does this, but only Telegram `/kill` and `POST /v1/control/kill` reach it. The watchdog (`watchdog/tick.py:95`, soft kill on perm/firewall/anchor drift), `ossbot-kill` (`kill.ps1`, `ossctl kill`) and the boot gate write `/var/lib/ossbot/control/killed` directly. `ControlService.reconcile()` (`control.py:238`) then only sets `control_state` to killed and audits `control_reconciled`. It never calls `approvals.invalidate_armed_confirmed`.

After a watchdog soft kill the gatekeeper keeps running with the approval still `armed` and its 5-minute confirm token valid. If the operator resumes within the TTL, the Confirm tap passes `check_token`, `system_running()` and the binding recheck, and submits. A `confirmed` approval whose `consume()` hit the kill stays `confirmed` until its 72 h expiry instead of being invalidated. No current path re-submits it, but the stored state contradicts the invariant. Writes are still blocked while killed, because `checkpoint()` reads the flags, so this is MEDIUM rather than HIGH.

Required fix: in `reconcile()`, when a kill flag is observed that the DB has not yet seen (state not killed, or a newer flag `ts`), call `invalidate_armed_confirmed("killed", "flag")` in the same transaction and FYI the count. Add unit tests in `test_control_reconcile.py` for a root-written soft flag and a full flag at startup.

### CR-001 graceful lifecycle (P1-3, MEDIUM, required by the user)

The user filed `docs/plan/CR-001-lifecycle-up-down.md` (uncommitted, required) during this review. Nothing in Phase 1 implements it. Kill is the only stop path, and it is sticky, invalidates approvals and needs a resume. The Phase 1 share is the parts that touch code Phase 1 owns:

- the up/draining/down lifecycle state in the gatekeeper control model, with kill overriding it;
- drain semantics in sandboxd (no new jobs, wait up to the drain timeout, mark the rest interrupted);
- `TimeoutStopSec` ≥ drain + 30 s on every unit, with stop ordering that keeps the gatekeeper and proxy last;
- `scripts/up.ps1`, `down.ps1 [-Timeout] [-All]`, `status.ps1`, and `ossctl up|down|status`;
- the M5 rule that an interrupted public write becomes `SUBMIT_FAILED (interrupted)`;
- a crash-safety test that SIGKILLs services and a sandbox mid-job, then checks reconciliation and no duplicate writes;
- monotonic-time / wall-clock-jump handling for sleep, and the fake-Bot-API long-poll reconnect test.

Stage re-queue on `up` needs the runner (FEAT-006), the Telegram commands need the FEAT-005 command UX, and the docs belong to FEAT-008. The fixer should implement the Phase 1 parts, commit the CR file with them, update DESIGN.md (§7.5, §7.6, §13) and record the Phase 2 remainder in PLAN.md under FEAT-005/006/008.

### Minor

`ApprovalService.set_text_override` (`approvals.py:273`) does not run `policy.check_text`. No Phase 1 code calls it. FEAT-005's Edit title/body must run the check before storing, otherwise an edited body could skip the confidentiality floor and the disclosure check (P1-4).

`keyboards.CALLBACK_RE` (`keyboards.py:10`) and `ids.TOKEN_RE` combine `$` with `re.match`, which accepts one trailing `\n` (P1-5). Lookup fails afterwards, so the path fails closed.

### Verification evidence (re-run by the reviewer, 2026-10-18)

| Bar item | Result |
|---|---|
| `DEV pytest -m "unit or telegram"` | 1494 passed |
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports` | clean / 167 formatted / 100 files no issues / 4 contracts kept |
| `ROOTDEV pytest -m sandbox` | 8 passed, 2 skipped (`needs_key`, plan-sanctioned) |
| `ROOT bash tests/system/*.sh` (8 scripts) | all exit 0, 143 PASS, 0 FAIL |
| `tests\system\test_kill.ps1` | all checks passed (hung kill 36.5 s, resume order, -Hard + resume.ps1) |
| `wsl --terminate ossbot; scripts\test-isolation.ps1` | PASS 13/13 |
| second `scripts\bootstrap.ps1` | every step `unchanged`; kiro/hermes `skipped (phase 2)`; Hyper-V WARN (not elevated) |
| `systemctl is-active` (5 required + boot-gate, firewall x2) | all active |
| `docker version` / `docker compose version` | 29.9.1 / v5.6.0 |
| `ossbot-firewall --check` / `ossctl audit verify` | 0 / ok (126 rows, 39 anchors) |
| `ossctl doctor` and `scripts\ossctl.ps1 doctor` | PASS 23, AWAITING SECRET 7, SKIP 3 (Phase 2), exit 0; Windows part all PASS |
| `gitleaks git --redact` | no leaks (23 commits) |
| personal values outside `docs/` (`git grep`) | none (only placeholder/test IdC URLs) |
| `git status --porcelain` | only `?? docs/plan/CR-001-lifecycle-up-down.md` (the user's CR, to be committed by the implementing step) |
| pushes | none by this step. `origin/main` reflog shows `update by push` entries including 1e79583 at 07:13; DESIGN §6 says the owner pushes `origin` himself, and this review cannot attribute them |

Bar (a) fails on P1-1 (egress fail-closed) and P1-2 (kill invalidation). Bars (b), (c) and (e) pass. Bar (d) passes apart from the user's own uncommitted CR file.

</details>

<details>
<summary>File map</summary>

- `src/ossbot/gatekeeper/{approvals,state_machine,control,submit,policy,ratelimit,audit,anchors,packaging,gitops,derive,live_checks,reconcile}.py`: safety core, git TCB, submission
- `src/ossbot/gatekeeper/telegram/**`: auth, two-tap handlers, minimal cards, safety commands
- `src/ossbot/dockerfilter/{main,rules}.py`: root Docker API filter (§10.8)
- `src/ossbot/sandboxd/**`, `src/ossbot/sandbox_tools/**`, `sandbox/**`: job templates, split coder, entrypoints, images, squid template
- `src/ossbot/ctl/{render,doctor,cli}.py`: squid/iptables renderers (P1-1), doctor, ossctl
- `src/ossbot/watchdog/**`: 30 s invariants, anchors, kill re-enforcement (P1-2 source of out-of-band kills)
- `scripts/**`, `systemd/**`, `config/**`: bootstrap/provision, kill/resume/boot gate, units, pins
- `tests/**`: unit, telegram, sandbox, system suites

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 7624a0caa7667aa02a4bccce76f820c5be1fc6f6..HEAD`

</details>
