# Phase 1 review (round 5): egress and DNS surface

Round 5 reviews commit cb9c273, the fix for round 4's P1-13 (`AF_VSOCK` to the Windows host) and its four NITs. It also re-reads the full 36-row threat table in `docs/VERIFICATION.md` against squid's ACL semantics, every DNS path, IPv6, ICMP, raw IP, the Hyper-V layer and the non-IP host channels. The four NITs are closed, and no IP or DNS row (1-30) has a remaining hole. P1-13 itself is not closed. The fix filters `socket(2)` through seccomp, and io_uring creates sockets without making that syscall.

Watch for (confirmed): in the exact unit context the surface test uses (the unit's user, its `RestrictAddressFamilies`, `NoNewPrivileges`, `SystemCallArchitectures=native`), `socket(AF_VSOCK)` gets errno 97. An `IORING_OP_SOCKET` submission for `AF_VSOCK` returns a working fd, though, and `connect((2, 50002))` on it reaches the drvfs Plan 9 server. This was checked for the gatekeeper and ossbox contexts. The WSL kernel is 6.18 with `kernel.io_uring_disabled = 0`, and no unit filters the io_uring syscalls. Threat-table row 31 and the ADR-0005/0007 addenda describe the path as closed for units. The surface probe only calls `socket()`, which is why it passes.

**Verdict**: NEEDS_CHANGES

## High-level view

No IP-level finding is left. The squid ACL order, `-n` on every name ACL, the per-uid owner chains (v4 and v6), the internal task networks, the `OSSBOT-IN` drops and the Hyper-V 80/443 rule leave no service uid or sandbox an IP or DNS path past the proxy. The proxy resolves only names its caller may reach. A TTL-0 rebinding does not get past `denied_dst`: squid's ipcache raises every positive TTL to at least `negative_dns_ttl`, so the connect uses the answer the ACL checked (likely, from squid's `ipcache` TTL clamp).

The non-IP layer is the one gap. `RestrictAddressFamilies=` is a seccomp filter on `socket()`, and on kernels with `IORING_OP_SOCKET` (5.19+) io_uring creates sockets without that syscall. With io_uring enabled for everyone and not filtered in the units, every confined unit, including the Phase 2 runner and hermes units that will process untrusted content, can still open Hyper-V sockets to WSL init, the drvfs Plan 9 server and the crash-dump collector. The fix is to turn io_uring off system-wide and check that as drift, filter its syscalls in each unit, and add an io_uring variant of the vsock probe so the evidence covers the bypass. Containers are likely already covered, because Docker's default seccomp profile refuses the io_uring syscalls, but no probe shows it (row 32).

The rest is hardening. The listener allowlist sees only TCP/UDP sockets (confirmed), so abstract unix sockets in the host netns and UDP listeners on ephemeral ports get no check, even though every service uid can reach them. The squid config has no `manager` deny (confirmed), so the allow lines decide cache manager requests, and the broadest of them is the `web_public` callers' `all` rule.

<details>
<summary>Issues (3)</summary>

1. **P1-14 HIGH: io_uring bypasses `RestrictAddressFamilies`, so `AF_VSOCK` to the Windows host stays open from every unit**: set `kernel.io_uring_disabled=2` through a sysctl.d file and fail closed on drift (`ossbot-firewall --check` / watchdog, doctor). Add `SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register` to every non-root unit and assert it in `tests/unit/test_systemd_units.py` next to the address-family line. Extend the unit-context and per-pool vsock probes in `test_egress_surface.sh` with an `IORING_OP_SOCKET` attempt, Correct threat-table row 31 and the ADR-0005/0007 addenda, and add the io_uring evidence to row 32.
2. **NIT listener allowlist covers TCP/UDP only**: enumerate abstract unix sockets in the host netns (`ss -xl`, names starting `@`) against an allowlist, and limit the ephemeral-UDP skip to sockets owned by known clients (`ss -p`), since every service uid can reach both.
3. **NIT no cache-manager deny in squid.conf**: add `acl manager url_regex -i ^cache_object:// /squid-internal-mgr/` with `http_access deny manager` among the fast denies, as the stock squid.conf does, so cache manager pages (ipcache, active requests of other callers) are never decided by the `web_public` `all` allow lines.

</details>

<details>
<summary>Details</summary>

### io_uring sockets bypass the unit's address-family filter (P1-14, HIGH, confirmed)

systemd implements `RestrictAddressFamilies=` as a seccomp filter on the `socket()` syscall's family argument. `IORING_OP_SOCKET` (kernel 5.19+) creates the socket in the kernel from an io_uring submission. No `socket()` syscall is made, so seccomp never sees the family. LSM hooks still run, but no LSM rule restricts vsock here. The WSL kernel is `6.18.54.1-microsoft-standard-WSL2` and `/proc/sys/kernel/io_uring_disabled` is `0`. Neither the units nor DESIGN 7.5's hardening list filter `io_uring_setup`/`io_uring_enter`, and `git grep io_uring` finds nothing in the repo.

Reviewer spot-check (one command). The probe is a ctypes io_uring client in a `systemd-run` transient unit with the same properties `test_egress_surface.sh` uses (`User=<unit user>`, `RestrictAddressFamilies=<the unit's value>`, `NoNewPrivileges=yes`, `SystemCallArchitectures=native`):

```
== ossbot-gatekeeper user=gatekeeper RAF=[AF_INET AF_INET6 AF_UNIX]
plain socket(AF_VSOCK): errno97
io_uring IORING_OP_SOCKET(AF_VSOCK): created fd 7
connect 2:50002: open
== ossbot-sandboxd user=ossbox RAF=[AF_INET AF_INET6 AF_UNIX]
plain socket(AF_VSOCK): errno97
io_uring IORING_OP_SOCKET(AF_VSOCK): created fd 7
connect 2:50002: open
```

The fd is an ordinary vsock socket. `connect()` is not filtered, and 2:50002 is the drvfs Plan 9 server that ADR-0007's new addendum identifies. That server accepts an attach for any Windows path the user can reach. The probe stopped at connect: no Tversion, no attach. The impact is the one P1-13 described, for every confined unit. The gatekeeper holds the GitHub PATs and the Telegram token, and sandboxd drives Docker. In Phase 2 the runner and hermes join them, and those two read attacker-controlled content. Threat-table row 31, the ADR-0005 decision ("so no `AF_VSOCK`") and the ADR-0007 decision all describe the path as closed for units. `test_egress_surface.sh` tests only `socket()`, so it passes against the bypass.

Turning io_uring off system-wide is the robust fix: `kernel.io_uring_disabled=2` (kernel 6.6+, refuses `io_uring_setup` for every process, root included). Nothing in the stack is likely to need io_uring: dockerd, containerd, runc, squid and the Python services use epoll. The Phase 1 bar after the change confirms this. The sysctl belongs with the other boot-time invariants. A sysctl.d file applies it, and the drift path (`ossbot-firewall --check`, watchdog `firewall_drift`, doctor) fails closed if it reads anything other than 2, like the masked-resolved check from P1-6. `SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register` on each non-root unit covers the unit even if the sysctl is lost, and `test_systemd_units.py` should assert it next to the address-family line so the Phase 2 units inherit it. The surface probe needs the io_uring path in the unit context, and also in each pool and in the proxy. Docker's default seccomp profile has refused the io_uring syscalls since moby 25 (likely, Docker 29.9.1 here), but nothing records that today. The control that makes the refusal evidence is the same as now: an unrestricted session, with io_uring enabled, reaches 2:50002 through `IORING_OP_SOCKET`.

The IP layer is not bypassed. A socket created through io_uring carries the submitter's credentials, so the `OSSBOT-OUT` owner match still applies (likely, not traced). With the sysctl at 2 this stops mattering.

### Listener allowlist coverage (NIT, confirmed gap in the probe)

Row 26's allowlist is built from `ss -ltnu`, so it covers TCP and UDP only. Abstract unix sockets (`@name`) in the host network namespace have no file permissions: any uid in the netns can connect to them, every service uid included, and the probe never enumerates them. The ephemeral-UDP skip also means a UDP listener on a port at or above `ip_local_port_range` passes whoever owns it. Neither case is a known hole today. Both are the same "new root listener reachable by service uids" class that NIT 2 turned into a test failure for TCP. A check of `ss -Hxl` names starting with `@` against an allowlist, plus `ss -p` owner checks for the skipped UDP sockets, closes the coverage gap.

### Cache manager is decided by the allow lines (NIT, likely)

The config has no `manager` ACL (confirmed). Stock squid.conf puts `http_access deny manager` ahead of every allow for one reason: otherwise a cache manager request (`cache_object://` URLs or `/squid-internal-mgr/` paths) is allowed by whichever allow line matches its host. Here the `web_public` callers (hermes 3141, research pool) have `all !denied_dst`. Whether one of the URL forms gets past `Safe_ports` on this squid version is not traced. The internal-path form needs the first `http_port` (3128), which `Safe_ports` refuses. The exposure is read-only: public actions such as `ipcache`, `fqdncache` and `active_requests` would show which hosts other callers resolve and use. Protected actions need `cachemgr_passwd`, which is unset. One fast deny line removes the question.

</details>

<details>
<summary>File map</summary>

- `systemd/ossbot-gatekeeper.service`, `systemd/ossbot-sandboxd.service`: `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` (P1-13; bypassed through io_uring, P1-14)
- `tests/unit/test_systemd_units.py`: address-family line asserted for every non-root unit (needs the io_uring filter assertion)
- `tests/system/test_egress_surface.sh`: unit-context and per-pool vsock probes (`socket()` only), proxy seccomp check, `/run/WSL` checks, listener allowlist (TCP/UDP only)
- `scripts/lib/wsl.sh`, `src/ossbot/ctl/doctor.py`: `/run/WSL` 0700 in `ossbot-nointerop`, `wsl.sh check`, and doctor `interop sockets`
- `config/egress.yaml`, `tests/unit/ctl/golden/squid.conf`, `tests/unit/ctl/test_render.py`: four special-purpose ranges in `denied_dst`
- `tests/system/lib.sh`: `skip` counted separately; `tests/system/test_audit_anchor.sh`: watchdog timer held around the tick-10 check
- `docs/VERIFICATION.md`, `docs/adr/0005-…`, `docs/adr/0007-…`, `docs/DESIGN.md`, `docs/PLAN.md`: rows 31-36, the P1-13 addenda and the round-4 bar (row 31 and both addenda overstate the closure; row 32 lacks io_uring evidence)

Full diff: `git -C d:\Work\autonomous-oss-contributor show cb9c273`

</details>
