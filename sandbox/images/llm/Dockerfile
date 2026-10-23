# ossbot/sandbox-llm (DESIGN §10.1, §15.3): key-holding image for research, agent-llm and catalog.
# kiro-cli (headless tarball, sha256-verified) + the stdio MCP relay; no compilers, no package
# managers, no repo code. Build (root, FEAT-004 'images' step, context = repo root):
#   docker build -f sandbox/images/llm/Dockerfile -t ossbot/sandbox-llm:<date> \
#     --build-arg KIRO_CLI_URL=<lock> --build-arg KIRO_CLI_SHA256=<lock> .
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG KIRO_CLI_VERSION=2.33.1
ARG KIRO_CLI_URL=https://desktop-release.q.us-east-1.amazonaws.com/2.33.1/kirocli-x86_64-linux.tar.gz
ARG KIRO_CLI_SHA256=5993158abeb1a3086a591d5c7dd77e3db3c889a3c0d8b41418bc6a1af79400fb

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PATH=/opt/kiro-cli/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git python3 \
 && rm -rf /var/lib/apt/lists/*

# kiro-cli headless (manifest variant=headless, fileType=tarGz, x86_64). The binaries are copied
# to /opt/kiro-cli/bin; the tarball layout is checked here so a changed layout fails the build.
RUN curl -fsSL -o /tmp/kiro.tar.gz "${KIRO_CLI_URL}" \
 && echo "${KIRO_CLI_SHA256}  /tmp/kiro.tar.gz" | sha256sum -c - \
 && mkdir -p /tmp/kiro /opt/kiro-cli/bin \
 && tar -xzf /tmp/kiro.tar.gz -C /tmp/kiro \
 && bin="$(dirname "$(find /tmp/kiro -type f -name kiro-cli -perm -u+x | head -n 1)")" \
 && test -x "$bin/kiro-cli" \
 && cp -a "$bin/." /opt/kiro-cli/bin/ \
 && rm -rf /tmp/kiro /tmp/kiro.tar.gz \
 && /opt/kiro-cli/bin/kiro-cli --version | grep -F "${KIRO_CLI_VERSION}"

# `chat --v3` extracts its engine (a node binary and the KAS bundle, ~270 MB) into
# $HOME/.local/share/kiro-cli and executes node from there (ADR-0011 addendum, FEAT-006 item 32).
# Job HOMEs are noexec tmpfs, so the engine is extracted once here, offline (no network) with a
# placeholder key that never leaves the build, into /opt/kiro-cli/engine (root-owned, read-only);
# kiro.sh links it into each job's HOME. A missing node or KAS bundle fails the build.
RUN --network=none mkdir -p /tmp/bake /opt/kiro-cli/engine/kas \
 && (echo hi | HOME=/tmp/bake KIRO_API_KEY="ksk_$(printf 'x%.0s' $(seq 32))" timeout 120 \
      /opt/kiro-cli/bin/kiro-cli chat --v3 --no-interactive --output-format stream-json >/tmp/bake.log 2>&1 || true) \
 && d=/tmp/bake/.local/share/kiro-cli \
 && test -x "$d/node" && test -s "$d/node.sha256" \
 && for k in "$d"/kas/*; do if [ -d "$k" ]; then cp -a "$k" /opt/kiro-cli/engine/kas/; fi; done \
 && ls -d /opt/kiro-cli/engine/kas/*/ \
 && cp -a "$d/node" "$d/node.sha256" /opt/kiro-cli/engine/ \
 && chown -R 0:0 /opt/kiro-cli/engine && chmod -R a+rX,go-w /opt/kiro-cli/engine \
 && rm -rf /tmp/bake /tmp/bake.log

# github-mcp-server (read-only GitHub tools for scout and self-review, GHMCP-1): the pinned release
# binary, sha256-verified, root-owned 0755; only ever started through ossbot-github-mcp (fixed flags).
ARG GITHUB_MCP_VERSION=2.0.2
ARG GITHUB_MCP_URL=https://github.com/github/github-mcp-server/releases/download/v2.0.2/github-mcp-server_Linux_x86_64.tar.gz
ARG GITHUB_MCP_SHA256=815e27d931c796af0c97f24fd211e14f8a4607d4528377719a5ad2352086b0b0
RUN curl -fsSL -o /tmp/ghmcp.tar.gz "${GITHUB_MCP_URL}" \
 && echo "${GITHUB_MCP_SHA256}  /tmp/ghmcp.tar.gz" | sha256sum -c - \
 && mkdir -p /tmp/ghmcp /opt/ossbot/bin \
 && tar -xzf /tmp/ghmcp.tar.gz -C /tmp/ghmcp github-mcp-server \
 && install -o 0 -g 0 -m 0755 /tmp/ghmcp/github-mcp-server /opt/ossbot/bin/github-mcp-server \
 && rm -rf /tmp/ghmcp /tmp/ghmcp.tar.gz \
 && /opt/ossbot/bin/github-mcp-server --version | grep -F "${GITHUB_MCP_VERSION}"

RUN groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -d /home/agent -s /bin/bash -M agent \
 && install -d -o 10001 -g 10001 -m 0700 /work /work/repo /io/output /run/exec /snap /home/agent \
 && install -d -o 0 -g 0 -m 0755 /io/input /in /opt/ossbot/bin /opt/ossbot/entrypoints \
      /opt/ossbot/kiro/agents /opt/ossbot/kiro/settings /opt/ossbot/prompts /opt/ossbot/probes

COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/exec_mcp.py /opt/ossbot/bin/ossbot-exec-mcp
COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/github_mcp.py /opt/ossbot/bin/ossbot-github-mcp
COPY --chown=0:0 --chmod=0755 sandbox/entrypoints/kiro.sh sandbox/entrypoints/list_models.sh \
     sandbox/entrypoints/probe.sh /opt/ossbot/entrypoints/
COPY --chown=0:0 --chmod=0444 sandbox/kiro/settings/cli.json sandbox/kiro/settings/permissions.yaml \
     /opt/ossbot/kiro/settings/
COPY --chown=0:0 --chmod=0444 .kiro/agents/ /opt/ossbot/kiro/agents/
COPY --chown=0:0 --chmod=0444 prompts/ /opt/ossbot/prompts/
COPY --chown=0:0 --chmod=0444 sandbox/probes/ /opt/ossbot/probes/
RUN chmod 0755 /opt/ossbot/kiro/agents /opt/ossbot/prompts /opt/ossbot/probes \
 && install -d -o 0 -g 0 -m 0755 /run/ossbot-probe \
 && printf 'ossbot research_deny fixture: a harmless file under the denied /run root\n' \
      >/run/ossbot-probe/inventory.txt && chmod 0444 /run/ossbot-probe/inventory.txt

USER 10001:10001
WORKDIR /
