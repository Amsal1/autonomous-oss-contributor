# ossbot/sandbox-full (DESIGN §10.1): code-running image for fetch, validate, package, unbundle,
# agent-exec, snapshot, stage-in, probe, extract_text (PDF text, §14.12.5) and run_code (the assistant's code
# jobs, §15.6). No kiro-cli and no secrets, ever.
# The thin top over ossbot/sandbox-full-base (toolchains, sandbox/images/full-base/Dockerfile): only our
# scripts and the fixed directories, so a script change rebuilds seconds of layers. Build (root, the
# 'images' step, context = repo root; BASE is the base's local in-<hash> tag):
#   docker build -f sandbox/images/full/Dockerfile --build-arg BASE=ossbot/sandbox-full-base:in-<hash> \
#     -t ossbot/sandbox-full:<tag> .
ARG BASE=ossbot/sandbox-full-base:missing
FROM ${BASE}

# Non-root user and the fixed directories with the M2 owners/modes (copy-up onto fresh volumes).
RUN groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -d /home/agent -s /bin/bash -M agent \
 && install -d -o 10001 -g 10001 -m 0700 /work /work/repo /io/output /run/exec /snap /home/agent \
 && install -d -o 0 -g 0 -m 0755 /io/input /in /opt/ossbot/bin /opt/ossbot/entrypoints /opt/ossbot/fixtures

COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/numstat2json.py /opt/ossbot/bin/numstat2json
COPY --chown=0:0 --chmod=0755 src/ossbot/sandbox_tools/execd.py /opt/ossbot/bin/ossbot-execd
COPY --chown=0:0 --chmod=0755 sandbox/entrypoints/fetch.sh sandbox/entrypoints/exec.sh \
     sandbox/entrypoints/validate.sh sandbox/entrypoints/package.sh sandbox/entrypoints/unbundle.sh \
     sandbox/entrypoints/snapshot.sh sandbox/entrypoints/probe.sh sandbox/entrypoints/extract_text.sh \
     sandbox/entrypoints/run_code.sh /opt/ossbot/entrypoints/

# Safety-probe fixture as a tar so its symlinks survive (DESIGN §10.1, §10.9).
COPY --chown=0:0 sandbox/fixtures/ /tmp/fixtures/
RUN bash /tmp/fixtures/build-probe-tar.sh /tmp/fixtures/probe-repo /opt/ossbot/fixtures/probe-repo.tar \
 && chmod 0444 /opt/ossbot/fixtures/probe-repo.tar \
 && rm -rf /tmp/fixtures

USER 10001:10001
WORKDIR /
