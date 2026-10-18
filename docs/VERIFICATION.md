# Verification log

Results of the phase verification bars (docs/PLAN.md) and of the live checks of docs/plan/NOTE-live-secrets.md. Newest first. Secret values are never recorded here, only key names and outcomes.

## Phase 1, after review round 3 (2026-10-18)

Fixes for `docs/review-phase1.json` round 3, made in one systematic pass over the whole egress/DNS surface (threat table below) instead of only the two named holes:

- P1-11: `-n` on every squid name ACL (`dstdomain -n` for the categories and `denied_domains`). New `ip_literal` ACL (`dstdom_regex -n -i`, every numeric IPv4 form and IPv6) with `http_access deny ip_literal` ahead of every allow on every port, `web_public` callers included.
- P1-12: the global `deny denied_dst` is gone. The resolved-IP deny is now the last ACL of each allow line (`allow port_X <src> <cat|all> !denied_dst`), so squid resolves only names that already matched the caller's port, source and category. Every deny line uses fast ACLs only.
- Same pass: `0.0.0.0/8` added to `always_denied.cidrs`, and the renderer refuses non-literal CIDRs. `pinger_enable off`, `visible_hostname ossbot-proxy`. New ip6tables `OSSBOT-IN` (`-i docker0` / `-i br-+` DROP at `INPUT` position 1, enforced by `--check`). Doctor's "proxy denies" check also sends an IP-literal CONNECT.
- Evidence: render tests in `tests/unit/ctl/test_render.py` and the new `tests/system/test_egress_surface.sh` (100 checks). Against the round-3 config, the same script failed exactly on the two findings: 2 proxy DNS packets per denied random label on 3140, 3150, 3128 and the fetch and llm pools, 14 PTR packets for the IP literals, and `web_public` IP literals allowed. After the fix all its counters are 0. ADR-0005 addendum, DESIGN §11.3/§11.4/§22.6.
- An allowlist-only resolver for squid was considered and not adopted. squid has one resolver for every listener, and `web_public` (research pool, hermes 3141) must resolve any name (DESIGN §11.3), so it would need a second proxy instance. The ACL order gives the confined callers the same property, measured by the meter below (ADR-0005 addendum).

### Egress threat table

"Meter" is the raw-table chain in `test_egress_surface.sh`. It counts the proxy's packets to its resolver (`10.212.0.2 -> <NS>:53`, UDP and TCP) that contain a fresh random label, plus every `in-addr.arpa` / `ip6.arpa` query. Its control is hermes's `web_public` lookup of its own label, which must be counted (2 packets). `surface` = `tests/system/test_egress_surface.sh`, `per_uid` = `test_egress_per_uid.sh`, `proxy_dns` = `test_proxy_dns.sh`, `profiles` = `tests/sandbox/test_profiles.py`, `render` = `tests/unit/ctl/test_render.py`.

| # | Path | Control that blocks it | Test proving it |
|---|---|---|---|
| **squid ACL semantics** | | | |
| 1 | IP-literal CONNECT matched to a category through the IP's PTR name (P1-11) | `-n` on every `dstdomain`/`dstdom_regex`; `deny ip_literal` before the allows | render `test_squid_name_acls_never_reverse_lookup` (only lookup ACL left = `denied_dst`); surface: IPv4/IPv6 literal 403 on 3140, 3150, 3128 host side and the fetch/research/llm pools, meter PTR v4 = 0 and v6 = 0; offline pinned-image repro: `dstdomain` 503 (tunnel attempted), `-n` 403 `HIER_NONE` |
| 2 | squid resolves a name no rule allows: DNS exfiltration through 403'd requests (P1-12) | no lookup ACL on any deny line; `!denied_dst` last on each allow line | render `test_squid_no_lookup_before_a_category_match`; surface: random-label CONNECT 403 and meter = 0 on 3140, 3150, 3128 host side, pool fetch, pool llm |
| 3 | IP-literal targets for `web_public` callers (research, hermes), incl. decimal/hex/octal forms | `deny ip_literal` (squid canonicalizes numeric hosts first) | render `test_ip_literal_patterns_match_literals_only` (14 literal forms, 10 look-alike names); surface: hermes 203.0.113.141, 3405803917, [2001:db8::3141] all 403; pool research IPv4/IPv6 literal 403; doctor "proxy denies" |
| 4 | Allowed or `web_public` name that resolves to loopback, RFC1918, link-local, CGNAT or `0.0.0.0` (rebinding) | `!denied_dst` on every allow line; `0.0.0.0/8` added | render (every allow line ends `!denied_dst`); surface: hermes `localtest.me` (127.0.0.1) 403 |
| 5 | Denied name spelled differently (case, trailing dot) | squid lowercases and strips the trailing dot before ACLs; `denied_domains` is `-n` | surface: raw CONNECT `gist.github.com.`, `GIST.GitHub.COM`, `x.github.io.` via hermes 403 |
| 6 | CONNECT to a port other than 443, or HTTP to a port other than 80/443 | `deny CONNECT !SSL_ports`, `deny !Safe_ports` (fast) | surface: raw CONNECT `example.com:8443` and `:22` 403 |
| 7 | SNI / Host header differing from the CONNECT host (domain fronting) | no TLS interception: squid tunnels only to the CONNECT host's resolved IP, and the inner SNI/Host cannot change that IP. Plain HTTP uses the URL host for both the ACL and the connection | surface: live squid.conf has no `ssl_bump`/`https_port`/`sslcrtd_program`. **Residual (accepted, ADR-0005 alternatives):** a front end shared by an allowed host can serve other tenants (e.g. `storage.googleapis.com` in `pkg_go`). Only callers without secrets (fetch/validate pools, runner) hold such categories; the llm pool has only `kiro_core` |
| 8 | Caller picks another caller's class | port by `myportname`, host ports need `src 10.212.0.1/32`; 3128 refuses the host side; pools by source /18; unknown subnet: `deny all` | per_uid (uid × port matrix); surface: root via 3128 403; proxy_dns: unknown-pool subnet 403 |
| 9 | Sandbox reaches the host-caller ports 3140/3141/3150 | those listeners bind `10.212.0.2` only; the task networks have no route to it | profiles (`alias_ports`, `host_ports`); surface: `errno101` on all three, per pool |
| **DNS paths** | | | |
| 10 | Service uid queries a resolver directly (WSL `10.255.255.254` on `lo`, `127.0.0.53`, any address) | `OSSBOT-OUT`: UDP/TCP 53 REJECT before the `lo` ACCEPT (P1-1) | per_uid: UDP+TCP 53 to every resolv.conf nameserver and 127.0.0.53, all five uids, root control answers |
| 11 | systemd-resolved varlink / D-Bus `ResolveHostname` (queries upstream as `systemd-resolve`) | unit masked; `ossbot-firewall --check` fails with `firewall_drift:resolved_ipc` (P1-6) | per_uid: varlink + `busctl` refused for all five uids; doctor `systemd-resolved masked` |
| 12 | nss: a caching or forwarding resolver or nss module that answers other uids (nscd, sssd, mdns, resolve) | `hosts: files dns` only; none of nscd, unscd, sssd, dnsmasq, unbound, bind9, avahi-daemon, libnss-resolve, libnss-mdns installed | surface (nsswitch line and 9 package checks) |
| 13 | squid's own resolver | `dns_nameservers <NS>` from resolv.conf only; queries only for allowed names (rows 1-2); `visible_hostname` set, so no hostname lookup at start | surface (live conf lines; meter rows 1-2) |
| 14 | Spoofed source `10.212.0.2` to use the proxy's DNS ACCEPT in `OSSBOT-IN` | no container can build raw packets (`--cap-drop ALL`, filter-enforced; proxy CapEff 0) | surface: raw socket `errno1` per pool, proxy CapEff `0000000000000000`; proxy_dns (ACCEPT rule names only the resolv.conf NS) |
| 15 | Docker embedded DNS (127.0.0.11) in a sandbox | `--internal` networks get no external forwarding (moby CVE-2024-29018 fix) and the filter forces `Dns == ["127.0.0.1"]` | proxy_dns (sandbox cannot resolve example.com); profiles (`resolves_example` false, `dns_txt_embedded` empty/no_reply); `tests/unit/dockerfilter` deny corpus `dns_other`, `dns_missing` |
| 16 | Sandbox sends to the WSL resolver directly | no default route on internal networks; `OSSBOT-IN -s 10.213.0.0/16 -j DROP` | surface: `resolver53 errno101`, `defaultroute=no` per pool |
| 17 | Docker embedded DNS in the proxy container | forwards from the proxy netns, so only squid (uid 13) and root's `docker exec` can use it | by construction (single-process container); doctor `proxy DNS` uses it as root |
| 18 | Root daemons acting for other uids: snapd (`/run/snapd.socket` 0666), PackageKit (D-Bus, polkit), unattended-upgrades, timesyncd | none take a caller-chosen host name or destination. snapd queries only the Snap Store and polkit gates installs; PackageKit uses the apt sources. Not a channel to an attacker-chosen endpoint | documented residual, no probe. Masking snapd/PackageKit is a possible later hardening; no snaps are installed |
| **IPv6** | | | |
| 19 | Service uid IPv6 egress | ip6tables `OSSBOT-OUT` (DNS DROP, `lo` ACCEPT, DROP); no global IPv6 route | surface: TCP to `[2606:4700:4700::1111]:443` refused for all five uids; `ip -6 route show default` empty |
| 20 | Container IPv6 (egress, or to a host service over a bridge link-local) | Docker `ip6tables: false`, non-IPv6 networks, so container interfaces have no IPv6 address; ip6tables `OSSBOT-FWD` drops forwarding on the bridges; **new** ip6tables `OSSBOT-IN` drops `-i docker0`/`-i br-+` | surface: `v6ifaces=none`, IPv6 direct `errno101` per pool, INPUT jump at position 1, proxy netns IPv6 only on `lo`; render golden + `test_parts_split_host_and_docker`; `ossbot-firewall --check` |
| 21 | squid connects to an AAAA answer | the proxy has no IPv6 address or route; `denied_dst` also lists `::1`, `fc00::/7`, `fe80::/10` | surface (proxy netns) |
| **ICMP** | | | |
| 22 | Service uid ICMP (ping sockets, tunnels) | `ping_group_range = 1 0`, no `CAP_NET_RAW`, and the final `OSSBOT-OUT` REJECT covers every protocol | surface: ICMP echo to 1.1.1.1 gets no reply, all five uids |
| 23 | Sandbox ICMP | no default route; `OSSBOT-FWD` drops `10.213/16 -> !10.213/16`; raw sockets refused | surface: ICMP `errno101`, raw `errno1` per pool |
| 24 | squid's ICMP pinger (setuid helper) | `pinger_enable off`; the container has no capabilities | surface (live conf, CapEff) |
| **Raw IP connections** | | | |
| 25 | Service uid TCP/UDP straight to an internet IP | `OSSBOT-OUT` final REJECT per uid | per_uid: 1.1.1.1:443 refused, 1.1.1.1:53/udp no answer, all five uids |
| 26 | Service uid to a host-local service over `lo` (allowed by design) | the only listeners are loopback and the WSL resolver, and port 53 is refused (row 10) | surface: no TCP/UDP service on a non-loopback address (ephemeral UDP client sockets excluded). **Residual:** a future loopback service would be reachable by service uids (the `lo` ACCEPT) |
| 27 | Sandbox straight to an internet IP | `--internal` network (no default route) + `OSSBOT-FWD` DROP | surface `direct443` per pool; profiles `noproxy_example`, `kiro_direct` |
| 28 | Sandbox to host services, or to a sandbox on another task network | `OSSBOT-IN -s 10.213.0.0/16 -j DROP` (IPv4), ip6tables `OSSBOT-IN` (IPv6); proxy `ip_forward=0` | profiles (`host_ports`, `peer`); surface (proxy netns `ip_forward` 0) |
| 29 | Proxy container to host services | `OSSBOT-IN -s 10.212.0.0/24 --ctstate NEW -j DROP`, with only the DNS ACCEPT above it | render `test_dns_accepts_precede_the_drops`; `ossbot-firewall --check` |
| **Hyper-V firewall layer** | | | |
| 30 | Anything in the VM (root, ossadmin, the proxy) to a non-80/443 port | Hyper-V VM setting: inbound Block, outbound Block, allow TCP 80/443 (`scripts/hyperv-firewall.ps1`). DNS for root and the proxy uses the WSL DNS tunnel, not the VM NIC, so rows 10-17 carry DNS control | surface: root to 1.1.1.1:853/tcp, 1.1.1.1:53/udp and ICMP blocked, 1.1.1.1:443 allowed (SKIP when the optional layer is not applied); `scripts\ossctl.ps1 doctor` `hyper-v firewall` PASS |

### Phase 1 bar (run in this step, 2026-10-18, about 10:00-10:30 +07)

| Phase 1 bar item | Result |
|---|---|
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports` | clean / 177 files formatted / 101 files no issues / 4 contracts kept |
| `ROOT shellcheck -x` provision.sh, scripts/lib, setup-secrets.sh, scripts/bin, entrypoints, fixtures, tests/system | clean |
| `DEV pytest -m "unit or telegram"` | 1547 passed, 11 deselected (new: 6 squid render tests; re-run after the last docs edit, same result) |
| `DEV pytest -m "acp or e2e"` | no tests collected yet (both suites are Phase 2: FEAT-006/FEAT-007) |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key` live Kiro probes, Phase 2) |
| `ROOT bash tests/system/*.sh` (11 scripts, final run after every change) | all exit 0, 313 PASS, 0 FAIL: audit_anchor 11, config_write 11, control_perms 23, crash_safety 10, docker_filter 10, egress_per_uid 71, egress_surface 100, firewall_failclosed 11, lifecycle 25, proxy_dns 5, secret_perms 36. An earlier full run, before rows 5-7 and the ephemeral-UDP rule of row 26 were added to egress_surface, gave 306 PASS / 0 FAIL |
| `tests\system\test_kill.ps1` | all checks passed |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13 |
| `scripts\bootstrap.ps1` | deploy run: `app-deploy` (3 items), `firewall` (applied host part: ip6tables `OSSBOT-IN`), `enable-start` changed. The run after the docs edit changed only `app-deploy` (DESIGN.md is deployed). The next run: every step `unchanged` (kiro/hermes `skipped (phase 2)`, Hyper-V WARN not elevated) |
| `ROOT docker version` / `docker compose version` | 29.9.1 / 5.6.0 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer, boot-gate, firewall, firewall-docker | all active |
| `ossbot-firewall --check` | exit 0 |
| `ossctl doctor` (root, full) | PASS 29, WARN 2, SKIP 3, exit 0; "proxy denies": 3150 example.com 403, 3141 203.0.113.7 403 |
| `scripts\ossctl.ps1 doctor` | exit 0; Windows part all PASS incl. `hyper-v firewall: inbound Block, outbound Block + TCP 80/443`; distro part as above |
| `ossctl audit verify` | ok (310 rows, 79 anchors) |
| `gitleaks git --redact .` | no leaks |

WARNs and SKIPs are unchanged from round 2: the runner-owned catalog (FEAT-006), safety probes not run yet (Kiro jobs refused until they pass), and the three Hermes checks (FEAT-007). Live traffic in this step: CONNECT probes to example.com, pypi.org, app.kiro.dev (no key), api.telegram.org and api.github.com/github.com (GET only, via the existing per-uid test). There were no GitHub writes. Telegram saw only the gatekeeper's normal polling and whatever kill/lifecycle FYIs `test_kill.ps1` and `test_lifecycle.sh` send, all to the configured user ID. Dry-run stayed ON. The watchdog timer was stopped for the deploy (app-deploy lands the new firewall renderer one step before `firewall` applies it) and started again by `enable-start`. No kill or pause happened.

## Phase 1, after review round 2 (2026-10-18)

Fixes for `docs/review-phase1.json` round 2: P1-6 (systemd-resolved masked; `ossbot-firewall --check` fails with `firewall_drift:resolved_ipc` while it is unmasked or listening; per-uid varlink + D-Bus probes; ADR-0005 addendum), P1-7 (`request_resume()` invalidates armed/confirmed while a kill flag exists; reconcile invalidates whenever it reads new offline `resume.log` lines), P1-8 (watchdog exempts only lifecycle `down` scope `distro`; scope `workers` keeps the health check and restarts the core units, never the stopped workers), P1-9 (`lifecycle.drain_timeout_s` capped at 120 to match the static unit/`down.ps1` values; longer drains per run via `--timeout`/`-Timeout`), P1-10 (app-deploy rsync with `--checksum --omit-dir-times`, itemized lines in the provision log, metadata-only items no longer trigger `uv sync` or restarts).

| Phase 1 bar item | Result |
|---|---|
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports`; `ROOT shellcheck` (changed scripts) | clean / 177 formatted / 101 files no issues / 4 contracts kept / clean |
| `DEV pytest -m "unit or telegram"` | 1541 passed, 11 deselected |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key`, Phase 2) |
| `ROOT bash tests/system/*.sh` (10 scripts) | all exit 0, 213 PASS, 0 FAIL; `test_egress_per_uid.sh` 71 PASS incl. varlink + D-Bus `ResolveHostname` refused for all five service uids, resolved masked and inactive |
| `tests\system\test_kill.ps1` | all checks passed |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13 |
| `scripts\bootstrap.ps1` | first run: `app-deploy` (6 content items, itemized), `firewall` (masked systemd-resolved), `enable-start` changed; next run: every step `unchanged`. A touched source file now logs `.f..t...... <path>` and "metadata-only, no content change" with the step `unchanged` |
| `ROOT docker version` / `docker compose version` | 29.9.1 / v5.6.0 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer | all active |
| `ossbot-firewall --check` | 0 |
| `ossctl doctor` (root, full) / `scripts\ossctl.ps1 doctor` | PASS 29, WARN 2, SKIP 3, exit 0 (new: `systemd-resolved masked` PASS; per-uid egress "no resolved IPC") |
| `ossctl audit verify` | ok |
| `gitleaks git --redact .` | no leaks |

## Phase 1, after review round 1 (2026-10-18)

Fixes for `docs/review-phase1.json`: P1-1 (per-uid DNS refusal), P1-2 (kill invalidation on every kill path), P1-3 (CR-001 Phase 1 share, DESIGN §13.5), P1-4 (recorded for FEAT-005), P1-5 (exact token/callback regexes).

| Phase 1 bar item | Result |
|---|---|
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports`; `ROOT shellcheck` (scripts, bins, system tests) | clean / formatted / 101 files no issues / 4 contracts kept / clean |
| `DEV pytest -m "unit or telegram"` | 1530 passed (new: lifecycle, drain, crash recovery, polling reconnect, offline bot start, systemd stop timeouts, watchdog sleep, DNS render, reconcile kill invalidation, exact regexes) |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key`: Kiro host CONNECT, S-ws-1; live Kiro probes belong to Phase 2), incl. `test_crash_safety.py` (SIGKILL sandboxd mid-job, recover) |
| `ROOT bash tests/system/*.sh` (10 scripts, new: `test_lifecycle.sh`, `test_crash_safety.sh`) | all exit 0, 199 PASS, 0 FAIL; `test_egress_per_uid.sh` now probes UDP+TCP 53 to every resolver per uid (all refused, root control answers) |
| `tests\system\test_kill.ps1` | all checks passed (hung kill 37.4 s after the proxy `ExecStop` went from 5 s to 2 s) |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13 |
| second `scripts\bootstrap.ps1` | every step `unchanged` (kiro/hermes `skipped (phase 2)`) |
| `ROOT docker version` / `docker compose version` | 29.9.1 / v5.6.0 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer | all active |
| `ossbot-firewall --check` | 0 |
| `ossctl doctor` (root, full) | PASS 28, WARN 2, SKIP 3, exit 0 |
| `scripts\ossctl.ps1 doctor` | Windows part all PASS (Hyper-V firewall now applied), distro part as above |
| `ossctl audit verify` | ok |
| `gitleaks git --redact .` | no leaks |
| CR-001 live cycle: `down.ps1 -Timeout 10` → `status.ps1` (distro stopped, not started) → `up.ps1` → `status.ps1` | down 8 s (summary delivered, nothing interrupted), last shutdown `graceful`, up 5 s with doctor --quick clean |

Doctor WARNs (documented): "sandbox Kiro auth + catalog" (the catalog is fetched by the runner, FEAT-006) and "safety probes: not run yet for the current tuple" (they need catalog models; Kiro jobs stay refused until they pass). SKIPs: the three Hermes checks (FEAT-007).

Live checks run in this step (NOTE-live-secrets.md, read-only):

- GitHub write token: `GET /user` = `Amsal1`, `X-OAuth-Scopes: public_repo` (doctor). No write call of any kind was made.
- GitHub read token: `GET /user` 200 (doctor). Reading a public repo with it is left to the FEAT-006 vetting checks.
- Telegram: `getMe` 200 (doctor); the gatekeeper polls live; the CR-001 lifecycle summaries ("Draining", "Going down", "Back up") and one offline-resume FYI went to the configured user only.
- Dry-run stayed ON throughout.

Incident during this step: adding `lifecycle:` to `config/policy.yaml` before deploying the new schema made the previously deployed gatekeeper refuse to start (config validation, fail closed); the watchdog paused the system (`gatekeeper_unhealthy`) as designed. After the deploy the pause was lifted with the offline resume (`ossbot-resume --offline`, logged in `resume.log`). The same deploy exposed that the gatekeeper's start failed whenever Telegram was unreachable (proxy restarting); the bot now connects in the background with backoff (CR-001 item 1).

### CR-001 items still open (tracked for Phase 2)

| CR-001 item | Owner | Status |
|---|---|---|
| 6a shutdown blocker (`WM_QUERYENDSESSION` + `ShutdownBlockReasonCreate` → `down.ps1 -Timeout 90`), verified live on this Windows 11 Home 26100 laptop (hidden window, auto-continue, Fast Startup), ADR | FEAT-008 | **open (required)** |
| 6b event-triggered task on System/User32 1074 → `down.ps1 -Timeout 20 -Kind hook` (non-elevated, no sleep trigger, SpiralPool BSD-3 credit if adapted) | FEAT-008 | **open (required)**; `down.ps1 -Kind hook` and the `hook` shutdown type already exist |
| `bootstrap.ps1` installs 6a/6b, `-Uninstall` removes them, `ossctl.ps1 doctor` checks them; optional `-Shortcuts` | FEAT-008 | open |
| Stage re-queue on `up` and the real `resumed` count; runner honours the lifecycle flag; SIGKILL-runner e2e | FEAT-006 | open (sandbox side done: jobs end `interrupted`, task volumes kept) |
| Telegram `/drain`, `/undrain`, `/down`, `/up` | FEAT-005 | open (API and ControlService methods exist) |
| README/SETUP "Daily use", AGENTS.md lifecycle invariants | FEAT-008 | open |
| Done in Phase 1 | | lifecycle states + API (kill overrides), sandboxd drain + crash recovery, `TimeoutStopSec`, `ossctl up/down/status`, `scripts/{up,down,status}.ps1`, boot-gate shutdown classification (graceful/hook/unclean), watchdog sleep handling, monotonic job/drain timeouts, Telegram reconnect tracking + offline start, crash-safety tests |
