# Verification log

Results of the phase verification bars (docs/PLAN.md) and of the live checks of docs/plan/NOTE-live-secrets.md. Newest first. Secret values are never recorded here, only key names and outcomes.

## Phase 1 rework (FEAT-005, 2026-10-18)

Phase 1 brought to DESIGN revision 5, safety-relevant parts first (docs/plan/phase2-build-plan.md items 22-27). Dry-run stayed ON; no GitHub write of any kind; Telegram only through product code to the owner (the egress-mode FYIs).

### Item 22: hard diff ceiling
`policy.diff` is 80 files / 3000 non-test lines / 3000 test lines (config file only). `policy.check_diff(entries, policy)` splits the gatekeeper numstat with `size.test_globs` and drops `size.exclude_globs` from both line totals; every path counts toward `max_files` (decision B5). The per-target `max_changed_lines` is gone (schema rejects it); `size_class` is an autonomous preference only. Tests: `test_policy.py` (80/81, 3000/3001 code, 3000/3001 test edges, excluded lines, no target parameter), `test_size.py` (class edges, hypothesis split invariants), `test_config.py` (new blocks, validator), `test_ceiling_not_writable.py` (every token purpose, command, confirm handler and API route; only models.yaml is written under config/).

### Item 23: research egress and egress modes
- Data: `github_user_content` (`.github.io`, `gist.github.com`, `gist.githubusercontent.com`) for `research` and host caller `hermes` in every mode; base lists carry no `web_public`; `modes` grants it (allowlist: none; research: research + hermes; open: + agent-llm and catalog). `always_denied` keeps `uploads.github.com`, `checkpoint-api.hashicorp.com`, adds `telegra.ph`, `api.telegra.ph`, `edit.telegra.ph` and `::/128` (P1-15), with a why-comment per entry. `mode_default: open`.
- Renderer: every render renders all three modes and refuses if denied networks/names, IP-literal or DNS lines, fetch/validate or runner/gatekeeper lists differ; each context is asserted (fetch/validate/runner/gatekeeper never `web_public`, allowlist none, llm pool only in open, a floor of always-denied networks and the Telegraph names). Grantee keys are a closed `Literal` set (a grant to `fetch` fails validation).
- Switch: `POST /v1/control/egress-mode` (operator) / `ossctl egress mode <m>` (TTY confirm) writes `/var/lib/ossbot/control/egress-mode`, audits `egress_mode_changed`, FYIs; `ossbot-egress.path` (PathChanged) -> `ossbot-egress-apply` re-renders and restarts the proxy (decision B4), then re-attaches it to every `ossbot.sandbox=1` network. `ossbot-firewall --check` (host part) and the watchdog compare the live squid.conf with the render for the current mode (`firewall_drift:squid_mode`, 60 s grace after a flag write). Doctor, `/status` and `ossctl egress status` show the mode.
- Verified live: rename-into-place of the flag triggers the path unit and a sibling file in the control dir does not; `ossctl egress mode allowlist` (TTY confirm through a pty) -> apply `success`, running proxy rendered for allowlist, `ossbot-firewall --check` exit 0; back to `open` the same. `ROOTDEV pytest -m sandbox` 9 passed, 2 skipped (needs_key) in `open` and again in `allowlist`. `tests/system/test_egress_surface.sh`: all checks passed (every mode probed: hermes, research, llm and fetch pools, runner and gatekeeper ports).
- Incident during the first redeploy (fixed in the same item): the new `--check` could not parse the old config snapshot (schema change) and the watchdog soft-killed (`firewall_drift:squid_mode`) before the gatekeeper wrote a new snapshot. The check now treats a hash-verified snapshot that the installed schema rejects as "not verifiable yet" (the gatekeeper restart writes a new one, the proxy restart re-renders) instead of drift, and provision's firewall step no longer dies on that render (unit test `test_squid_drift_tolerates_a_snapshot_the_new_schema_rejects`). The soft kill was lifted with `ossbot-resume --offline --via feat005-redeploy` (resume.log line, FYI on the gatekeeper's next reconcile).

### Item 23a: Hermes capability-tier control plane
`hermes.capability_tier_default: self_improve`, fail-closed `hermes-tier` flag (`locked` on anything unusable), `TIER_TOOLSETS`/`ALWAYS_DISABLED` in `gatekeeper/chat/hermes_surface.py`, `POST/GET /v1/control/hermes-tier`, `ossctl hermes tier [<tier>]`, doctor `hermes tier` line; a switch invalidates open skill approvals (`tier_changed`). Tests: `test_hermes_surface.py`, `test_control_flags.py` (tier cases), `test_control.py` (audit, FYI, invalidation, operator-only API), `test_authz.py` route table; import-linter 5 contracts kept. Applying a tier to Hermes is FEAT-007 item 36.

### Item 24: token and approval core deltas 1-9, decision services
Migration `0002_decisions.sql` (`proposal_approvals`, `skill_approvals`, `tg_views`, `tg_thread_seq`, `tg_outbox.result_message_id/bind_tokens_json/view_json`). Card purposes per kind (view purposes multi-use), `disarm_expired` on all three services, `create(expires_at=)`, `reissue(part_count, keep_expiry)` with eligibility and the confirmed-sibling refusal, the `AppContext.reissue*` wrappers, the kill/reload fan-out, `Outbox.mark_sent(row, result)` binding in one transaction, `CardPublisher` through `ctx.decisions.for_id` with `recover()`, and the sweep reference at the window start (EXPIRED cancels pending actions). `DEV pytest -m "unit or telegram"`: 1873 passed, 11 deselected (every Phase 1 approval test unchanged except the two `reissue` call sites that now pass `part_count`). New tests: `test_card_purposes.py`, `test_disarm_expired.py`, `test_reissue_keeps_expiry.py`, `test_reissue_part_counts.py`, `test_reissue_eligibility.py`, `test_decision_fanout.py`, `test_card_recover.py` (a_/p_/s_ × four crash points), `test_outbox_bind_atomic.py`, `test_dedupe_keys.py`, `test_proposal_approvals.py`, `test_skill_approvals.py`, `test_reissue_wrappers_ast.py`, `test_migrations.py`. The Telegram cards and handlers of proposals and skills arrive with FEAT-006/007/009 (the services are reachable only from code today).

### Item 25: update handler budget, redaction choke point, P1-4
`Handlers.spawn`/`cancel`/session map with one idempotent done callback, `KILL_CANCELS`, `PREFLIGHT_TIMEOUT_S = 8.0` (Confirm preflight bounded, timeout = `live_check:timeout`), `HANDLER_SLOW_S = 1.0` timers (`handler_slow`), `/kill` and `/pause` cancel the cancellable kinds after their local effect, registry `effect: local|io` (io commands spawned). `gatekeeper/secret_scan.py` (`SecretScanner`, load refusals), `policy.check_text(..., secret_values=)` at every production caller, packaging scans the patch for the gatekeeper's values, `telegram/ui/redact.py` (`redact`, `scan`, `draft_visible`). P1-4: `set_text_override` refuses before storing. New tests: `tests/telegram/test_handlers_nonblocking.py` (/kill < 2 s with a 60 s fake stream, Approve < 2 s, no `handler_slow`, cancel before start, a held io command survives /kill), `test_secret_scan.py`, `test_redaction_paths.py`, `test_check_text_callers_ast.py`, `test_approvals.py::test_text_override_refused_before_storing`; `tests/fakes/fake_hermes.py`. The chat-turn persistence, renderer AST and dashboard parts of AC 15/59/66/67, U14, BA-15/16 are recorded in TRACEABILITY for FEAT-007/009.

### Egress threat table, FEAT-005 changes
Rows not listed are unchanged (table below, "Phase 1, after review round 5").

| # | Path | Control that blocks it | Test proving it |
|---|---|---|---|
| 5 | Denied name spelled differently (case, trailing dot) | squid lowercases and strips the trailing dot before ACLs; `denied_domains` is `-n` | surface: raw CONNECT `api.telegra.ph.`, `EDIT.Telegra.PH`, `telegra.ph` via hermes 403; `gist.github.com.`/`GIST.GitHub.COM`/`x.github.io.` are now allowed for hermes (github_user_content, 200) and 403 via runner/gatekeeper |
| 7 | SNI / Host header differing from the CONNECT host (domain fronting) | no TLS interception: squid tunnels only to the CONNECT host's resolved IP | surface: no `ssl_bump`/`https_port`/`sslcrtd_program`. **Residual (accepted, ADR-0005 alternatives, P1-18):** a front end shared by an allowed host can serve other tenants. The research pool and Hermes hold a Kiro key and have `web_public` (modes research and open), and egress mode `open` (the default) gives it to the llm pool (agent-llm, catalog) too, so for those callers fronting adds nothing: they already reach any public host. Controls: the key stays out of reach of attacker-controlled input (ADR-0017: key-holding containers mount no attacker-writable path, coder file I/O goes through the key-free execd; ADR-0018: Hermes runs only allowlisted toolsets) and sandboxd scans every output for it; fetch/validate/runner have no secret and no `web_public` in any mode |
| 36 | Allowed SaaS host, attacker's own account | none in the proxy: CONNECT-only matching cannot tell tenants apart (deliberate ADR-0005 trade-off). **P1-18 / CR-002:** the research pool and Hermes hold a Kiro key and `web_public`; in mode `open` the llm pool also does. Controls: ADR-0017 (no attacker-writable mounts in key-holding containers, execd) and ADR-0018 (Hermes toolset allowlist), output key scans (sandboxd, §10.6), the Kiro key-exfil probes (§10.9) stay mandatory and fail closed; fetch/validate/package hold no secrets; the runner has no GitHub write token | **accepted residual**, documented; render `test_mode_table_of_design_11_5`, `test_invariants_identical_in_every_mode`; surface "egress mode" rows |
| 42 | An egress mode (data or a renderer bug) widens a code-running profile or a trusted writer | grantee keys `research`/`hermes`/`agent-llm`/`catalog` only (pydantic `Literal`); renderer assertions per context and across all three modes | render `test_grant_to_a_code_running_profile_is_rejected_by_the_schema`, `test_renderer_refuses_web_public_in_base_lists_of_code_or_trusted_callers`, `test_renderer_refuses_a_hand_built_context_giving_fetch_web_public`, `test_check_mode_invariants_catches_a_grant_to_the_code_pool`; surface: fetch pool, runner and gatekeeper 403 for example.com and github.io in every mode |
| 43 | The running proxy does not match the selected mode (apply not run, hand edit, unparseable flag) | flag unparseable/oversize/symlink = `allowlist`; `--check` and the watchdog compare the live conf with the render for the flag's mode (soft kill `firewall_drift:squid_mode`); the apply restarts the container because the single-file bind mount keeps the old inode | `test_control_flags.py` (fail-closed cases), render `test_squid_drift_*`, watchdog `test_firewall_drift_reason[squid_mode]`; surface: each mode's running conf (`docker exec`) names the mode and `--check` passes |
| 44 | LLM callers publish through Telegraph (CR-003 U11) | `telegra.ph`, `api.telegra.ph`, `edit.telegra.ph` in `always_denied` (asserted by the renderer) | surface: 403 for hermes, runner, gatekeeper and every pool, in every mode; profiles `telegraph` fact 403 |

## Design consolidation, revision 5 (2026-10-18)
Docs and plan only; no product code changed and no test was run in this step. CR-001…CR-004, NOTE-live-secrets and the user's chat decisions were merged into `docs/DESIGN.md` (revision 5; CR-003 became §14.6–§14.25). `docs/TRACEABILITY.md` was created with one row per requirement and decision, and the Phase 1 code was audited against it (rows with status `rework`, handled first by FEAT-005 in `docs/PLAN.md`). From now on requirement status lives in TRACEABILITY.md; the "CR-001 items still open" and "CR-002 and CR-003 (tracked for Phase 2)" tables below are historical.

Consolidation review fix pass (same day, docs only, no test run): the findings of `docs/review-consolidation.json` (C-1…C-11) were addressed in DESIGN revision 5 (responses in DESIGN §27.0b), TRACEABILITY.md, PLAN.md and the FEAT files. A mechanical cross-check (every open row named by a PLAN item of each FEAT in its phase; every `rework` row has evidence) reports no problems.

## Phase 1, after review round 5 (2026-10-18)

Fixes for `docs/review-phase1.json` round 5: P1-14 (HIGH) and two NITs. Round 5 found no hole in rows 1-30 and confirmed round 4's four NITs closed, but P1-13 was still open through io_uring. This step's prompt still listed P1-11/P1-12 as the open findings. 109711f closed both, and rounds 4 and 5 confirmed it, so the fixes below follow the review file. The systematic pass this round covered every way to create a socket other than `socket(2)` (rows 39-41) and re-read rows 1-38 against the live system.

- P1-14 (HIGH): `kernel.io_uring_disabled = 2` (`/etc/sysctl.d/60-ossbot.conf`, provision step `firewall`) with a fail-closed drift check (`ossbot-firewall --check` -> `firewall_drift:io_uring`, watchdog reason of the same name, doctor `io_uring disabled`). Also `SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register` + `SystemCallErrorNumber=EPERM` on `ossbot-gatekeeper` and `ossbot-sandboxd`, asserted with `SystemCallArchitectures=native` for every non-root unit. An `IORING_OP_SOCKET` probe runs in every unit context, every pool and an unrestricted control (rows 39-40). Row 31 and the ADR-0005/0007 addenda are corrected, DESIGN 7.5/22.6 and PLAN updated.
- NIT (listener coverage): abstract unix listeners in the host netns are checked against an allowlist, which is empty (row 38). The ephemeral-UDP skip now applies only to sockets with no owner in this distro, i.e. the WSL VM's own root sockets (row 26). `ss -p` shows no owner even for the allowlisted `<NS>:53` and `:323` sockets: they live outside this distro's PID namespace. So the rule is "no visible owner and no uid", not a process-name list.
- NIT (cache manager): `acl manager` + `http_access deny manager` among the fast denies (row 37). squid 6 parses the redefinition without a warning (`squid -k parse` with the pinned image).
- Nothing in the stack needed io_uring: the full bar below ran with the sysctl at 2, and so did a `wsl --terminate` cold boot (`systemd-sysctl` sets it before any unit starts).
- CR-002 (Phase 2, not implemented here): egress stays data-driven. `always_denied`, `categories`, `sandbox_profiles` and `host_callers` are config-snapshot data, and the renderer has no per-profile special cases. The invariants CR-002 keeps in every mode (IP literals, `denied_dst`, vsock/io_uring, DNS, Hyper-V) do not depend on that data. Open items: "CR-002 and CR-003" below the CR-001 table.

### Egress threat table

Current table. Rows 1-36 keep their numbers. Rows 26, 31 and 32 are updated, and rows 37-41 are new in round 5.

"Meter" is the raw-table chain in `test_egress_surface.sh`. It counts the proxy's packets to its resolver (`10.212.0.2 -> <NS>:53`, UDP and TCP) that contain a fresh random label, plus every `in-addr.arpa` / `ip6.arpa` query. Its control is hermes's `web_public` lookup of its own label, which must be counted (2 packets). `surface` = `tests/system/test_egress_surface.sh`, `per_uid` = `test_egress_per_uid.sh`, `proxy_dns` = `test_proxy_dns.sh`, `profiles` = `tests/sandbox/test_profiles.py`, `render` = `tests/unit/ctl/test_render.py`.

| # | Path | Control that blocks it | Test proving it |
|---|---|---|---|
| **squid ACL semantics** | | | |
| 1 | IP-literal CONNECT matched to a category through the IP's PTR name (P1-11) | `-n` on every `dstdomain`/`dstdom_regex`; `deny ip_literal` before the allows | render `test_squid_name_acls_never_reverse_lookup` (only lookup ACL left = `denied_dst`); surface: IPv4/IPv6 literal 403 on 3140, 3150, 3128 host side and the fetch/research/llm pools, meter PTR v4 = 0 and v6 = 0; offline pinned-image repro: `dstdomain` 503 (tunnel attempted), `-n` 403 `HIER_NONE` |
| 2 | squid resolves a name no rule allows: DNS exfiltration through 403'd requests (P1-12) | no lookup ACL on any deny line; `!denied_dst` last on each allow line | render `test_squid_no_lookup_before_a_category_match`; surface: random-label CONNECT 403 and meter = 0 on 3140, 3150, 3128 host side, pool fetch, pool llm |
| 3 | IP-literal targets for `web_public` callers (research, hermes), incl. decimal/hex/octal forms | `deny ip_literal` (squid canonicalizes numeric hosts first) | render `test_ip_literal_patterns_match_literals_only` (14 literal forms, 10 look-alike names); surface: hermes 203.0.113.141, 3405803917, [2001:db8::3141] all 403; pool research IPv4/IPv6 literal 403; doctor "proxy denies" |
| 4 | Allowed or `web_public` name that resolves to loopback, RFC1918, link-local, CGNAT, `0.0.0.0/8`, the fake-IP range of a Windows VPN/proxy client (`198.18.0.0/15`, reachable through the WSL NAT), `192.0.0.0/24`, multicast or reserved space (rebinding) | `!denied_dst` on every allow line; `0.0.0.0/8` (round 3); `198.18.0.0/15`, `192.0.0.0/24`, `224.0.0.0/4`, `240.0.0.0/4` (round 4, NIT 1) | render (every allow line ends `!denied_dst`; `test_squid_pinger_off_and_cidrs_validated` checks the four new ranges; golden); surface: hermes `localtest.me` (127.0.0.1) 403; live `acl denied_dst` line carries the four ranges (checked in this step) |
| 5 | Denied name spelled differently (case, trailing dot) | squid lowercases and strips the trailing dot before ACLs; `denied_domains` is `-n` | surface: raw CONNECT `gist.github.com.`, `GIST.GitHub.COM`, `x.github.io.` via hermes 403 |
| 6 | CONNECT to a port other than 443, or HTTP to a port other than 80/443 | `deny CONNECT !SSL_ports`, `deny !Safe_ports` (fast) | surface: raw CONNECT `example.com:8443` and `:22` 403 |
| 7 | SNI / Host header differing from the CONNECT host (domain fronting) | no TLS interception: squid tunnels only to the CONNECT host's resolved IP, and the inner SNI/Host cannot change that IP. Plain HTTP uses the URL host for both the ACL and the connection | surface: live squid.conf has no `ssl_bump`/`https_port`/`sslcrtd_program`. **Residual (accepted, ADR-0005 alternatives):** a front end shared by an allowed host can serve other tenants (e.g. `storage.googleapis.com` in `pkg_go`). Only callers without secrets (fetch/validate pools, runner) hold such categories; the llm pool has only `kiro_core` |
| 8 | Caller picks another caller's class | port by `myportname`, host ports need `src 10.212.0.1/32`; 3128 refuses the host side; pools by source /18; unknown subnet: `deny all` | per_uid (uid × port matrix); surface: root via 3128 403; proxy_dns: unknown-pool subnet 403 |
| 9 | Sandbox reaches the host-caller ports 3140/3141/3150 | those listeners bind `10.212.0.2` only; the task networks have no route to it | profiles (`alias_ports`, `host_ports`); surface: `errno101` on all three, per pool |
| 37 | Cache manager request (`cache_object://`, `/squid-internal-mgr/`) decided by an allow line, e.g. the `web_public` callers' `all` (round 5 NIT) | `acl manager url_regex -i ^cache_object:// /squid-internal-mgr/` and `http_access deny manager` among the fast denies, ahead of every allow (round 5) | render `test_squid_no_lookup_before_a_category_match` (`deny manager` before the first allow, exact ACL) and golden; surface: hermes via 3141 `cache_object://ossbot-proxy/menu` and `http://ossbot-proxy/squid-internal-mgr/menu` both 403, and the live conf has the deny before the first allow |
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
| 26 | Service uid to a host-local service over `lo` (allowed by design), or a listener on a wildcard/bridge address | explicit listener allowlist: the WSL resolver `<NS>:53` (UDP/TCP; port 53 is refused to service uids, row 10) and chronyd's loopback command port `127.0.0.1:323`/`[::1]:323` (UDP, read-only monitoring commands; it belongs to the WSL VM, no process of this distro owns it) | surface: `ss -ltnupe` must show nothing else, loopback included (round 4, NIT 2). An unconnected UDP socket on an ephemeral port is skipped only when ss shows no owning process and no uid, i.e. a root socket of the WSL VM outside this distro's PID namespace; one owned by any process of this distro must be on the list (round 5 NIT). **Residual:** a new loopback service is still reachable by service uids, but it fails this test instead of passing silently |
| 27 | Sandbox straight to an internet IP | `--internal` network (no default route) + `OSSBOT-FWD` DROP | surface `direct443` per pool; profiles `noproxy_example`, `kiro_direct` |
| 28 | Sandbox to host services, or to a sandbox on another task network | `OSSBOT-IN -s 10.213.0.0/16 -j DROP` (IPv4), ip6tables `OSSBOT-IN` (IPv6); proxy `ip_forward=0` | profiles (`host_ports`, `peer`); surface (proxy netns `ip_forward` 0) |
| 29 | Proxy container to host services | `OSSBOT-IN -s 10.212.0.0/24 --ctstate NEW -j DROP`, with only the DNS ACCEPT above it | render `test_dns_accepts_precede_the_drops`; `ossbot-firewall --check` |
| 38 | Service uid to an abstract unix socket (`@name`) in the host netns: abstract sockets have no file permissions, so any uid can connect (round 5 NIT) | none exists: in the round-5 survey every abstract entry in `/proc/net/unix` was a connected systemd/D-Bus endpoint, none listening. A new one fails the test. Path-named sockets keep their file permissions (rows 11, 34; `test_control_perms.sh`, `test_secret_perms.sh`) | surface: `ss -H -xl` names starting `@` checked against an allowlist, which is empty |
| **Hyper-V firewall layer** | | | |
| 30 | Anything in the VM (root, ossadmin, the proxy) to a non-80/443 port | Hyper-V VM setting: inbound Block, outbound Block, allow TCP 80/443 (`scripts/hyperv-firewall.ps1`). DNS for root and the proxy uses the WSL DNS tunnel, not the VM NIC, so rows 10-17 carry DNS control | surface: root to 1.1.1.1:853/tcp, 1.1.1.1:53/udp and ICMP blocked, 1.1.1.1:443 allowed. When the optional layer is not applied this is a `skip` counted apart from PASS (`tests/system/lib.sh`, round 4, NIT 4). **In this bar it ran enforced:** 4 PASS, 0 SKIP; `scripts\ossctl.ps1 doctor` `hyper-v firewall` PASS |
| **Non-IP channels to the Windows host (round 4)** | | | |
| 31 | Service unit opens a Hyper-V socket (`AF_VSOCK`, host CID 2) to a WSL host listener with `socket(2)`: 50000 init, 50002 drvfs Plan 9 server (any Windows path the user can reach, ADR-0007 addendum), 50005 crash dumps (P1-13). netfilter, the WSL NAT and the Hyper-V firewall never see it | `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` on every non-root unit (gatekeeper, sandboxd now; FEAT-006/007 runner and hermes must carry it); also no `AF_NETLINK`/`AF_PACKET`. This filter sees only `socket(2)`: the io_uring path is row 39 (P1-14), which round 4 wrongly counted as closed here | unit `test_non_root_units_restrict_address_families` (every `ossbot-*.service` with a non-root `User=`) and `test_non_root_units_are_known`; surface, per loaded non-root unit: `systemctl show` value, MainPID `Seccomp: 2`, vsock connect in a `systemd-run` unit with the unit's user and its full filter set (address families, `SystemCallFilter`, `SystemCallErrorNumber`) = `errno97`. Control: an unrestricted `hermes` session reaches 2:50002 (`open`) |
| 32 | Sandbox container or the proxy opens `AF_VSOCK` | Docker's default seccomp profile refuses `socket(AF_VSOCK)`; the filter pins `SecurityOpt == ["no-new-privileges"]` and runc, so no profile override. io_uring in containers: row 40 | surface: `errno1` in the fetch, research and llm pools; proxy `Seccomp: 2` and SecurityOpt `["no-new-privileges"]` (the proxy image has no interpreter to probe with) |
| 33 | `AF_VSOCK`, drvfs or interop from sessions outside units (`runuser`, `wsl.exe -u <user>`, root) | none, by design: they need root or the owner's Windows console, which ADR-0007 already treats as root-equivalent (root can also `mount -t drvfs`) | documented residual (ADR-0005/0007 addenda); the row-31 control shows such a session is unrestricted |
| 34 | WSL interop sockets `/run/WSL/<pid>_interop` (mode 0777) asking the host to start a Windows process | host side: `[interop] enabled=false`, the host never answers (`UtilAcceptVsock: accept4 failed 110`, a `cmd.exe /c echo` probe as `hermes` wrote nothing, checked before the fix). Distro side (new): `ossbot-nointerop.service` sets `/run/WSL` 0700 at boot | surface: `/run/WSL` mode 700, connect refused for all five service uids; doctor `interop sockets` PASS; `wsl.sh check` fails on drift; test-isolation after `wsl --terminate` still 13/13 |
| 35 | `/mnt/wsl` (1777 tmpfs shared by every distro in the VM) and WSLg sockets | WSL runs only ossbot (ADR-0013), so `/mnt/wsl` reaches no other distro and not the host; `guiApplications=false`, so `/mnt/wslg` and `/tmp/.X11-unix` hold no X11/Wayland/PulseAudio sockets (no RDP clipboard path) | checked in this step (`find /mnt/wslg /tmp/.X11-unix`: directories only); `.wslconfig` hash in doctor |
| **Socket creation outside `socket(2)` (round 5)** | | | |
| 39 | Service unit creates an `AF_VSOCK` (or `AF_NETLINK`/`AF_PACKET`) socket with io_uring `IORING_OP_SOCKET`, which the `socket()` seccomp filter of row 31 never sees, then connects to 2:50002 (P1-14) | two independent layers. (a) `kernel.io_uring_disabled = 2` from `/etc/sysctl.d/60-ossbot.conf` refuses `io_uring_setup` to every process, root included. `ossbot-firewall --check` fails with `firewall_drift:io_uring` on any other value or a missing sysctl (every unit's `ExecStartPre`, watchdog soft kill, doctor `io_uring disabled`). (b) `SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register` with `SystemCallErrorNumber=EPERM` on every non-root unit | unit `test_non_root_units_filter_io_uring` (both lines and `SystemCallArchitectures=native` for every non-root unit, Phase 2 included); render `test_io_uring_check_requires_disabled_2`; watchdog `test_firewall_drift_reason[io_uring]`. Surface: a ctypes io_uring client gets `setup-errno1` in each unit context, as unrestricted `hermes` and as root. With the sysctl held at 0 for a few seconds (watchdog timer held, trap restores 2), each unit context still gets `setup-errno1` (filter alone), and the control, unrestricted `hermes`, reaches 2:50002 (`open`). Before the fix (this step, sysctl 0, unit's round-4 properties): gatekeeper context `open`. Live drift: with 0, `ossbot-firewall --check` exits 1 with `firewall_drift:io_uring` |
| 40 | Sandbox container or the proxy uses io_uring | Docker's default seccomp profile refuses `io_uring_setup` (EPERM), and the filter pins the profile (row 32). The sysctl (row 39) covers every container too | surface: with the sysctl at 0, `setup-errno1` in the fetch, research and llm pools. The proxy runs under the same default profile (`Seccomp: 2`, no override) and has no interpreter to probe with, so its evidence is the pools' result plus the sysctl |
| 41 | Other ways to hold a socket of a refused family: `socketcall(2)` through the 32-bit or x32 ABI, an fd received over `SCM_RIGHTS` or taken with `pidfd_getfd`, a new user or network namespace | `SystemCallArchitectures=native` (asserted per unit, row 39). Only root and WSL's own processes hold vsock sockets, and `pidfd_getfd` needs ptrace access (Yama `ptrace_scope = 1`, empty capability set). Seccomp filters are inherited by every child and namespace, and a new network namespace has only `lo` | unit test of row 39 for the ABI; the rest holds by construction (ADR-0005 P1-14 addendum). `RestrictNamespaces=yes` is a possible later hardening |
| **Allowed endpoints** | | | |
| 36 | Allowed SaaS host, attacker's own account: the llm pool calls `kiro_core` with an attacker's Kiro token, the runner writes to `api.github.com` with a token it obtained, the fetch pool uploads to an attacker's `storage.googleapis.com` bucket or `ghcr.io` (round 4, NIT 3) | none in the proxy: CONNECT-only matching cannot tell tenants apart (deliberate ADR-0005 trade-off, no TLS interception). Mitigations: fetch/validate/package hold no secrets, so there is nothing of ours to upload; the sandbox Kiro key is scoped to Kiro and rotatable, and sandboxd scans every output for it; the runner has no GitHub write token (writes are gatekeeper-only) | **accepted residual**, documented; per-profile egress categories keep each pool to the hosts it needs (render tests, `per_uid`) |

### Phase 1 bar (run in this step, 2026-10-18, about 12:16-12:40 +07)

| Phase 1 bar item | Result |
|---|---|
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports` | clean / 177 files formatted / 101 files no issues / 4 contracts kept |
| `ROOT shellcheck -x` provision.sh, scripts/lib, setup-secrets.sh, scripts/bin, entrypoints, fixtures, tests/system | clean |
| `DEV pytest -m "unit or telegram"` | 1557 passed, 11 deselected (new: 4 io_uring drift cases in `test_render.py`, `test_non_root_units_filter_io_uring` for 2 units, the `io_uring` watchdog reason; squid manager asserts and golden) |
| `DEV pytest -m "acp or e2e"` | no tests collected (exit 5; both suites are Phase 2: FEAT-006/FEAT-007) |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key` live Kiro probes, Phase 2) |
| `ROOT bash tests/system/*.sh` (11 scripts, final run after every code change) | all exit 0, 350 PASS, 0 FAIL, 0 SKIP: audit_anchor 11, config_write 11, control_perms 23, crash_safety 10, docker_filter 10, egress_per_uid 71, egress_surface 137 (was 118), firewall_failclosed 11, lifecycle 25, proxy_dns 5, secret_perms 36. The Hyper-V row ran enforced (4 PASS) |
| `tests\system\test_kill.ps1` | all checks passed |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13. After the cold boot: `kernel.io_uring_disabled` 2, `/run/WSL` 700, `systemd-sysctl` and all ten ossbot units active |
| `scripts\bootstrap.ps1` | deploy runs: `app-deploy` (render, doctor, tick, squid template; then DESIGN.md), `firewall` (new `/etc/sysctl.d/60-ossbot.conf`, `kernel.io_uring_disabled = 2`), `systemd` (gatekeeper, sandboxd units), `enable-start` restarts. Final run: every step `unchanged` (kiro/hermes `skipped (phase 2)`, Hyper-V WARN not elevated) |
| `ROOT docker version` / `docker compose version` | 29.9.1 / 5.6.0 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer | all active |
| `ossbot-firewall --check` | exit 0. Drift probe (watchdog timer held): with the sysctl at 0 it exits 1 with `firewall_drift:io_uring kernel.io_uring_disabled is 0, expected 2`, and 0 again after restoring 2 |
| `ossctl doctor` (root, full) | PASS 31, WARN 2, SKIP 3, exit 0 (new: `io_uring disabled: kernel.io_uring_disabled = 2`); "proxy denies": 3150 example.com 403, 3141 203.0.113.7 403 |
| `scripts\ossctl.ps1 doctor` | exit 0; Windows part all PASS incl. `hyper-v firewall: inbound Block, outbound Block + TCP 80/443`; distro part as above |
| `ossctl audit verify` | ok (404 rows, 124 anchors) |
| `gitleaks git --redact .` | no leaks (history). `gitleaks dir --redact` over each changed file (this log included) before the commit: no leaks |

With both io_uring layers in force, the gatekeeper's Telegram long-poll keeps answering and sandboxd restarts healthy. The restarts in the deploys and the cold boot show this, and so does every system test that drives them. WARNs and SKIPs are unchanged from round 4: the runner-owned catalog (FEAT-006), safety probes not run yet, and the three Hermes checks (FEAT-007).

Live traffic in this step: CONNECT probes to example.com, pypi.org, app.kiro.dev (no key), api.telegram.org and api.github.com/github.com (GET only, via the per-uid test). Cache-manager GETs went to the proxy (403, nothing forwarded). Hyper-V socket connects to 2:50002 came from the controls only, with no 9P message sent. There were no GitHub writes. Telegram saw only the gatekeeper's normal polling and the kill/lifecycle FYIs from `test_kill.ps1` and `test_lifecycle.sh`, all to the configured user ID. Dry-run stayed ON. The sysctl was at 0 only inside the surface test's control window and the one drift probe, both a few seconds with the watchdog timer held, and both restored to 2. No kill or pause happened outside the tests, and every test left the system running.

## Phase 1, after review round 4 (2026-10-18)

Fixes for `docs/review-phase1.json` round 4. Round 3's P1-11/P1-12 were closed by 109711f, and round 4 confirmed that. Round 4 found P1-13 and four NITs. The systematic pass extended the threat table with every non-IP channel from the VM to the Windows host (rows 31-35) and the attacker-tenant row (36).

- P1-13 (HIGH): `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` on `ossbot-gatekeeper` and `ossbot-sandboxd`, asserted for every non-root unit (Phase 2 runner/hermes included) by `tests/unit/test_systemd_units.py`. Listeners identified from the WSL source and a live Tversion: 50000 init, 50002 drvfs Plan 9 (ADR-0007 impact recorded), 50005 crash dumps. Probes run in the unit context and in every pool (rows 31-33). ADR-0005 and ADR-0007 addenda, DESIGN 7.2/7.5/22.6.
- Same pass: `/run/WSL` (interop sockets, 0777) made root-only by `ossbot-nointerop.service`, checked by `wsl.sh check`, doctor `interop sockets` and the surface test (row 34). `/mnt/wsl` and WSLg reviewed (row 35).
- NIT 1: `198.18.0.0/15`, `192.0.0.0/24`, `224.0.0.0/4`, `240.0.0.0/4` in `always_denied.cidrs` (row 4). NIT 2: host-listener probe is an explicit allowlist, `<NS>:53` only (row 26). NIT 3: row 36. NIT 4: `skip` in `tests/system/lib.sh` counts SKIPs apart from PASS. Both former `pass "SKIP ..."` calls use it; row 30 ran enforced.
- Test reliability: `test_audit_anchor.sh` failed once in this bar ("watchdog tick 10 appended an anchor") and passed on the two re-runs. Cause: a timer-driven tick that was already running wrote its own counter over the test's `tick = 9`. The test now holds `ossbot-watchdog.timer` around that check. Five consecutive runs passed, and so did the final full run below.

### Egress threat table

Superseded by the round-5 table above. Rows 26, 31 and 32 were updated there. Row 31 overstated the closure: io_uring bypassed the address-family filter (row 39).


### Phase 1 bar (run in this step, 2026-10-18, about 11:00-11:12 +07)

| Phase 1 bar item | Result |
|---|---|
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports` | clean / 177 files formatted / 101 files no issues / 4 contracts kept |
| `ROOT shellcheck -x` provision.sh, scripts/lib, setup-secrets.sh, scripts/bin, entrypoints, fixtures, tests/system | clean |
| `DEV pytest -m "unit or telegram"` | 1550 passed, 11 deselected (new: 3 systemd unit tests; render test extended) |
| `DEV pytest -m "acp or e2e"` | no tests collected (exit 5; both suites are Phase 2: FEAT-006/FEAT-007) |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key` live Kiro probes, Phase 2) |
| `ROOT bash tests/system/*.sh` (11 scripts, final run after every change) | all exit 0, 331 PASS, 0 FAIL, 0 SKIP: audit_anchor 11, config_write 11, control_perms 23, crash_safety 10, docker_filter 10, egress_per_uid 71, egress_surface 118, firewall_failclosed 11, lifecycle 25, proxy_dns 5, secret_perms 36. The Hyper-V row ran enforced (4 PASS) |
| `tests\system\test_kill.ps1` | all checks passed |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13; afterwards `/run/WSL` is `700 root` (set at boot) and `wsl -u ossadmin` sessions still work |
| `scripts\bootstrap.ps1` | deploy run: `wsl-confine` changed (new `ossbot-nointerop.service`, so bootstrap restarted WSL), `app-deploy` (doctor.py, DESIGN.md), `systemd` (gatekeeper, sandboxd units), `enable-start` restarted dockerfilter, gatekeeper, proxy (re-rendered squid.conf from the new snapshot), sandboxd. Next run: every step `unchanged` (kiro/hermes `skipped (phase 2)`, Hyper-V WARN not elevated) |
| `ROOT docker version` / `docker compose version` | 29.9.1 / 5.6.0 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer, boot-gate, firewall, firewall-docker, nointerop | all active |
| `ossbot-firewall --check` | exit 0 |
| `ossctl doctor` (root, full) | PASS 30, WARN 2, SKIP 3, exit 0 (new: `interop sockets: /run/WSL mode 700`); "proxy denies": 3150 example.com 403, 3141 203.0.113.7 403 |
| `scripts\ossctl.ps1 doctor` | exit 0; Windows part all PASS incl. `hyper-v firewall: inbound Block, outbound Block + TCP 80/443`; distro part as above |
| `ossctl audit verify` | ok (373 rows, 107 anchors) |
| `gitleaks git --redact .` | no leaks (history); `gitleaks dir --redact` over each of the 16 changed files (this log included) before the commit: no leaks |

Under the restriction the gatekeeper's Telegram long-poll answers 200 (one failed attempt while the proxy restarted with it, then `consecutive_failures 0`). sandboxd restarts and stays healthy. A `docker ps` through the filter socket in a transient unit with sandboxd's user and restriction exits 0. WARNs and SKIPs are unchanged from round 3: the runner-owned catalog (FEAT-006), safety probes not run yet, and the three Hermes checks (FEAT-007).

Live traffic in this step: CONNECT probes to example.com, pypi.org, app.kiro.dev (no key), api.telegram.org and api.github.com/github.com (GET only, via the per-uid test). Hyper-V socket connects to the WSL host listeners. A single 9P Tversion went to 50002 (no attach), and a create-process request went through the interop socket before it was closed. That request launched nothing. There were no GitHub writes. Telegram saw only the gatekeeper's normal polling and the kill/lifecycle FYIs from `test_kill.ps1` and `test_lifecycle.sh`, all to the configured user ID. Dry-run stayed ON. The watchdog timer was stopped before the deploy and started again by `enable-start`. No kill or pause happened outside the tests, and every test left the system running.

## Phase 1, after review round 3 (2026-10-18)

Fixes for `docs/review-phase1.json` round 3, made in one systematic pass over the whole egress/DNS surface (threat table below) instead of only the two named holes:

- P1-11: `-n` on every squid name ACL (`dstdomain -n` for the categories and `denied_domains`). New `ip_literal` ACL (`dstdom_regex -n -i`, every numeric IPv4 form and IPv6) with `http_access deny ip_literal` ahead of every allow on every port, `web_public` callers included.
- P1-12: the global `deny denied_dst` is gone. The resolved-IP deny is now the last ACL of each allow line (`allow port_X <src> <cat|all> !denied_dst`), so squid resolves only names that already matched the caller's port, source and category. Every deny line uses fast ACLs only.
- Same pass: `0.0.0.0/8` added to `always_denied.cidrs`, and the renderer refuses non-literal CIDRs. `pinger_enable off`, `visible_hostname ossbot-proxy`. New ip6tables `OSSBOT-IN` (`-i docker0` / `-i br-+` DROP at `INPUT` position 1, enforced by `--check`). Doctor's "proxy denies" check also sends an IP-literal CONNECT.
- Evidence: render tests in `tests/unit/ctl/test_render.py` and the new `tests/system/test_egress_surface.sh` (100 checks). Against the round-3 config, the same script failed exactly on the two findings: 2 proxy DNS packets per denied random label on 3140, 3150, 3128 and the fetch and llm pools, 14 PTR packets for the IP literals, and `web_public` IP literals allowed. After the fix all its counters are 0. ADR-0005 addendum, DESIGN §11.3/§11.4/§22.6.
- An allowlist-only resolver for squid was considered and not adopted. squid has one resolver for every listener, and `web_public` (research pool, hermes 3141) must resolve any name (DESIGN §11.3), so it would need a second proxy instance. The ACL order gives the confined callers the same property, measured by the meter below (ADR-0005 addendum).

### Egress threat table

Superseded by the round-4 table above. Rows 1-30 keep their numbers; rows 4, 26 and 30 were updated there.

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
> Historical (2026-10-18): superseded by docs/TRACEABILITY.md (rows CR1-*, CR2-*, CR3-*). The FEAT numbers in this table are the pre-consolidation plan's (old FEAT-005 = Telegram decision paths, old FEAT-008 = docs); the current owner of each item is the phase column of its TRACEABILITY row.

| CR-001 item | Owner | Status |
|---|---|---|
| 6a shutdown blocker (`WM_QUERYENDSESSION` + `ShutdownBlockReasonCreate` → `down.ps1 -Timeout 90`), verified live on this Windows 11 Home 26100 laptop (hidden window, auto-continue, Fast Startup), ADR | FEAT-008 | **open (required)** |
| 6b event-triggered task on System/User32 1074 → `down.ps1 -Timeout 20 -Kind hook` (non-elevated, no sleep trigger, SpiralPool BSD-3 credit if adapted) | FEAT-008 | **open (required)**; `down.ps1 -Kind hook` and the `hook` shutdown type already exist |
| `bootstrap.ps1` installs 6a/6b, `-Uninstall` removes them, `ossctl.ps1 doctor` checks them; optional `-Shortcuts` | FEAT-008 | open |
| Stage re-queue on `up` and the real `resumed` count; runner honours the lifecycle flag; SIGKILL-runner e2e | FEAT-006 | open (sandbox side done: jobs end `interrupted`, task volumes kept) |
| Telegram `/drain`, `/undrain`, `/down`, `/up` | FEAT-005 | open (API and ControlService methods exist) |
| README/SETUP "Daily use", AGENTS.md lifecycle invariants | FEAT-008 | open |
| Done in Phase 1 | | lifecycle states + API (kill overrides), sandboxd drain + crash recovery, `TimeoutStopSec`, `ossctl up/down/status`, `scripts/{up,down,status}.ps1`, boot-gate shutdown classification (graceful/hook/unclean), watchdog sleep handling, monotonic job/drain timeouts, Telegram reconnect tracking + offline start, crash-safety tests |

### CR-002 and CR-003 (tracked for Phase 2)
> Historical (2026-10-18): superseded by docs/TRACEABILITY.md (rows CR1-*, CR2-*, CR3-*). The FEAT numbers in this table are the pre-consolidation plan's (old FEAT-005 = Telegram decision paths, old FEAT-008 = docs); the current owner of each item is the phase column of its TRACEABILITY row.

| Change request | Owner | Status |
|---|---|---|
| CR-002 research egress and Hermes capability tiers (`docs/plan/CR-002-research-egress-and-hermes-capabilities.md`): `github_user_content` for research/hermes only, egress modes `allowlist`/`research`/`open` whose invariants no mode relaxes, Hermes tiers `locked`/`research`/`self_improve` with Telegram-approved skills, live Hermes research check. Amendment A: defaults `self_improve` and `open`; Hermes becomes the user's general multimodal assistant (routing of non-command messages, attachments with caps and EXIF stripping, `vision` role, streamed replies, per-feature plugin stores, OSS notifications kept apart from chat) | Phase 2 planning (FEAT-005/006/007/008) | **open (required)**; nothing implemented in Phase 1 |
| Phase 1 notes for CR-002 | | Egress is data: `config/egress.yaml` `categories`, `sandbox_profiles`, `host_callers` and `always_denied` go through the config snapshot to `ossbot.ctl.render squid`, with no per-profile special cases. So `open` for `agent-llm` is the config line `agent-llm: [kiro_core, web_public]`. One constraint: `agent-llm` and `catalog` share the llm pool (DESIGN 10.3), and the renderer refuses pool-mates with different categories. `open` therefore also needs `catalog` with the same list, or its own pool. The every-mode invariants (`ip_literal`, `denied_dst`, vsock/io_uring, DNS, Hyper-V) are not in that data. Tests that pin today's defaults and must change with CR-002: `test_egress_surface.sh` (hermes raw CONNECT `gist.github.com.` / `x.github.io.` 403) and the squid golden |
| CR-003 Telegram UX and PR review surface (`docs/plan/CR-003-telegram-ux.md`, ADR-0019, mockups in `docs/ux/`; written and committed by a separate design workflow) | Phase 2, sequenced last | **pending approval**. Once `docs/ux/cr003-verdict.json` is APPROVED and the user confirms, it replaces the Telegram presentation details of DESIGN 14. Phase 2 builds the backend and the deterministic handlers first. If CR-003 is not approved by the time the Telegram presentation and review-flow work comes up, pause and ask; do not implement the old 14 presentation |
