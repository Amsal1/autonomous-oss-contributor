"""The floci regression (c_D1D31ZG6DA, build-env design §6.7, §8.5, BE-10.1, AC 12): an only-script ``mvnw`` and
``maven.compiler.release 25``. The resolver selects the image's JDK 25 at the re-fetch, so ``./mvnw -B -ntp verify``
runs in validate and in the offline agent-exec with no ``JAVA_HOME=`` prefix (the failure was mvnw's "JAVA_HOME …
not defined correctly, this environment variable is needed to run this program"). Strict egress.

O-3 (not an AC-12 case): one networked agent-exec run resolves an artifact nobody fetched before; it runs only when
the live egress mode is a web mode and is skipped with that reason otherwise."""

from __future__ import annotations

import dataclasses
import json

import pytest

from ossbot.common.control_flags import CODE_WEB_MODES
from tests.sandbox.conftest import Sandbox
from tests.sandbox.test_profiles import live_mode
from tests.sandbox.toolchain_common import execd_runs, log, prepare, tool, validate

CID = "c_TOOLCHAIN2"
NET_CID = "c_TOOLCHAIN3"
JDK25 = "/usr/lib/jvm/java-25-openjdk-amd64"
NEEDED = "this environment variable is needed to run this program"


def test_mvnw_release_25_needs_no_java_home_prefix(sandbox: Sandbox) -> None:
    try:
        info = prepare(sandbox, CID, "maven-release25")
        java = tool(info, "java")
        assert java["chosen"] == "25" and java["action"] == "image", info["toolchains"]
        assert tool(info, "maven")["chosen"] == "mvnw"
        assert info["prefetch"]["maven"] == 0 and info["prefetch"]["maven-compile"] == 0, info
        out, done = validate(sandbox, CID, [
            {"name": "verify", "cmd": "./mvnw -B -ntp verify", "timeout_s": 1200},
            {"name": "java-home", "cmd": 'echo "$JAVA_HOME"; java -version 2>&1 | head -1', "timeout_s": 60},
        ])  # fmt: skip
        verify = log(out, "verify")
        assert done["verify"]["exit_code"] == 0 and NEEDED not in verify, verify[-3000:]
        assert "Tests run: 1, Failures: 0, Errors: 0" in verify, verify[-3000:]
        home, version = log(out, "java-home").splitlines()[:2]
        assert home == JDK25 and 'version "25.' in version
        replies = execd_runs(sandbox, CID, [
            {"id": 1, "cmd": "./mvnw -B -ntp verify", "timeout_s": 1200},
            {"id": 2, "cmd": 'echo "$JAVA_HOME $OSSBOT_EXEC_NETWORK"', "timeout_s": 30},
        ])  # fmt: skip
        first = replies["1"]["result"]
        assert first["exit_code"] == 0 and NEEDED not in first["stdout"] + first["stderr"], first["stdout"][-3000:]
        assert "Tests run: 1, Failures: 0, Errors: 0" in first["stdout"]
        assert replies["2"]["result"]["stdout"].split() == [JDK25, "none"]
    finally:
        sandbox.cleanup(CID)


def test_networked_agent_exec_installs_what_the_build_needs(sandbox: Sandbox) -> None:
    mode = live_mode()
    if mode not in CODE_WEB_MODES:
        pytest.skip(f"the live egress mode is {mode}: the coder's agent-exec gets a network only in a web mode (O-3)")
    try:
        prepare(sandbox, NET_CID, "maven-release25")
        runs = [{"id": 1, "cmd": 'echo "$OSSBOT_EXEC_NETWORK $JAVA_HOME"', "timeout_s": 30},
                {"id": 2, "timeout_s": 900, "cmd": "./mvnw -B -ntp org.apache.maven.plugins:maven-dependency-plugin:"
                 "3.11.0:get -Dartifact=org.apache.commons:commons-text:1.13.0"}]  # fmt: skip
        src = sandbox.root / "inputs" / NET_CID / "execd-run.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps(runs), encoding="utf-8")
        plan = sandbox.plan(NET_CID, "coder", repo_mode="rw", env={"OSSBOT_PROBE": "execd_run"},
                            inputs={"execd-run.json": src}, timeout=1800)  # fmt: skip
        plan = dataclasses.replace(plan, exec_network="web", exec_env={"OSSBOT_EXEC_NETWORK": "web"}, net="web")
        out = sandbox.run(plan)
        assert out.exit_code == 0, out.logs[-3000:]
        replies = json.loads(out.files["execd.json"])
        assert replies["1"]["result"]["stdout"].split() == ["web", JDK25]
        assert replies["2"]["result"]["exit_code"] == 0, replies["2"]["result"]["stdout"][-3000:]
    finally:
        sandbox.cleanup(NET_CID)
