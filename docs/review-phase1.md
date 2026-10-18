# Phase 1 review (round 2): safety core, sandbox isolation, provisioning foundation

Round 2 checks the fixes for round 1 (commit 703648a, logged in 3ccd9bc) on top of the whole Phase 1 diff since 7624a0c. All five round-1 findings are closed (P1-1 to P1-5, re-probed live). The Phase 1 share of CR-001 is in place, and every command in the bar passes when I re-run it. A new reviewer probe found a second DNS channel that the port-53 fix cannot see. systemd-resolved answers arbitrary names for every service uid over its world-writable varlink socket and over D-Bus. The queries leave the host from resolved's own uid, so the owner-match firewall rules never apply.

Watch for: DNS exfiltration through systemd-resolved IPC for ossrunner/hermes/gatekeeper/ossbox/ossnobody (confirmed, live probe); a ≤10 s window where a root kill plus a fast resume keeps an armed approval alive (confirmed by code reading); the watchdog's `down` exemption ignoring scope (confirmed); the drain-timeout knob not reaching `down.ps1` or the sandboxd unit (confirmed); an unexplained app-deploy `changed` that restarted four units (confirmed observation, cause unknown).

**Verdict**: NEEDS_CHANGES

## High-level view

The host egress layer still has a DNS hole. Raw UDP/TCP 53 is closed for the five service uids (my probes of 10.255.255.254, 127.0.0.53 and 127.0.0.54 all refuse). systemd-resolved, however, takes requests from any uid over its varlink socket and over D-Bus, then queries upstream as `systemd-resolve`, which OSSBOT-OUT does not restrict. Nothing in the distro needs resolved, so masking it is safe. Neither `test_egress_per_uid.sh` nor doctor's per-uid probe tries this path, so both report PASS.

Kill invalidation now covers every kill path, but it relies on the 10 s reconcile tick seeing the `killed` flag. A root-written kill that is resumed before the next tick keeps an armed approval and its confirm token. The DESIGN §13 claim "a quick resume never revives a confirm token" overstates this.

The CR-001 lifecycle matches DESIGN §13.5 except at two edges. The watchdog treats any `down` as "stopped on purpose", including the workers scope where the gatekeeper keeps running. The configurable drain timeout is honoured only by `ossctl down`, not by `down.ps1` or the sandboxd unit.

<details>
<summary>Issues (5)</summary>

1. **P1-6 HIGH: DNS exfiltration through systemd-resolved IPC** (`scripts/provision.sh` / `scripts/lib/wsl.sh`: resolved left enabled; `src/ossbot/ctl/render.py:198-215`; `tests/system/test_egress_per_uid.sh:22-53`; `src/ossbot/ctl/doctor.py:248-275`). Every service uid resolves arbitrary names via varlink and D-Bus, past the firewall. Mask `systemd-resolved` in an idempotent provision step, probe varlink + D-Bus `ResolveHostname` per uid in the system test and doctor (expect failure, root control still resolves via 10.255.255.254), have doctor/watchdog check that resolved stays masked, and add an ADR-0005 addendum.
2. **P1-7 NIT: kill-to-resume window keeps approvals armed** (`src/ossbot/gatekeeper/control.py:226-235,314-319`; `src/ossbot/gatekeeper/app.py:35`). A root kill that is resumed within one 10 s housekeeping tick is never seen by reconcile. Invalidate armed/confirmed in `request_resume()` when a kill flag is present and in `reconcile()` whenever offline resume entries are read. Also fix the comment claiming nothing can be armed while killed (`ApprovalService.approve` itself has no running check).
3. **P1-8 NIT: watchdog `down` exemption ignores scope** (`src/ossbot/watchdog/tick.py:208-216,228`). A workers-scope `down` keeps the gatekeeper running and survives reboots, yet disables the gatekeeper-unhealthy pause and `restart_failed`. Exempt only `scope == "distro"`, or keep `check_health` for workers.
4. **P1-9 NIT: drain timeout knob not propagated** (`scripts/down.ps1:35-36`; `systemd/ossbot-sandboxd.service` `OSSBOT_DRAIN_TIMEOUT_S=120`, `TimeoutStopSec=150`; `src/ossbot/common/config.py` `LifecyclePolicy.drain_timeout_s le=3600`). Raising `lifecycle.drain_timeout_s` above 120 lets `down.ps1` terminate the distro mid-drain (cap fixed at 210 s), and the SIGTERM drain and stop timeout stay at 120/150. Either cap the policy at 120 or make `down.ps1` and the unit (drop-in) use the effective value.
5. **P1-10 NIT: unexplained app-deploy `changed`** (`scripts/lib/services.sh:49-55`). My first bootstrap after the fixer's unchanged 08:30 run reported `rsync to /opt/ossbot/app (1 items)` and restarted dockerfilter, gatekeeper, proxy and sandboxd, although no deploy-source file was newer. Only the count is logged, so the cause cannot be traced. Log the itemized lines to the provision log and ignore metadata-only differences (`--omit-dir-times` or a content checksum) so they don't restart services.

</details>

<details>
<summary>Details</summary>

### systemd-resolved is a DNS channel past OSSBOT-OUT (P1-6, HIGH, confirmed)

The round-1 fix (`render.py:198-215`) renders `-p udp|tcp --dport 53 -j REJECT` for each service uid ahead of the `-o lo` ACCEPT. That covers sockets the uid opens itself. systemd-resolved is still `active`, its varlink socket is `srw-rw-rw- systemd-resolve` and its D-Bus API is open to any local user. A resolution request over either path is a unix-socket or bus message. resolved then sends the actual query upstream as uid `systemd-resolve`, which has no owner rule. Reviewer probe, run as each uid:

```
ossbox     varlink:  {"addresses":[{"family":2,"address":[104,20,23,154]},...],"name":"example.com"}
ossbox     resolvectl query rv-ossbox-16871.example.com: ... does not have any RR of the requested type
ossbox     busctl ... ResolveHostname 'isit' 0 example.com 2 0  ->  a(iiay)st 2 0 2 4 172 66 147 243 ...
(same for ossrunner, hermes, gatekeeper, ossnobody; getent via nss "files dns" gets nothing)
```

The random-label query returning an upstream NODATA shows that attacker-chosen labels reach the internet. That is enough for DNS tunnelling to an authoritative server the attacker controls. The exposed uids are the same as in P1-1, including Hermes in Phase 2 (LLM-driven, holds a Kiro key) and ossbox (holds the sandbox `KIRO_API_KEY`). Sandbox containers are not affected: they get `--dns 127.0.0.1` on `--internal` networks and no `/run` mounts.

Nothing on this distro needs resolved. `/etc/resolv.conf -> /mnt/wsl/resolv.conf` names only `nameserver 10.255.255.254`, nsswitch is `hosts: files dns`, and root/ossadmin resolve directly. Masking it (`systemctl disable --now systemd-resolved` and `systemctl mask systemd-resolved`, printing changed/unchanged) removes both the varlink socket and the bus name. A restrictive D-Bus policy would not cover varlink, so masking is the simpler fix. The probes need the same coverage as P1-1: in `test_egress_per_uid.sh` and doctor's per-uid check, a varlink `ResolveHostname` and a D-Bus `ResolveHostname` per uid, both expected to fail. Doctor and the watchdog should also flag resolved being unmasked again, for example after an apt upgrade.

### Kill invalidation depends on reconcile seeing the flag (P1-7, NIT, confirmed)

`reconcile()` (`control.py:314-319`) calls `invalidate_armed_confirmed` only while `flags.killed` is present, and housekeeping runs it every `HOUSEKEEPING_SECONDS = 10.0`. Take a soft kill written by root (`ossbot-kill`, `kill.ps1`, watchdog) followed within that tick by either resume path:

- an offline `ossbot-resume`, which removes the flags and leaves a `resume.log` line;
- `/resume` or `ossctl resume`, where `request_resume()` writes `resume-request` and the enforcer removes the flags.

Reconcile then sees no flag, and the armed approval and its 5-minute confirm token survive. Confirm still needs a human tap, and writes were blocked for the whole kill, so the impact is small. It does contradict the new DESIGN §13 bullet. Two cheap fixes close it: invalidate in `request_resume()` when a kill flag exists, and in `reconcile()` whenever `offline > 0`. The reconcile comment "nothing can be armed while the flag is present" is also inaccurate. `ApprovalService.approve()` has no running check; only the Telegram handler (`handlers.py` `_approve`) refuses.

### Lifecycle edges (P1-8, P1-9, NIT, confirmed)

`run_tick()` sets `down = lifecycle.state == "down"` regardless of scope (`tick.py:208-216,228`). It then skips `check_health` and `restart_failed`. That fits the distro scope, where `ossctl down` stops the gatekeeper. In the workers scope the gatekeeper keeps running, and the flag survives restarts until `up`. `/v1/control/down` accepts `scope: workers` today, and `test_lifecycle.sh` uses it. A gatekeeper that dies during a long workers-down is then neither paused nor restarted by the watchdog. systemd's `Restart=` still applies.

`down.ps1` caps the in-distro step at `drain + 90` s, where `drain` falls back to a hard-coded 120 when `-Timeout` is absent (`down.ps1:35-36`). `ossctl down` meanwhile uses `policy.yaml lifecycle.drain_timeout_s`, which `LifecyclePolicy` allows up to 3600. With the policy at, say, 300, the Windows script kills `ossctl down` and terminates the distro during the drain. The SIGTERM drain (`OSSBOT_DRAIN_TIMEOUT_S=120`) and `TimeoutStopSec=150` in the units are also static. The CR-001 rule "TimeoutStopSec ≥ drain timeout + 30 s" then holds only for the default.

### Provision idempotence observation (P1-10, NIT)

The fixer's last full bootstrap (provision log `20261018T013049Z`, 08:30:53) was all `unchanged`. My first run (`014623Z`) reported `app-deploy: changed` (1 rsync item, then `uv sync`) and restarted four units through `enable-start`. My next run (`014736Z`) was all `unchanged`. No file under `src prompts sandbox/proxy pyproject.toml uv.lock docs/DESIGN.md` was newer than 08:30:40. A dry-run of the same rsync stayed empty after the sandbox pytest, all ten system scripts, the DEV suite, `doctor --quick` and `ossctl status`, so I could not reproduce the cause. Logging the itemized rsync output would make such a change explainable. The restart it triggers also cuts any running sandbox job, which now drains for up to 120 s.

### Verification evidence (re-run by the reviewer, 2026-10-18 08:40-08:58 +07)

| Bar item | Result |
|---|---|
| `DEV pytest -m "unit or telegram"` | 1530 passed, 11 deselected (run twice) |
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports` | clean / 176 formatted / 101 files no issues / 4 contracts kept |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key` live Kiro probes, Phase 2), run twice |
| `ROOT bash tests/system/*.sh` (10 test scripts + `lib.sh`) | all exit 0, 199 PASS, 0 FAIL (run twice) |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13 |
| `scripts\bootstrap.ps1` | first run: `app-deploy`/`enable-start` changed (P1-10); next run: every step `unchanged` (kiro/hermes `skipped (phase 2)`, Hyper-V WARN not elevated) |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer (+ boot-gate, firewall x2) | all active |
| `docker version` / `docker compose version` | 29.9.1 / v5.6.0 |
| `ossbot-firewall --check` / `ossctl audit verify` | 0 / ok (214 rows, 56 anchors) |
| `ossctl doctor` (root) | PASS 28, WARN 2, SKIP 3, FAIL 0, exit 0. Secrets now exist (NOTE-live-secrets.md), so the live GitHub/Telegram checks PASS; the WARNs are the runner-owned catalog and the not-yet-run safety probes (Kiro jobs stay refused by the sandboxd probe gate) |
| `gitleaks git --redact` | no leaks (26 commits) |
| personal values outside `docs/` | none: `git grep` for the owner's name/login/email/IdC host and every value in the gitignored `config/profile.yaml` found 0 hits; only placeholder/test IdC URLs |
| `git status --porcelain` | empty before this review's commit |
| pushes | none by this step. `origin/main` = 3ccd9bc via `update by push` at 08:37:42, the same ~20 s-after-commit pattern as every commit since the design phase. No hooks or push config in the repo; the user pushes `origin` himself (message 10, PLAN "After the build"), so these pushes are not attributable to the build steps |

Bar (a) fails on P1-6 (egress allowlist fail-closed). Bars (b), (d) and (e) pass. Bar (c) passes on the run after the first; P1-10 covers the first run's change.

</details>

<details>
<summary>File map</summary>

- `src/ossbot/ctl/render.py`, `tests/unit/ctl/golden/iptables.rules`: per-uid port-53 REJECT/DROP ahead of the lo ACCEPT (P1-1 fix; P1-6 is outside its reach)
- `src/ossbot/ctl/doctor.py`, `tests/system/test_egress_per_uid.sh`: resolver probes per uid (extend for P1-6)
- `src/ossbot/gatekeeper/control.py`, `api/control.py`: reconcile kill invalidation (P1-7), lifecycle drain/down/up/status
- `src/ossbot/common/control_flags.py`, `paths.py`, `schemas.py`, `config.py`: lifecycle flag, requests, `LifecyclePolicy` (P1-9)
- `src/ossbot/sandboxd/{jobs,main,app}.py`: admission refusal while not up, drain, start-up recovery, SIGTERM drain
- `src/ossbot/gatekeeper/telegram/{bot,handlers,keyboards}.py`, `commands/builtin.py`, `pollstats.py`: background connect with backoff, poll tracking, Approve/Confirm refusal while not up, exact callback regex
- `src/ossbot/watchdog/tick.py`: sleep detection, lifecycle `down` exemption (P1-8)
- `src/ossbot/ctl/cli.py`, `scripts/{up,down,status}.ps1`, `scripts/bin/ossbot-boot-gate`: `ossctl up|down|status`, Windows entry points (P1-9), shutdown classification
- `systemd/*.service`: `TimeoutStopSec=150`, proxy `ExecStop` 2 s
- `scripts/lib/services.sh`: app-deploy rsync (P1-10)
- `tests/**`: lifecycle, drain, crash safety, polling, reconcile, systemd-unit tests
- `docs/{DESIGN,PLAN,VERIFICATION}.md`, `docs/plan/{CR-001-lifecycle-up-down,NOTE-live-secrets}.md`, ADR-0005 addendum

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 7624a0caa7667aa02a4bccce76f820c5be1fc6f6..HEAD` (round-2 delta: `4f4e2a3..HEAD`)

</details>
