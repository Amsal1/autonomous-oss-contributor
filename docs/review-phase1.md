# Phase 1 review (round 6): egress and DNS surface

Round 6 reviews commit 7288d2d, the fix for round 5's P1-14 (io_uring's `IORING_OP_SOCKET` bypassing `RestrictAddressFamilies`) and its two NITs. It also re-reads the 41-row threat table in `docs/VERIFICATION.md` against squid's ACL semantics, every DNS path, IPv6, ICMP, raw IP, the Hyper-V layer, the non-IP host channels and the ways a confined process could get out from under its unit's filters. Two independent layers close P1-14. One is `kernel.io_uring_disabled = 2` with a fail-closed drift check. The other is a per-unit `SystemCallFilter`. A control window in the evidence proves each layer works on its own. Both NITs are closed, and no HIGH or MEDIUM hole remains in any row.

Watch for: five hardening NITs, none blocking.
- `denied_dst` lacks the IPv6 unspecified address `::/128` (confirmed omission; nothing reachable today, likely).
- The Hyper-V check matches rule names, not rule contents, and ignores extra allow rules (confirmed).
- The unit filters assume no service uid can reach a systemd user manager, and nothing asserts that (likely).
- Row 7 misstates which callers hold broad egress (confirmed).
- If the surface test is SIGKILLed inside its io_uring window, io_uring stays enabled with the watchdog timer stopped (confirmed).

**Verdict**: APPROVED

## High-level view

The io_uring fix is complete. The drift check runs in the `ExecStartPre` of gatekeeper, sandboxd and the proxy, and in the watchdog and doctor. An unreadable sysctl counts as drift. The control window holds the sysctl at 0. Unit contexts and pools still refuse io_uring there, and the unrestricted control reaches 2:50002, so the refusals are real evidence. Row 41's other socket entry points hold: `socketcall` through foreign ABIs, fd passing, `pidfd_getfd` and new namespaces. One assumption under all of this has no assertion: a unit must not be able to reach a systemd user manager for its uid. Such a manager spawns processes without the unit's filters.

squid, DNS and the Docker filter have no new hole. The new `deny manager` line is a fast ACL. The Docker filter allows no pull or build, no logging driver except `local`, and no non-internal or IPv6 network, so dockerd cannot act as an egress agent. The remaining items are consistency and drift detection. `denied_dst` has no IPv6 twin of `0.0.0.0/8`. The Hyper-V check would not notice a widened rule. Row 7 states the wrong reason the key-holding `web_public` callers are acceptable. The io_uring test window has no dead-man restore.

<details>
<summary>Issues (5)</summary>

1. **NIT IPv6 unspecified address not in `denied_dst`**: add `::/128` to `always_denied.cidrs`. It is the IPv6 twin of `0.0.0.0/8` and also connects to the proxy's own loopback. `::ffff:0:0/96` can be listed too as documentation, although squid's v4-mapped storage already matches it against the IPv4 ranges.
2. **NIT Hyper-V drift check reads names only**: the doctor in `scripts/ossctl.ps1` should enumerate every Hyper-V rule for the WSL VMCreatorId. It should require exactly the two ossbot rules: Outbound, Allow, Enabled, TCP, remote port 443 or 80. `hyperv-firewall.ps1` should re-create a rule whose properties differ, not keep any rule that has the right name.
3. **NIT user-manager escape not asserted**: assert `ProtectHome=yes` or `tmpfs` for every non-root unit in `tests/unit/test_systemd_units.py`. In the surface test and doctor, assert `Linger=no` and no `/run/user/<uid>` for the five service uids. Record both as controls in row 41.
4. **NIT row 7 misstates who holds broad egress**: the research pool and Hermes hold a Kiro key and `web_public`. Reword rows 7 and 36 so the accepted residual names the control that actually applies: ADR-0017 gives the agent no path to the key, and outputs are scanned for it.
5. **NIT io_uring control window has no dead-man**: before the test sets the sysctl to 0, arm a root `systemd-run --on-active=90` job that restores `kernel.io_uring_disabled=2` and starts `ossbot-watchdog.timer`. Cancel it after the restore.

</details>

<details>
<summary>Details</summary>

### io_uring control window edge (NIT, confirmed)

The surface test stops `ossbot-watchdog.timer` and then writes 0 to the sysctl. The EXIT trap restores both on any signal bash handles. It does not run on SIGKILL, or when `wsl --terminate` hits during those seconds. The system then runs with io_uring enabled and no watchdog until something starts the timer or the next boot resets the sysctl. Running units keep their filter and containers keep Docker's seccomp profile, so only unconfined, root-equivalent sessions gain io_uring. That limited reach makes it a NIT. A transient root timer armed before the write removes the edge.

### IPv6 unspecified address in `denied_dst` (NIT, confirmed omission)

`always_denied.cidrs` includes `0.0.0.0/8`, with a comment: a name that resolves to 0.0.0.0 makes squid connect to its own loopback. On Linux an `AF_INET6` connect to `::` behaves the same way and reaches `::1`. The list has `::1/128` but not `::/128`. A `web_public` caller (Hermes on 3141, or the research pool) can publish an AAAA record of `::` for a name it controls. Squid then tries `[::]:443` or `[::]:80` inside the proxy netns. Nothing listens there: squid uses 3128/3140/3141/3150, and `Safe_ports` limits targets to 80/443. So today it reaches nothing (likely). Adding the CIDR keeps the IPv6 list consistent with the IPv4 rationale.

### Hyper-V layer drift detection (NIT, confirmed)

`scripts/hyperv-firewall.ps1` and the doctor PASS both find the two rules by name only. The script skips creation when a rule named `ossbot-out-https` exists, whatever its protocol, ports or action. The doctor passes when both names exist and both defaults are Block. Neither lists the other Hyper-V rules for the WSL VMCreatorId, so a broader Allow rule from a WSL update, another tool or a manual change goes unnoticed. The row-30 surface probe covers only 853/tcp, 53/udp and ICMP. This layer is defense in depth below squid and `OSSBOT-OUT`, and it constrains only root, ossadmin and the proxy. Drift loses a secondary layer but does not open a hole, which makes this a NIT.

### Unit confinement depends on no reachable user manager (NIT, likely)

Every non-IP control on the service uids is a seccomp filter attached to the unit: `RestrictAddressFamilies`, the io_uring filter and `SystemCallArchitectures`. A process started by that uid's systemd user manager (`user@<uid>.service`) has none of them. A unit that can reach `/run/user/<uid>/systemd/private` or the user bus can run `systemd-run --user`. That spawns an unfiltered process that can open `AF_VSOCK` to 2:50002, which is P1-13 again.

Two things keep this closed today. `ProtectHome=yes` hides `/run/user`, and so would Hermes's planned `ProtectHome=tmpfs`. No service uid appears to have a user manager either: the spot-check shows managers only for uids 1000 and 1001. Nothing asserts either condition. `test_systemd_units.py` does not pin `ProtectHome`, and nothing checks linger for the five service uids. A Phase 2 unit with a different `ProtectHome`, or an install step that enables linger for `hermes`, would reopen the path with no test failing. The system-bus routes (`StartTransientUnit`, `run0`, `machinectl shell`) need polkit admin and stay closed.

### Row 7 and the research/Hermes key holders (NIT, confirmed)

Row 7's residual says only callers without secrets (the fetch/validate pools and the runner) hold categories with shared front ends. The research pool (DESIGN 10.3: Kiro key plus `web_public`) and Hermes (Kiro key, `web_public` on 3141) hold a strict superset. They can reach any public host directly and need no fronting. ADR-0005 approves that design. The row should name the control that actually protects the key: ADR-0017 keeps the key out of the agent's tool reach (`read`/`web_*` only, `/proc` denied), and sandboxd scans outputs for it. CR-002's planned `open` mode adds `agent-llm` to this set, so the Phase 2 design review needs this row to be right.

### Reviewer spot-check

One command checked the empty abstract-socket allowlist (row 38) for a silent false pass: `ss -H -xl` and `ss -H -xa` in the distro. Column 5 holds the local name, and abstract names start with `@`. Datagram `UNCONN` sockets show up under `-l`. All six abstract entries are `ESTAB` D-Bus/systemd connections. So the awk filter reads a real column, and an empty result does mean no abstract listener.

</details>

<details>
<summary>File map</summary>

- `scripts/lib/firewall.sh`: `disable_io_uring` writes `/etc/sysctl.d/60-ossbot.conf` and sets the live value (P1-14)
- `src/ossbot/ctl/render.py`, `src/ossbot/ctl/doctor.py`, `src/ossbot/watchdog/tick.py`: `io_uring_problems()` in `--check`, the doctor `io_uring disabled` check, the watchdog reason `firewall_drift:io_uring`
- `systemd/ossbot-gatekeeper.service`, `systemd/ossbot-sandboxd.service`: `SystemCallFilter=~io_uring_*` and `SystemCallErrorNumber=EPERM`
- `sandbox/proxy/squid.conf.j2`, `tests/unit/ctl/golden/squid.conf`: `acl manager` and `http_access deny manager` among the fast denies
- `tests/system/test_egress_surface.sh`: ctypes io_uring probe per unit, as root and as unrestricted hermes; the sysctl-0 control window; cache manager 403s; owner-limited ephemeral-UDP skip; abstract unix listener allowlist
- `tests/unit/test_systemd_units.py`, `tests/unit/ctl/test_render.py`, `tests/unit/watchdog/test_tick_lifecycle.py`: filter lines, sysctl drift cases, manager deny order, watchdog reason
- `docs/VERIFICATION.md`, `docs/adr/0005-…`, `docs/adr/0007-…`, `docs/DESIGN.md`, `docs/PLAN.md`, `docs/plan/CR-002-…`: rows 26, 31 and 32 corrected, rows 37-41 added, P1-14 addenda, round-5 bar, CR-002 tracking

Full diff: `git -C d:\Work\autonomous-oss-contributor show 7288d2d`

</details>
