# ossbot/sandbox-llm (DESIGN §10.1, §15.3): key-holding image for research, agent-llm and catalog.
# kiro-cli (headless tarball, sha256-verified) + the stdio MCP relay; no compilers, no package
# managers, no repo code. Build (root, FEAT-004 'images' step, context = repo root):
#   docker build -f sandbox/images/llm/Dockerfile -t ossbot/sandbox-llm:<date> \
#     --build-arg KIRO_CLI_URL=<lock> --build-arg KIRO_CLI_SHA256=<lock> .
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG KIRO_CLI_VERSION=2.32.0
ARG KIRO_CLI_URL=https://desktop-release.q.us-east-1.amazonaws.com/2.32.0/kirocli-x86_64-linux.tar.gz
ARG KIRO_CLI_SHA256=05989f9bfe465b304e8d1b2dba50aec2b84fcc1deaa4c0773af3f539d6e0d312

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

RUN groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -d /home/agent -s /bin/bash -M agent \
 && install -d -o 10001 -g 10001 -m 0700 /work /work/repo /io/output /run/exec /snap /home/agent \
 && install -d -o 0 -g 0 -m 0755 /io/input /in /opt/ossbot/bin /opt/ossbot/entrypoints \
      /opt/ossbot/kiro/agents /opt/ossbot/kiro/settings /opt/ossbot/prompts /opt/ossbot/probes

COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/exec_mcp.py /opt/ossbot/bin/ossbot-exec-mcp
COPY --chown=0:0 --chmod=0755 sandbox/entrypoints/kiro.sh sandbox/entrypoints/list_models.sh \
     sandbox/entrypoints/probe.sh /opt/ossbot/entrypoints/
COPY --chown=0:0 --chmod=0444 sandbox/kiro/settings/cli.json sandbox/kiro/settings/permissions.yaml \
     /opt/ossbot/kiro/settings/
COPY --chown=0:0 --chmod=0444 .kiro/agents/ /opt/ossbot/kiro/agents/
COPY --chown=0:0 --chmod=0444 prompts/ /opt/ossbot/prompts/
COPY --chown=0:0 --chmod=0444 sandbox/probes/ /opt/ossbot/probes/
RUN chmod 0755 /opt/ossbot/kiro/agents /opt/ossbot/prompts /opt/ossbot/probes

USER 10001:10001
WORKDIR /
