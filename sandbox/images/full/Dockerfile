# ossbot/sandbox-full (DESIGN §10.1): code-running image for fetch, validate, package, unbundle,
# agent-exec, snapshot, stage-in and probe. No kiro-cli and no secrets, ever.
# Build (root, FEAT-004 'images' step, context = repo root):
#   docker build -f sandbox/images/full/Dockerfile -t ossbot/sandbox-full:<date> .
# Pins come from config/versions.lock.yaml (tests/unit/sandbox/test_gitcmd_image_env.py checks them).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG UV_VERSION=0.13.1
ARG UV_SHA256=eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220
ARG MISE_VERSION=2026.10.13
ARG MISE_SHA256=b775d3feee8f0cdb1934c60a1ec26526d6eb7cdeb258b48c64cfadcec07a8fec
ARG TOFU_VERSION=1.13.1
ARG KIND_VERSION=0.33.0
ARG GITLEAKS_VERSION=8.30.1

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    MISE_DATA_DIR=/opt/mise \
    MISE_CONFIG_DIR=/opt/mise/config \
    MISE_GLOBAL_CONFIG_FILE=/opt/mise/config/config.toml \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH=/opt/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Fixed git commands shared with the gatekeeper (ossbot.common.gitcmd FORMAT_PATCH / NUMSTAT, R4-N6).
ENV OSSBOT_FORMAT_PATCH="-c diff.algorithm=myers -c diff.renames=false -c core.quotePath=true format-patch --stdout --no-signature --no-stat --full-index --no-ext-diff --no-textconv --no-color"
ENV OSSBOT_NUMSTAT="-c diff.algorithm=myers -c diff.renames=false -c core.quotePath=true diff --numstat --no-ext-diff --no-textconv --no-color"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq dnsutils rsync build-essential shellcheck unzip xz-utils zstd \
      python3 python3-venv openjdk-17-jdk-headless openjdk-21-jdk-headless \
 && rm -rf /var/lib/apt/lists/*

# uv (pinned, sha256-verified) and Python 3.13 next to the distro's 3.12.
RUN curl -fsSL -o /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${UV_SHA256}  /tmp/uv.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 \
 && rm /tmp/uv.tar.gz \
 && uv python install 3.13

# mise (pinned, sha256-verified) and the toolchains it manages (shims in /opt/mise/shims).
RUN curl -fsSL -o /tmp/mise.tar.gz \
      "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64.tar.gz" \
 && echo "${MISE_SHA256}  /tmp/mise.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/mise.tar.gz -C /tmp \
 && install -m 0755 /tmp/mise/bin/mise /usr/local/bin/mise \
 && rm -rf /tmp/mise /tmp/mise.tar.gz \
 && mkdir -p /opt/mise/config \
 && mise use --global --yes node@lts go@latest "opentofu@${TOFU_VERSION}" terraform@latest tflint \
      terraform-docs kubectl helm "kind@${KIND_VERSION}" kubeconform pre-commit "gitleaks@${GITLEAKS_VERSION}" \
 && mise reshim \
 && corepack enable --install-directory /opt/mise/shims \
 && chmod -R a+rX /opt/mise /opt/uv-python

# Non-root user and the fixed directories with the M2 owners/modes (copy-up onto fresh volumes).
RUN groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -d /home/agent -s /bin/bash -M agent \
 && install -d -o 10001 -g 10001 -m 0700 /work /work/repo /io/output /run/exec /snap /home/agent \
 && install -d -o 0 -g 0 -m 0755 /io/input /in /opt/ossbot/bin /opt/ossbot/entrypoints /opt/ossbot/fixtures

COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/numstat2json.py /opt/ossbot/bin/numstat2json
COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/execd.py /opt/ossbot/bin/ossbot-execd
COPY --chown=0:0 --chmod=0755 sandbox/entrypoints/fetch.sh sandbox/entrypoints/exec.sh \
     sandbox/entrypoints/validate.sh sandbox/entrypoints/package.sh sandbox/entrypoints/unbundle.sh \
     sandbox/entrypoints/snapshot.sh sandbox/entrypoints/probe.sh /opt/ossbot/entrypoints/

# Safety-probe fixture as a tar so its symlinks survive (DESIGN §10.1, §10.9).
COPY --chown=0:0 sandbox/fixtures/ /tmp/fixtures/
RUN bash /tmp/fixtures/build-probe-tar.sh /tmp/fixtures/probe-repo /opt/ossbot/fixtures/probe-repo.tar \
 && chmod 0444 /opt/ossbot/fixtures/probe-repo.tar \
 && rm -rf /tmp/fixtures

USER 10001:10001
WORKDIR /
