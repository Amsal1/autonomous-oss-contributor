# Phase 1 review (round 3): safety core, sandbox isolation, provisioning foundation

Round 3 checks the round-2 fixes (commit 381848e) on top of the whole Phase 1 diff since 7624a0c. All five round-2 findings are closed. systemd-resolved is masked and `ossbot-firewall --check` fails closed if it comes back (P1-6). Both resume paths invalidate armed approvals (P1-7). The watchdog's `down` exemption is scoped (P1-8), the drain timeout is capped at 120 (P1-9), and app-deploy now changes only on content (P1-10). Every bar command passes when I re-run it. A new probe of the squid allowlist found two problems in the rendered `squid.conf`, and both defeat the egress allowlist for the confined callers.

Watch for: squid lets an IP-literal CONNECT through any `dstdomain` category whose name matches the IP's PTR record, a full allowlist bypass for anyone who controls a reverse zone (confirmed: throwaway squid 6.14 from the pinned proxy image tunnels to the IP without `-n` and returns 403 with it). Squid also resolves every requested hostname, including ones it is about to deny, because the global `deny denied_dst` runs before the category checks. That gives every caller, including the `llm` and `fetch` sandbox pools, a DNS exfiltration channel past the allowlist (confirmed: live OSSBOT-IN DNS counters rise on 403'd CONNECTs via 3150 and 3128).

**Verdict**: NEEDS_CHANGES

## High-level view

With direct DNS and resolved closed, the proxy is the only path left from a confined caller to a resolver. Its ACL order makes squid do the lookups for the caller. The PTR match turns an IP the attacker owns into an "allowed" host, for example by setting its reverse name to `runtime.us-east-1.kiro.dev` for the coder pool or `pypi.org` for the fetch pool. The early `dst` deny resolves names that no category allows.

Both fixes stay in the renderer and template, and they keep DESIGN §11.3's resolved-IP deny for allowed names. Render `-n` on every `dstdomain` ACL. Then evaluate `denied_dst` only after a request has matched an allowed category. Neither the system tests nor doctor probe the proxy's own lookups today, which is why every bar item passes.

<details>
<summary>Issues (2)</summary>

1. **P1-11 HIGH: PTR-based allowlist bypass in squid `dstdomain`** (`sandbox/proxy/squid.conf.j2:31,33`; golden `tests/unit/ctl/golden/squid.conf`). An IP-literal CONNECT is allowed when the IP's reverse name matches a category, so any caller can tunnel to an attacker IP whose PTR names an allowed host. Render `-n` on every `dstdomain` ACL (categories and `denied_domains`). Add a render test asserting it, and add a system/sandbox probe: an IP-literal CONNECT per port and pool returns 403 with no proxy DNS packet. Record the decision in an ADR-0005 addendum.
2. **P1-12 HIGH: squid resolves denied hostnames (DNS exfiltration via the proxy)** (`sandbox/proxy/squid.conf.j2:34,39,43`). The global `http_access deny denied_dst` forces a forward lookup of every requested name before the port/pool/category rules. Callers without `web_public` (3140 runner, 3150 gatekeeper, 3128 `fetch` and `llm` pools) can therefore push arbitrary labels to the internet through 403'd requests. Move the resolved-IP deny behind the category match, e.g. `http_access allow port_X src_Y cat_Z !denied_dst` per rule, or a fast `deny !<union of the port's categories>` ahead of `deny denied_dst`. Add a probe: a random-label CONNECT per port and pool leaves the OSSBOT-IN proxy-DNS counter (or the squid ipcache) unchanged. Add a render test that no `dst` ACL is evaluated before a category ACL, and an ADR-0005 addendum.

</details>

<details>
<summary>Details</summary>

### Squid matches IP-literal CONNECTs through the PTR name (P1-11, HIGH, confirmed)

The template renders `acl cat_<name> dstdomain <hosts>` and `acl denied_domains dstdomain ...` with no flags (`squid.conf.j2:31,33`; live `/etc/ossbot/proxy/squid.conf:34-44`). When the request target is an IP and the IP string does not match, squid's `dstdomain` does a reverse lookup and matches the PTR name. The resolver does not forward-confirm the PTR, and the reverse zone belongs to whoever controls the IP. Reviewer probe with the pinned proxy image (`ossbot/proxy@sha256:24c4…`, squid 6.14), on throwaway `--network none` containers with `hosts_file` mapping `203.0.113.10 api.github.com` and `acl cat dstdomain api.github.com` / `http_access allow cat`:

```
dstdomain (as rendered, no -n):
  CONNECT 203.0.113.10 -> HTTP/1.1 503 Service Unavailable
  log: ... TCP_TUNNEL/503 0 CONNECT 203.0.113.10:443 - HIER_DIRECT/203.0.113.10 -
dstdomain -n:
  CONNECT 203.0.113.10 -> HTTP/1.1 403 Forbidden
  log: ... TCP_DENIED/403 3393 CONNECT 203.0.113.10:443 - HIER_NONE/- text/html
```

The 503 is squid trying to open the tunnel, which fails only because the probe container has no network. On the live proxy, an IP-literal CONNECT via 3150 made two DNS packets leave through the OSSBOT-IN proxy-DNS ACCEPT (counter 10 → 12), so the reverse lookup also happens in production. `denied_dst` does not help, because the attacker IP is public. The exposure follows `rules` (`squid.conf:53-67`). `pool_llm` (agent with the sandbox Kiro key, reading untrusted repo content) gets out through any `cat_kiro_core` name. `pool_fetch` (runs untrusted install and build steps) gets out through any `pkg_*` or `github_read` name. The runner (3140) and gatekeeper (3150) are exposed through their categories. The split coder and the per-profile allowlist (bar (a): egress allowlist fail-closed) rest on these ACLs. With `-n` the ACL never converts between IP and name, so an IP literal matches only an IP entry, and the categories contain none.

### Every requested hostname is resolved, even when it is denied (P1-12, HIGH, confirmed)

`http_access deny denied_dst` (`squid.conf.j2:39`) is a `dst` ACL, so squid has to resolve the request's hostname to evaluate it. It sits above the per-port/pool allow rules (`:43`), so the lookup happens for every request that is not on `denied_domains`. Live probe as uid `gatekeeper` through 3150, three CONNECTs to `rv3-<random>.example.com:443`, all answered `403 Forbidden`. The OSSBOT-IN `udp dpt:53` ACCEPT from `10.212.0.2` went from 4 to 10 packets (A + AAAA per name). One CONNECT to port 3128 from the host side, which `deny port_sandbox src_host` refuses two rules later, moved it from 12 to 14. The 3128 ordering is the same for every sandbox pool, so a process in the `llm` or `fetch` pool can stream data as DNS labels to an authoritative server it controls, with every request "denied". This is the same channel class as P1-1 and P1-6, one layer up. The callers with `web_public` (`pool_research`, hermes 3141) may resolve anything by design. For the rest, a name that matches no category must not be resolved. The resolved-IP deny that DESIGN §11.3 asks for ("by resolved IP, `dst` ACL") still applies once the name is allowed. Squid evaluates the ACLs of one `http_access` line left to right and stops at the first mismatch. So `allow port_X src_Y cat_Z !denied_dst` resolves only names that already matched `cat_Z`, and so does a fast `deny !cat_union_X` placed before the `dst` deny.

The current probes miss this because they check only that the confined callers get 403 and that direct DNS is refused. They never check whether the proxy resolved anything. The OSSBOT-IN proxy-DNS counter is a cheap, deterministic signal for the system test (`test_egress_per_uid.sh` / `test_proxy_dns.sh`): read it, send a random-label CONNECT and an IP-literal CONNECT per port and per pool, and expect it unchanged. `ossctl doctor` can do the same for the host ports.

### Verification evidence (re-run by the reviewer, 2026-10-18 09:00-09:40 +07)

| Bar item | Result |
|---|---|
| round-2 findings P1-6..P1-10 | closed: resolved `masked`, varlink refused, varlink + D-Bus `ResolveHostname` refused for all five uids; resume paths invalidate (and `ossbot-resume:48` still refuses a request older than the kill flag); `distro_down` exemption with `restart_failed(skip=WORKER_UNITS)`; `drain_timeout_s le=120`; first bootstrap after the full test run all `unchanged` |
| `DEV pytest -m "unit or telegram"` | 1541 passed, 11 deselected |
| `DEV ruff check` / `ruff format --check` / `mypy` / `lint-imports` | clean / 177 formatted / 101 files no issues / 4 contracts kept |
| `ROOTDEV pytest -m sandbox` | 9 passed, 2 skipped (`needs_key` live Kiro probes, Phase 2) |
| `ROOT bash tests/system/*.sh` (10 scripts) | all exit 0, 213 PASS, 0 FAIL |
| `scripts\bootstrap.ps1` (run twice) | both runs: every step `unchanged` (kiro/hermes `skipped (phase 2)`, Hyper-V WARN not elevated, doctor --quick PASS 25 WARN 2 SKIP 3) |
| `wsl --terminate ossbot` + `scripts\test-isolation.ps1` | PASS 13/13 |
| `systemctl is-active` gatekeeper, dockerfilter, proxy, sandboxd, watchdog.timer (+ boot-gate, firewall x2) | all active (after the isolation test's WSL restart) |
| `docker version` / `docker compose version` | 29.9.1 / v5.6.0 |
| `ossctl doctor` (root, full) | PASS 29, WARN 2, SKIP 3, FAIL 0, exit 0. Live GitHub/Telegram checks PASS (secrets exist). WARNs: runner-owned catalog (FEAT-006) and not-yet-run safety probes (Kiro jobs refused until they pass) |
| `gitleaks git --redact` | no leaks |
| personal values outside `docs/` | none: `git grep` for the owner's name/login/emails/IdC host found 0 hits. The only matches for `config/profile.yaml` values are generic (a category name, `us-east-1`) |
| `git status --porcelain` | empty before this review's commit |
| pushes | none: `origin/main` is still 3ccd9bc; 4d13e23 and 381848e are local only |

Bar (a) fails on P1-11 and P1-12 (egress allowlist fail-closed). Bars (b) to (e) pass.

</details>

<details>
<summary>File map</summary>

- `sandbox/proxy/squid.conf.j2`, `src/ossbot/ctl/render.py` (`render_squid`), `tests/unit/ctl/golden/squid.conf`, `tests/unit/ctl/test_render.py`: squid ACLs (P1-11, P1-12)
- `tests/system/test_egress_per_uid.sh`, `tests/system/test_proxy_dns.sh`, `src/ossbot/ctl/doctor.py`: per-uid and proxy probes (extend for P1-11, P1-12)
- `scripts/lib/firewall.sh`, `src/ossbot/ctl/render.py` (`resolved_ipc_problems`), `src/ossbot/ctl/doctor.py`: resolved masked and checked (P1-6, closed)
- `src/ossbot/gatekeeper/control.py`: resume-path invalidation (P1-7, closed)
- `src/ossbot/watchdog/tick.py`: scope-aware lifecycle exemption (P1-8, closed)
- `src/ossbot/common/config.py`: drain timeout cap (P1-9, closed)
- `scripts/lib/services.sh`: content-only app-deploy with itemized log (P1-10, closed)
- `docs/{DESIGN,PLAN,VERIFICATION}.md`, `docs/adr/0005-sandbox-isolation-and-egress.md`: round-2 records

Full diff: `git -C d:\Work\autonomous-oss-contributor diff 7624a0caa7667aa02a4bccce76f820c5be1fc6f6..HEAD` (round-3 delta: `4d13e23..HEAD`)

</details>
