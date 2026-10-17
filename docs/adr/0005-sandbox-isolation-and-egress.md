# ADR-0005: Sandbox isolation via per-stage profiles, a split coder, per-task internal networks, and a squid allowlist
Status: Accepted (revised in design rev 1, rev 2 and rev 3) · Date: 2026-10-17 · Design: DESIGN.md §10, §11

## Context
The coder must run hostile repo code (tests, install scripts) and kiro-cli needs a Kiro key. Hostile code at the same UID can read the key from `/proc/<pid>/environ`, and "Kiro hosts give an attacker no readable channel" is unverified (server-side web tools, remote MCP, cloud sessions). A hostile repo can also ship `.kiro/` workspace config (MCP servers, hooks). IP allowlists are brittle for CDN-fronted hosts.

## Decision
- Stages run in throwaway containers (UID 10001, `--cap-drop ALL`, `no-new-privileges`, read-only rootfs, default seccomp, limits, per-task volume, no host mounts, `--pull never`, `--dns 127.0.0.1`), created only through the Docker API filter (ADR-0001).
- **Invariant: no container both holds a credential and executes repo code.**
  - `fetch`/`validate`/`package`: run repo code or git on repo data; no secrets.
  - `research` (vetter, planner, reviewer, summarizer): Kiro key + public web (summarizer: no web), tools `read`/`web_*` only, repo read-only.
  - Coder = `agent-llm` (key, kiro-cli, tools `read`, `write` on `/work/repo/**`, `@ossbot-exec/run`; no shell; Kiro-only egress) + `agent-exec` (no key, `--network none`, deterministic `ossbot-execd` running commands with a fixed env). kiro-cli is not even installed in code-running images.
  - All kiro-cli processes use a tmpfs HOME and an empty tmpfs cwd, never the repo, so repo-local `.kiro/` config is not loaded.
- Single-container coder mode exists only as an audited downgrade, refused unless an ADR addendum proves no attacker-directed egress via Kiro features and env scrubbing by `kiro-cli --sandbox`.
- sandboxd scans every output, log, and bundle blob for the literal key and its encodings before publishing (fail + pause); the gatekeeper hash-checks token-like strings against the key hashes.
- Each task/profile has its own `--internal` network with a persisted /28; the proxy (10.212.0.2 on `ossbot-egress`) is attached with alias `proxy`; squid selects the profile by source subnet and matches CONNECT host names (no TLS interception). Host users get one proxy port each (runner 3140, Hermes 3141, gatekeeper 3150 limited to Telegram + GitHub) enforced by owner-match iptables; no direct 443/53 for any service user.

## Alternatives
- Key and hostile code in one container with Kiro-only egress (v1): rejected (review H3).
- TLS-intercepting proxy to allow fetch but not push: rejected for complexity and CA distribution.
- Kiro local command sandbox as the primary control: unverified inside containers; only relevant to the downgrade mode.

## Revision 2 amendments (design review round 2)
- H1: the coder's built-in `read`/`write` and research's read-only task-volume mount are replaced. Key-holding containers never mount an attacker-writable path. Research reads a symlink-free snapshot volume, and `agent-llm` has no `/work` mount and uses `@ossbot-exec/*` file tools. Denies are extended to `/proc`, `/sys`, `/dev`, `/run`. See ADR-0017.
- M2: live probes gate Kiro jobs per (CLI, image, agent config, settings, trust mode) tuple (ADR-0017).
- M3/M4: stage I/O at image-rooted `/io/input` (read-only, filled by a `stage-in` container) and `/io/output`; complete Docker filter create allowlist.
- M6/M7: firewall split into `ossbot-firewall` (before Docker) and `ossbot-firewall-docker` (`BindsTo`/`PartOf` Docker), own chains, `ExecStartPre --check` on every service (fail closed); proxy DNS allowed to the WSL resolver only.
- **Addendum slot (implementation):** result of "does Kiro `read` resolve symlinks before matching `deniedPaths`?" (DESIGN §23 5a). Not load-bearing after H1.

## Revision 3 amendments (design review round 3, M2/M11/N5)

- M2: a fresh named volume takes its root owner and mode from the image directory (copy-up). So the writable mountpoints `/work`, `/work/repo`, `/io/output`, `/run/exec`, `/snap` and `/home/agent` are created `10001:10001 0700`, and `/io/input` and `/in` stay `root 0755`. The read-only rootfs, not ownership, is what prevents mountpoint swaps.
- M2: Docker mounts every tmpfs `noexec` unless `exec` is given. `/tmp` gets `exec` only in the code-running profiles (fetch, validate, agent-exec, package), and the Docker filter enforces that profile set. LLM profiles keep `noexec`. If kiro-cli is proven to need exec, the only allowed change is `exec` on `/tmp` for the LLM profiles, recorded here with probe evidence. Sandbox test S-perm-1.
- M11: new `catalog` profile for `list_models` only: `sandbox-llm`, `repo_mode none`, no inputs, network from the `agent-llm` pool (`kiro_core` only). It is exempt from the probe gate (ADR-0017).
- N5: the Docker filter refuses container start, not only create, while killed or paused.
- **Addendum slot (implementation):** whether kiro-cli needs exec under `/tmp` or `$HOME` (DESIGN §23 5), and the Docker 29 copy-up ownership result (§23 14).

## Consequences
Two containers per implement run and an MCP relay to maintain. The coder has no web tools in v1 (planner does the research). **Verify:** exact V3 tool ids and MCP tool naming, workspace config discovery rules (S-ws-1), Kiro host set via learn mode, whether web tools are server-side.

## Implementation addendum (FEAT-003, 2026-10-17): DESIGN §23 items 14 and 16

Checked by web search and a live probe on the `ossbot` distro before the firewall and squid renderers were written. Items marked "pending live probe" need Docker, which FEAT-004 installs; the sandbox smoke tests there (S-perm-1, stage-in, `test_proxy_dns.sh`) close them.

Docker 29 (item 14):
- Firewall backend. iptables is still the default in Docker 29; nftables support (since 29.0.0) is experimental and has **no `DOCKER-USER` chain** ([Docker with nftables](https://docs.docker.com/engine/network/firewall-nftables/)). Decision: `daemon.json` sets `"firewall-backend": "iptables"` explicitly, so `OSSBOT-FWD` can hook `DOCKER-USER`. FEAT-004 checks `iptables -S DOCKER-USER` after the daemon starts.
- Host to an unpublished port on a user bridge. Docker documents that every port of a container on a bridge network is reachable from the Docker host ([Networking overview](https://docs.docker.com/engine/network/)). The proxy therefore runs without published ports; the `-p 10.212.0.1:<port>` fallback is not used. Pending live probe.
- Embedded DNS. The embedded resolver forwards non-container names from the **container's** network namespace; only a loopback upstream taken from the host's `resolv.conf` is forwarded from the host namespace, and since 25.0.5/26.0.0 containers attached only to internal networks get no external forwarding at all ([GHSA-mq39-4gv4-mvpx / CVE-2024-29018](https://github.com/moby/moby/security/advisories/GHSA-mq39-4gv4-mvpx)). An explicit `--dns` makes forwarding happen from the container namespace. So with `--dns 127.0.0.1` the upstream is the sandbox's own loopback, where nothing listens: external names never resolve. Content was rephrased for compliance with licensing restrictions.
- Resolver placement (live probe, 2026-10-17): `/etc/resolv.conf` is WSL-generated with `nameserver 10.255.255.254`, and that address is assigned to the distro's `lo` (`inet 10.255.255.254/32 scope global lo`). Packets from the proxy (10.212.0.2) to it arrive over the bridge and traverse the host **INPUT** chain, not FORWARD. The two `OSSBOT-IN` ACCEPT rules (UDP/TCP 53 from 10.212.0.2/32 to `<NS>/32`) are therefore load-bearing and are rendered before both DROPs. squid uses `dns_nameservers <NS>` directly, never Docker's embedded resolver, for external names.
- Copy-up ownership. moby copies the image mountpoint's owner, group and mode onto a fresh, empty named volume at first mount ([moby#35486](https://github.com/moby/moby/issues/35486)); this is what the M2 directory owners in the Dockerfiles rely on. Pending live probe S-perm-1 (both the rw-first and the `ossbot-ro-*` snapshot-first case).
- Archive PUT into a created container. Extraction into a writable volume mountpoint of a container with a read-only rootfs is supported; the known failure is only a symlinked parent directory ([moby#43015](https://github.com/moby/moby/issues/43015)), which `/in` is not (image-rooted directory). `docker cp` stats the destination with `HEAD /containers/{id}/archive?path=…` before the PUT, so the filter allows `HEAD` with exactly the same checks as the matching `PUT`/`GET` row. Pending live probe and the corpus re-recording (§23 item 15).
- Image store. Fresh Docker 29 installs use the containerd image store ([containerd image store](https://docs.docker.com/engine/storage/containerd/)), so locally built images carry a content digest and the `ossbot/<image>@sha256:<digest>` form works with `--pull never`. FEAT-004 records the digests and confirms a run by digest.
- `--workdir` inside a tmpfs (`/home/agent/cwd`): pending live probe. `kiro.sh` and `list_models.sh` also `mkdir -p` and `cd` into it themselves, so the result only decides whether Docker creates it.
- `exec` in `--tmpfs` options is passed explicitly (Docker adds `noexec,nosuid,nodev` otherwise); pending live probe S-perm-1.

Squid 6 (item 16):
- Version: Ubuntu 24.04 ships `squid 6.14-0ubuntu0.24.04.4` (noble-updates, `apt-cache policy squid` on the distro). The proxy image pins that package version.
- Listeners and ACLs: `http_port <ip>:<port>` binds only that address. The `localport` ACL matches the TCP port the client connected to, and `myportname` matches the `name=` of an `http_port` line (both "fast" ACLs, [squid acl reference](https://www.squid-cache.org/Doc/config/acl/)). The renderer names every listener (`name=sandbox|runner|hermes|gatekeeper`) and matches with `myportname`, plus `src` ACLs: per-profile source pools on 3128 and `src 10.212.0.1/32` on the host ports. Requests on 3128 from 10.212.0.1 are denied.
- Interface limit: no per-container limit on attached networks is documented. The often quoted limit of about 30 networks comes from default-address-pool exhaustion, which does not apply because every ossbot network has an explicit /28. Expected attachments of the proxy: 2 pipelines × 3 task networks + catalog ≈ 7. FEAT-004's smoke test attaches 10 networks. Pending live probe.
- Log format: the rendered `logformat ossbot %ts.%03tu %>a %>lp %Ss/%03>Hs %rm %ru %<a` records the client address (source subnet, hence profile and task), the local port (caller class), the result and the requested host. The squid log parser (Phase 2 anomaly) keys on `%>a` and `%>lp`.
- Kiro hosts in `kiro_core` are unchanged; learn mode (§11.3) still has to pin them once a key exists.
- The rendered `squid.conf` was parsed by the real squid 6.14 binary (`squid -k parse`, package extracted to a temp dir, not installed): every `acl` and `http_access` line accepted; the only warning is the RFC note for `via off`.

iptables (live format probe): the distro's `iptables` is 1.8.10 (nf_tables). Each rendered chain was loaded into an unreferenced scratch chain and read back with `iptables -S`/`ip6tables -S` (no jump, no traffic effect, removed afterwards). Output adds `-m udp`/`-m tcp`, `/32` on addresses and `--reject-with icmp-port-unreachable`; `ossbot-firewall --check` normalizes exactly these and treats any other token as drift.

Implementation decisions (FEAT-003), each stricter than or equal to the design text:
- Output key scan of bundles (§10.6): a package bundle is a thin pack whose prerequisite (the round base) sandboxd does not have, so `git bundle unbundle` into an empty repo fails for every fix round. sandboxd inflates every pack entry itself instead: whole objects in full, deltas by their literal insert data (the only place new bytes can be). Unparseable bundles fail the stage (`bundle_unreadable`).
- Docker filter: `HEAD /containers/{id}/archive` is allowed with the same checks as the matching `PUT`/`GET` row (the CLI stats before copying). The image is bound to the profile (`sandbox-llm` only for research, agent-llm and catalog; `sandbox-full` otherwise), `/in` only for stage-in and `/snap` only for snapshot. `AttachStdout`/`AttachStderr` booleans and `ConsoleSize: [0, 0]` are accepted because `docker create` sends them; the attach endpoint stays forbidden.
- The digest allowlist comes from a root-owned copy of `versions.lock.yaml` at `/etc/ossbot/versions.lock.yaml` (written by the provision `images` step), because image digests are not part of the four-file config snapshot. Missing or `pending` digests allow no image.
- Job args reach the fixed entrypoints as `OSSBOT_ARG_*` environment values (the filter binds `Cmd` to the bare script path); `package.sh`/`unbundle.sh` read them as defaults for their flags.
- `agent-exec` writes its `exec-log.jsonl` to its own output volume `ossbot-out-<run_id>-exec`; package outputs go to `/srv/ossbot/artifacts/<cid>/<rev>/` (the JobSpec package args carry `rev`).
- ip6tables: `OSSBOT-OUT` (per service uid: `lo` accept, else DROP) and `OSSBOT-FWD` in `FORWARD` (DROP on `docker0`/`br-+`), in the host part.
- `policy.yaml sandbox.kiro_trust_mode` (`ids` | `all`, default `ids`) added to the schema, read by sandboxd from the snapshot.
