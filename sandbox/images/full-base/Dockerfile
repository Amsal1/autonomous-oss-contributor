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
ARG POPPLER_VERSION=24.02.0-1ubuntu9.11

# MISE_USE_VERSIONS_HOST(_TRACK)=false (build-env design §6.2, BE-4.8): mise makes no request to
# mise-versions.jdx.dev, neither in this build nor in a job (the entrypoints and execd set it again).
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    MISE_DATA_DIR=/opt/mise \
    MISE_CONFIG_DIR=/opt/mise/config \
    MISE_GLOBAL_CONFIG_FILE=/opt/mise/config/config.toml \
    MISE_USE_VERSIONS_HOST=false \
    MISE_USE_VERSIONS_HOST_TRACK=false \
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

# mise (pinned, sha256-verified) and the toolchains it manages (shims in /opt/mise/shims). The tools are
# pinned by exact version (mise.toml) and by download URL and checksum (mise.lock, generated once with
# `mise lock --global --platform linux-x64`); MISE_LOCKED=1 installs only what the lockfile names (build-env
# design §6.4, M8). The assertions: no floating version word in the config, and the installed set is exactly
# the pinned one. Only the entries that are not yet world-readable are chmod-ed: a recursive chmod over the
# ~100k toolchain files journals one metadata write per inode and took over 10 minutes on the virtual disk.
COPY --chown=0:0 --chmod=0644 sandbox/images/full-base/mise.toml /opt/mise/config/config.toml
COPY --chown=0:0 --chmod=0644 sandbox/images/full-base/mise.lock /opt/mise/config/mise.lock
RUN curl -fsSL -o /tmp/mise.tar.gz \
      "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64.tar.gz" \
 && echo "${MISE_SHA256}  /tmp/mise.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/mise.tar.gz -C /tmp \
 && install -m 0755 /tmp/mise/bin/mise /usr/local/bin/mise \
 && rm -rf /tmp/mise /tmp/mise.tar.gz \
 && ! grep -Eqw 'lts|latest' /opt/mise/config/config.toml \
 && MISE_LOCKED=1 mise install --yes \
 && mise ls --global --json | python3 -I -c 'import json, sys, tomllib; \
got = {k: sorted(e["version"] for e in v if e.get("installed")) for k, v in json.load(sys.stdin).items()}; \
want = {k: [v] for k, v in tomllib.load(open("/opt/mise/config/config.toml", "rb"))["tools"].items()}; \
sys.exit(0 if got == want else "mise ls --global differs from the pinned set")' \
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

# Everything below is appended after the code-job venv (sandbox-network Part 1, ADR-0005 addendum
# 2026-10-23): its ARGs are declared here, not at the top, because a RUN after an ARG uses it as an
# implicit env var and would miss the cache of every toolchain layer above (Dockerfile reference,
# "Impact on build caching"). Pins: config/versions.lock.yaml openjdk_25 and maven.
ARG OPENJDK25_VERSION=25.0.4.1+1-1~24.04.4
ARG MAVEN_VERSION=3.9.16
ARG MAVEN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6
# JDK 25 next to 17 and 21 (owner decision 2026-10-23); 21 stays the default java (asserted here,
# whatever priority the packages give their alternatives).
RUN apt-get update \
 && apt-get install -y --no-install-recommends "openjdk-25-jdk-headless=${OPENJDK25_VERSION}" \
 && rm -rf /var/lib/apt/lists/* \
 && for tool in /usr/lib/jvm/java-21-openjdk-amd64/bin/*; do \
      name="$(basename "$tool")"; \
      if update-alternatives --query "$name" >/dev/null 2>&1; then update-alternatives --set "$name" "$tool"; fi; \
    done \
 && java -version 2>&1 | grep -q 'version "21\.' \
 && javac -version 2>&1 | grep -q '^javac 21\.' \
 && test -x /usr/lib/jvm/java-25-openjdk-amd64/bin/javac -a -x /usr/lib/jvm/java-17-openjdk-amd64/bin/javac
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
# Apache Maven (F1), pinned and sha512-verified (noble ships 3.8.7, too old for many enforcer rules).
RUN curl -fsSL -o /tmp/maven.tar.gz \
      "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
 && echo "${MAVEN_SHA512}  /tmp/maven.tar.gz" | sha512sum -c - \
 && mkdir -p /opt/maven && tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1 --no-same-owner \
 && rm /tmp/maven.tar.gz && ln -s /opt/maven/bin/mvn /usr/local/bin/mvn \
 && mvn -v | grep -q "Apache Maven ${MAVEN_VERSION}"

# build-env (design §6.4, BE-4.5): the remaining fallbacks. Pins: config/versions.lock.yaml openjdk_11, openjdk_8,
# rustup, rust_default and corepack_fallback (tests/unit/sandbox/test_gitcmd_image_env.py asserts equality).
ARG OPENJDK11_VERSION=11.0.32.1+1-1ubuntu1~24.04
ARG OPENJDK8_VERSION=8u504-ga-1ubuntu1~24.04.3
ARG RUSTUP_VERSION=1.29.2
ARG RUSTUP_SHA256=5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4
ARG RUST_VERSION=1.99.1
ARG RUST_MANIFEST_SHA256=fb152be80975c194aa5fc684b2aec3a6e14e2b855efe2faa4e5f8ae6e590f463
ARG COREPACK_FALLBACK_VERSION=0.36.0
ARG COREPACK_FALLBACK_SHA512=4a2889b01859a9d0623c74c497a393dec012ad1aca21c61340cb15197c7a104fe033c5853306237885fc4edf11894e08bf627a2db4fc2a919f08eb01a51581ff
# JDK 11 and 8 next to 17, 21 and 25; 21 stays the default java (asserted, as above).
RUN apt-get update \
 && apt-get install -y --no-install-recommends "openjdk-11-jdk-headless=${OPENJDK11_VERSION}" \
      "openjdk-8-jdk-headless=${OPENJDK8_VERSION}" \
 && rm -rf /var/lib/apt/lists/* \
 && for tool in /usr/lib/jvm/java-21-openjdk-amd64/bin/*; do \
      name="$(basename "$tool")"; \
      if update-alternatives --query "$name" >/dev/null 2>&1; then update-alternatives --set "$name" "$tool"; fi; \
    done \
 && java -version 2>&1 | grep -q 'version "21\.' \
 && javac -version 2>&1 | grep -q '^javac 21\.' \
 && test -x /usr/lib/jvm/java-11-openjdk-amd64/bin/javac -a -x /usr/lib/jvm/java-8-openjdk-amd64/bin/javac
# Rust (F1): rustup-init pinned by sha256; the channel manifest of the default toolchain is checked against its
# pinned sha256 before rustup-init runs (the server served the pinned release in this build); rustup then checks
# every component against that manifest's hashes. Jobs link this toolchain as ossbot-<version> into their own
# RUSTUP_HOME on the task volume (sandbox_tools/toolchains.py); /opt/rust stays root-owned and read-only.
RUN curl -fsSL -o /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
 && echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum -c - \
 && curl -fsSL -o "/tmp/channel-rust-${RUST_VERSION}.toml" \
      "https://static.rust-lang.org/dist/channel-rust-${RUST_VERSION}.toml" \
 && echo "${RUST_MANIFEST_SHA256}  /tmp/channel-rust-${RUST_VERSION}.toml" | sha256sum -c - \
 && chmod 0755 /tmp/rustup-init \
 && RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo /tmp/rustup-init -y --no-modify-path --profile minimal \
      --default-toolchain "${RUST_VERSION}" -c rustfmt,clippy \
 && rm -f /tmp/rustup-init "/tmp/channel-rust-${RUST_VERSION}.toml" \
 && RUSTUP_HOME=/opt/rust/rustup /opt/rust/cargo/bin/rustc --version | grep -q "^rustc ${RUST_VERSION} " \
 && RUSTUP_HOME=/opt/rust/rustup /opt/rust/cargo/bin/rustup --version 2>/dev/null | grep -q "^rustup ${RUSTUP_VERSION} " \
 && test -x "/opt/rust/rustup/toolchains/${RUST_VERSION}-x86_64-unknown-linux-gnu/bin/cargo" \
 && find /opt/rust \( ! -perm -a+r -o \( -type d ! -perm -a+x \) -o \( -perm -u+x ! -perm -a+x \) \) \
      -exec chmod a+rX {} + \
 && find /opt/rust -perm /go+w -exec chmod go-w {} +
# corepack fallback for a Node without a bundled corepack (design §6.2, M8): the tarball is checked by sha512 here
# and installed offline at job time; no download and no unpinned install happen in a job.
RUN mkdir -p /opt/ossbot/corepack \
 && curl -fsSL -o "/opt/ossbot/corepack/corepack-${COREPACK_FALLBACK_VERSION}.tgz" \
      "https://registry.npmjs.org/corepack/-/corepack-${COREPACK_FALLBACK_VERSION}.tgz" \
 && echo "${COREPACK_FALLBACK_SHA512}  /opt/ossbot/corepack/corepack-${COREPACK_FALLBACK_VERSION}.tgz" | sha512sum -c - \
 && chmod 0644 "/opt/ossbot/corepack/corepack-${COREPACK_FALLBACK_VERSION}.tgz"
