# ossbot/sandbox-full-base: the slow, rarely changing half of ossbot/sandbox-full (DESIGN §10.1): apt
# packages, uv + Python 3.13, the mise toolchains and the code-job venv. The 'images' step tags it
# ossbot/sandbox-full-base:in-<hash of this file, its build args and code-requirements.txt> and builds
# sandbox/images/full/Dockerfile (our scripts, a thin top) FROM it, so a script change never rebuilds a
# toolchain and a pruned build cache never loses the base (VERIFICATION 2026-10-19, redeploy timings).
# Keep the instructions above the code-job venv byte-identical when editing, or every toolchain
# layer is rebuilt; pins come from config/versions.lock.yaml (tests/unit/sandbox/test_gitcmd_image_env.py).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG UV_VERSION=0.13.1
ARG UV_SHA256=eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220
ARG MISE_VERSION=2026.10.13
ARG MISE_SHA256=b775d3feee8f0cdb1934c60a1ec26526d6eb7cdeb258b48c64cfadcec07a8fec
ARG TOFU_VERSION=1.13.1
ARG KIND_VERSION=0.33.0
ARG GITLEAKS_VERSION=8.30.1
ARG POPPLER_VERSION=24.02.0-1ubuntu9.10

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
      "poppler-utils=${POPPLER_VERSION}" \
 && rm -rf /var/lib/apt/lists/*

# uv (pinned, sha256-verified) and Python 3.13 next to the distro's 3.12.
RUN curl -fsSL -o /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${UV_SHA256}  /tmp/uv.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 \
 && rm /tmp/uv.tar.gz \
 && uv python install 3.13

# mise (pinned, sha256-verified) and the toolchains it manages (shims in /opt/mise/shims). Only the
# entries that are not yet world-readable are chmod-ed: a recursive chmod over the ~100k toolchain
# files journals one metadata write per inode and took over 10 minutes on the distro's virtual disk.
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
 && find /opt/mise /opt/uv-python \( ! -perm -a+r -o \( -type d ! -perm -a+x \) -o \( -perm -u+x ! -perm -a+x \) \) \
      -exec chmod a+rX {} + \
 && test -z "$(find /opt/mise /opt/uv-python ! -perm -a+r -print -quit)"

# Python for the assistant's code jobs (profile `code`, --network none; ADR-0020: Pillow only in
# network-none sandboxes): a root-owned venv from hash-pinned wheels (code-requirements.txt). uv's
# download cache is a BuildKit cache mount, so a requirements bump re-downloads only what changed.
COPY --chown=0:0 --chmod=0444 sandbox/images/full/code-requirements.txt /tmp/code-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv --python /usr/bin/python3 /opt/ossbot/code-py \
 && uv pip install --python /opt/ossbot/code-py/bin/python --require-hashes --no-deps --only-binary :all: \
      -r /tmp/code-requirements.txt \
 && /opt/ossbot/code-py/bin/python -c "import PIL, numpy, pandas" \
 && rm /tmp/code-requirements.txt \
 && find /opt/ossbot/code-py -perm /go+w -exec chmod go-w {} +
