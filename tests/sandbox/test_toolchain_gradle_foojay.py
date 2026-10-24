"""Gradle's own toolchain provisioning (build-env design §6.2 M7, §15, AC 12, L-9): the toolchain version comes
from a Gradle property, so the resolver can't see it and installs nothing for it (no test-only switch in production
code). Fetch's warm-up resolves the project's toolchain with auto-download on, so Gradle's foojay resolver
(api.foojay.io, pkg_toolchains) provisions JDK 23 into ``$GRADLE_USER_HOME/jdks`` on the task volume; the offline
agent-exec rebuild uses it. Strict egress; L-12 except the foojay discovery and JDK hosts (both in the base list)."""

from __future__ import annotations

import time

from tests.sandbox.conftest import Sandbox
from tests.sandbox.toolchain_common import assert_l12, execd_runs, job_lines, log, prepare, tools, validate

CID = "c_TOOLCHAIN9"
RUN = "bash ./gradlew --no-daemon -q printJavaVersion"


def test_foojay_provisions_jdk_23_at_fetch_and_the_offline_rebuild_uses_it(sandbox: Sandbox) -> None:
    start = time.time() - 1
    try:
        info = prepare(sandbox, CID, "gradle-foojay")
        (computed,) = tools(info, "java-toolchain")
        assert computed["declared"] == "computed" and not [
            e for e in info["toolchains"] if e.get("action") == "install"
        ]
        assert info["prefetch"]["gradle"] == 0, info
        lines = job_lines(sandbox, CID, "fetch-strict", start)
        hosts = {e.host for e in lines}
        assert "api.foojay.io" in hosts, sorted(hosts)  # L-9: the JDK download host is in the base list too
        assert_l12(lines)
        out, done = validate(sandbox, CID, [
            {"name": "jdks", "cmd": "ls -1 /work/cache/gradle/jdks", "timeout_s": 60},
            {"name": "run", "cmd": RUN, "timeout_s": 900},
        ])  # fmt: skip
        assert done["jdks"]["exit_code"] == 0 and "23" in log(out, "jdks"), log(out, "jdks")
        assert done["run"]["exit_code"] == 0 and "java.version=23." in log(out, "run"), log(out, "run")[-2000:]
        replies = execd_runs(sandbox, CID, [{"id": 1, "cmd": RUN.replace("-q", "--offline -q"), "timeout_s": 900}])
        result = replies["1"]["result"]
        assert result["exit_code"] == 0 and "java.version=23." in result["stdout"], result
    finally:
        sandbox.cleanup(CID)
