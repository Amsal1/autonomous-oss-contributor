# ADR-0001: Container runtime is Docker Engine inside the ossbot distro

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §7.4, §10

## Context
Sandboxes need per-network egress control, Compose (stack validation), and kind. Options on this laptop: `wslc.exe` (WSL Containers, GA 2026-09-29, no compose), Docker Desktop (not installed, not wanted), Docker Engine inside the `ossbot` distro, Podman.

## Decision
Install Docker Engine (`docker-ce`, `containerd.io`, buildx, compose plugin) from Docker's apt repo inside `ossbot`, pinned in `config/versions.lock.yaml`, `data-root=/srv/ossbot/docker` on ext4. All container operations go through `ossbot.sandboxd.runtime.ContainerRuntime` (`DockerCliRuntime`, `FakeRuntime`; future `WslcRuntime`).

## Alternatives
- wslc: faster startup, but no compose, no documented per-network iptables hooks, no kind support today. Revisit when compose lands.
- Podman rootless: better default privilege, but slirp/pasta networking makes the proxy-only egress model and kind harder.

## Consequences
Docker group is root-equivalent; only `ossbox` (sandboxd) is a member and it builds argv from fixed templates. **Verify:** Docker 29 firewall backend on WSL kernel 6.18, `DOCKER-USER` chain presence, internal-network DNS behaviour.
