"""Gradle 9.8.2 with ``languageVersion = 22`` (build-env design §6.2, §15, AC 12): the resolver installs Temurin 22
through mise at the re-fetch (an exact version from the mise-java index), Gradle finds it through
``org.gradle.java.installations.paths``; validate and the offline agent-exec run on it. Strict egress (the base
list), so it holds in every live mode; L-12 on the re-fetch's squid lines."""

from __future__ import annotations

import time

from tests.sandbox.conftest import Sandbox
from tests.sandbox.toolchain_common import assert_l12, execd_runs, job_lines, log, prepare, tools, validate

CID = "c_TOOLCHAIN1"
RUN = "bash ./gradlew --no-daemon -q printJavaVersion"


def test_gradle_toolchain_22_is_installed_and_reused_offline(sandbox: Sandbox) -> None:
    start = time.time() - 1
    try:
        info = prepare(sandbox, CID, "gradle-jdk22")
        (jdk,) = [e for e in tools(info, "java-toolchain") if e.get("declared") == "22"]
        assert jdk["action"] == "install" and jdk["chosen"].startswith("temurin-22."), info["toolchains"]
        assert info["prefetch"]["gradle"] == 0, info
        assert_l12(job_lines(sandbox, CID, "fetch-strict", start))
        out, done = validate(sandbox, CID, [{"name": "run", "cmd": RUN, "timeout_s": 900}])
        assert done["run"]["exit_code"] == 0, log(out, "run")[-3000:]
        assert "java.version=22." in log(out, "run"), log(out, "run")[-2000:]
        replies = execd_runs(sandbox, CID, [{"id": 1, "cmd": RUN.replace("-q", "--offline -q"), "timeout_s": 900}])
        assert replies["1"]["ok"] and replies["1"]["result"]["exit_code"] == 0, replies["1"]
        assert "java.version=22." in replies["1"]["result"]["stdout"], replies["1"]
    finally:
        sandbox.cleanup(CID)
