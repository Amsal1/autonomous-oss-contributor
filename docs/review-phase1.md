# Phase 1 review (round 4): egress and DNS surface

Round 4 reviews commit 109711f, the fix for round 3's P1-11/P1-12. It also covers the whole egress and DNS surface as recorded in the new threat table in `docs/VERIFICATION.md`. Both round-3 findings are closed in the template, the golden, the render tests and the live meter evidence, and the same pass also closed IPv6 `INPUT` and `0.0.0.0/8`. The 30-row table misses one channel that none of its layers can see: `AF_VSOCK` (Hyper-V sockets) from the VM to the Windows host.

Watch for: any confined service uid can open hvsocket connections to the Windows host (confirmed: as `hermes`, `AF_VSOCK` connects to CID 2 on ports 50000, 50002 and 50005 succeed). iptables owner rules, the WSL NAT and the Hyper-V VM firewall all filter IP traffic on the vNIC, and this traffic is not IP. Nothing in the units restricts the socket family. What sits behind those ports (WSL service channels, possibly the drvfs Plan 9 server or the DNS tunnel) is not traced. Either would be an uncontrolled path from a confined uid to a host process that has full network and disk access.

**Verdict**: NEEDS_CHANGES

## High-level view

With every name ACL `-n`, IP literals denied ahead of every allow, and `!denied_dst` as the last term of each allow line, squid resolves a name only after the port, the source pool and the category have matched. The meter's positive control (hermes's `web_public` lookup must be counted) is what makes its zero counts evidence of P1-11/P1-12 closure.

Every DNS and IP row in the table has a control and a probe. The design assumes the network stack is the only way out of the VM, and on WSL2 it is not. `AF_VSOCK` reaches the host directly, it needs no capability, and the moby default seccomp profile blocks it only inside containers. The fix is cheap: `RestrictAddressFamilies=` on every non-root ossbot unit, a probe, and a threat-table row.

The remaining items are hardening. `denied_dst` lacks a few special-purpose ranges. The host-listener probe exempts every port on the resolver address and does not allowlist loopback listeners, even though every service uid may reach `lo`. The threat table does not record the attacker-tenant channel through allowed SaaS endpoints. A skipped Hyper-V check is counted as a PASS.

<details>
<summary>Issues (5)</summary>

1. **P1-13 HIGH: unfiltered `AF_VSOCK` path from confined uids to the Windows host**: add `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK` to every non-root ossbot unit (gatekeeper, sandboxd, and the Phase 2 runner/hermes templates; drop `AF_NETLINK` where it is not needed). Assert it in `tests/unit/test_systemd_units.py`, add a per-unit-context and per-pool vsock probe to `test_egress_surface.sh`, and add a threat-table row and an ADR-0005 addendum. Identify what listens on host ports 50000/50002/50005, and if one is the drvfs Plan 9 server, record what it means for ADR-0007.
2. **NIT `denied_dst` special-purpose ranges**: add `198.18.0.0/15` (the fake-IP range of common Windows VPN/proxy clients), `192.0.0.0/24`, `224.0.0.0/4` and `240.0.0.0/4` so a `web_public` name cannot point squid at them.
3. **NIT host-listener probe too permissive**: limit the resolver exemption to `<NS>:53` and turn the loopback check into an allowlist of the expected listeners. Every service uid reaches any `lo` port, so an unexpected loopback listener should fail the test, not pass it.
4. **NIT attacker-tenant channel not in the threat table**: add a row for "allowed SaaS host, attacker's own account" (llm pool `kiro_core` with the sandbox Kiro key, runner `github_read`, fetch `storage.googleapis.com`/`ghcr.io`). Mark it as an accepted residual of CONNECT-only matching and name the mitigations (key scope, rotation, no secrets in fetch).
5. **NIT Hyper-V SKIP counted as PASS**: `test_egress_surface.sh` reports the unapplied-layer case through `pass "SKIP …"`, so "313 PASS" does not show whether row 30 ran enforced. Count SKIPs separately in `lib.sh` (or record the row-30 outcome in VERIFICATION.md).

</details>

<details>
<summary>Details</summary>

### Hyper-V sockets bypass every egress layer (P1-13, HIGH, confirmed reachability)

The threat table's rows assume that a confined process can only leave the VM over IP: owner rules in `OSSBOT-OUT` (IPv4 and IPv6), the `lo` ACCEPT with port 53 refused, the Hyper-V VM firewall on the vNIC. WSL2 also gives the guest Hyper-V sockets to the host (`AF_VSOCK`, host CID 2). This is how WSL's init, interop, drvfs and DNS tunnelling talk to the Windows side. netfilter never sees these sockets, the Hyper-V firewall filters only the virtual NIC, and an unprivileged process can create them. ADR-0007 already notes that the interop handler "failed on its vsock", so the transport is live in this distro. Reviewer spot-check, one command, as a confined service uid:

```
wsl -d ossbot -u root -- runuser -u hermes -- python3 -I -c '... socket(AF_VSOCK).connect((2, p)) for p in 50000..50010'
{50000: 'OPEN', 50001: 'timeout', 50002: 'OPEN', 50003: 'timeout', 50004: 'timeout',
 50005: 'OPEN', 50006..50010: 'timeout'}
```

Three host listeners accept connections from `hermes`. The same holds for gatekeeper (GitHub PAT, Telegram token), sandboxd and the Phase 2 runner, because none of the units restricts address families (`git grep RestrictAddressFamilies` finds nothing). I did not trace which WSL services own these ports, and that sets the impact. Possible: a listener is WSL's drvfs Plan 9 server. Then a confined uid could attach Windows paths directly and get around ADR-0007's `automount=false` confinement. Possible: a listener carries the DNS tunnel. Then this is the P1-1/P1-6/P1-12 channel again, without port 53. In every case it is an unfiltered channel from an identity that reads untrusted content (hermes, the runner) to a host process outside every ossbot control. Even the cheapest reading of the impact gives an attack surface on a privileged Windows service.

Sandbox containers are likely not affected. The filter forces `Runtime: runc` and `SecurityOpt == ["no-new-privileges"]`, so the moby default seccomp profile applies, and that profile refuses `socket(AF_VSOCK)`. No probe shows this, though. The proxy container falls in the same "likely" bucket.

The fix is a systemd property on each non-root unit, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` (plus `AF_NETLINK` where glibc's `AI_ADDRCONFIG` needs it). The renderers and the Phase 2 unit templates for runner and hermes should carry it as an invariant, and `test_systemd_units.py` should assert it. The live check has to run in the unit's context, for example the unit's `Seccomp:` and the `systemctl show -p RestrictAddressFamilies` value, plus a vsock connect from a `systemd-run` transient unit with the same properties. A connect under plain `runuser` would still succeed and prove nothing. Add a pool-side vsock probe to `POOL_SCRIPT` as well. Sessions outside units (`runuser`, `wsl -u ossnobody`) remain unrestricted. They need root or the owner's console, and the threat table should record them as such.

### `denied_dst` range gaps (NIT)

`always_denied.cidrs` covers RFC1918, CGNAT, link-local, loopback and `0.0.0.0/8`. It does not cover `198.18.0.0/15`. Fake-IP VPN/proxy clients on Windows (Clash, Surge-style TUN modes) hand out addresses in that range, and the WSL NAT forwards to them. With such a client running on the laptop, a `web_public` name resolving into the range would reach the Windows client through squid. `192.0.0.0/24`, `224.0.0.0/4` and `240.0.0.0/4` are cheap to add alongside it. IPv4-mapped AAAA answers are already handled, because squid stores IPv4 as v4-mapped, so `::ffff:127.0.0.1` matches `127.0.0.0/8`.

### Host-listener probe (NIT)

The awk filter in the "host surface" block drops every socket whose address equals `$NS`, on any port. It also accepts any `127.*`/`::1` listener. `OSSBOT-OUT` lets every service uid reach any port on `lo` (row 26), so the security-relevant question is which loopback listeners exist. The probe never asks it. Exempt only `<NS>:53`, and fail on any loopback listener that is not on an explicit list. That turns row 26's "future loopback service" residual into a test failure instead of a silent change.

### Same-service, attacker-tenant channel (NIT, documentation)

Row 7 records domain fronting as accepted. The more direct variant is not recorded: an allowed host that serves every tenant. A compromised llm pool can call `q.*.amazonaws.com`/`runtime.*.kiro.dev` with an attacker's own Kiro token, the runner can write to `api.github.com` with any token it gets, and the fetch pool can upload to an attacker's `storage.googleapis.com` bucket or `ghcr.io`. CONNECT-only matching cannot tell these apart from legitimate use, and that is a deliberate ADR-0005 trade-off. The table should still list it, with its mitigations: the sandbox Kiro key's scope and rotation, and no secrets in fetch/validate.

### Hyper-V row evidence (NIT)

When the Hyper-V layer is not applied, row 30's probe emits `pass "SKIP …"`, so the bar's "313 PASS, 0 FAIL" cannot distinguish an enforced row 30 from a skipped one. `scripts\ossctl.ps1 doctor` reports the layer applied, so it most likely ran enforced. The evidence log should say so explicitly, or `lib.sh` should count SKIPs separately.

</details>

<details>
<summary>File map</summary>

- `sandbox/proxy/squid.conf.j2`, `tests/unit/ctl/golden/squid.conf`: `-n` name ACLs, `ip_literal` deny, `!denied_dst` last on each allow line, `pinger_enable off`, `visible_hostname` (P1-11/P1-12, closed)
- `src/ossbot/ctl/render.py`: `IP_LITERAL_PATTERNS`, literal-only CIDRs, ip6tables `OSSBOT-IN`
- `config/egress.yaml`: `0.0.0.0/8` added to `always_denied.cidrs` (NIT 2 extends it)
- `scripts/lib/firewall.sh`, `tests/unit/ctl/golden/iptables.rules`: ip6tables `INPUT` jump enforced by `--check`
- `tests/system/test_egress_surface.sh`: proxy-DNS meter and per-row probes (NITs 3 and 5; P1-13 probe goes here)
- `tests/unit/ctl/test_render.py`: squid render invariants
- `systemd/ossbot-gatekeeper.service`, `systemd/ossbot-sandboxd.service`, `tests/unit/test_systemd_units.py`: no `RestrictAddressFamilies` (P1-13)
- `docs/VERIFICATION.md`, `docs/adr/0005-sandbox-isolation-and-egress.md`, `docs/DESIGN.md`: threat table, addenda (P1-13 row and NIT 4 go here)

Full diff: `git -C d:\Work\autonomous-oss-contributor show 109711f`

</details>
