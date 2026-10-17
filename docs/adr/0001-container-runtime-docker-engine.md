# ADR-0001: Container runtime is Docker Engine inside the ossbot distro, behind a root API filter
Status: Accepted (revised in design rev 1 and rev 2) · Date: 2026-10-17 · Design: DESIGN.md §7.4, §10.2, §10.8

## Context
Sandboxes need per-network egress control, Compose (stack validation), and kind. Options on this laptop: `wslc.exe` (WSL Containers, GA 2026-09-29, no compose), Docker Desktop (not installed, not wanted), Docker Engine inside `ossbot`, Podman. The Docker socket is root-equivalent, and the sandbox daemon parses large untrusted outputs (design review M10).

## Decision
- Install Docker Engine (`docker-ce`, `containerd.io`, buildx, compose plugin) from Docker's apt repo inside `ossbot`, pinned in `config/versions.lock.yaml`, `data-root=/srv/ossbot/docker`, `bip=10.211.0.1/24`, explicit subnets for every ossbot network.
- All container operations go through `ossbot.sandboxd.runtime.ContainerRuntime` (`DockerCliRuntime`, `FakeRuntime`; future `WslcRuntime`).
- The `docker` group has no members. sandboxd (`ossbox`) talks to `/run/ossbot/docker-filter.sock`, served by the root `ossbot-dockerfilter`, which forwards only template-conformant calls: labelled containers, named `ossbot-*` volumes without `DriverOpts`, internal `ossbot-t-*` networks, no binds, no privilege, fixed security options. Everything else (exec, build, image ops, update) gets 403.
- Root-only operations (image builds, proxy container, kill enforcement, Phase 2 `ossbot-stack`) use the real socket as root.

- (rev 2, M4) The filter's create check is a complete allowlist over `Config`, `HostConfig` and `NetworkingConfig`; any unlisted key with a non-zero value, and any duplicate JSON key, is rejected. It also includes profile-specific mount and env rules: no task or snapshot volume on `agent-llm`, `KIRO_API_KEY` only on key profiles. Archive PUT is allowed only into `stage-in` containers at `/in`. Readiness checks use inspect, never exec. Volume names include `ossbot-ro-*` (snapshot). The filter keeps running while killed and refuses every create.
## Alternatives
- wslc: faster startup, but no compose, no per-network iptables hooks, no kind today. Revisit when compose lands.
- Podman rootless: better default privilege, but pasta/slirp networking makes the proxy-only egress model and kind harder.
- sandboxd in the `docker` group (v1): rejected; it made I2 false.

## Consequences
One more small root service to keep correct; it is deterministic and covered by allow/deny corpus tests. **Verify:** Docker 29 firewall backend on WSL kernel 6.18, `DOCKER-USER` presence, internal-network DNS with `--dns 127.0.0.1`, API request shapes emitted by the pinned CLI.

## Implementation addendum (FEAT-001, 2026-10-17)
- Verified on download.docker.com (noble/stable, amd64): newest `docker-ce`/`docker-ce-cli` is `5:29.9.1-1~ubuntu.24.04~noble` (DESIGN §0 said 29.8.2), `containerd.io` `2.4.2-2`, `docker-buildx-plugin` `0.38.0-1`, `docker-compose-plugin` `5.6.0-1` (compose major version 5). The pins in `config/versions.lock.yaml` are updated to these. The Docker install itself and the firewall-backend check stay in FEAT-004.
