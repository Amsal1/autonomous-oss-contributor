"""JVM builds in the sandbox (sandbox-network Part 1; DESIGN §9.6, §9.8, §9.9, §10.1; ADR-0005 addendum
2026-10-23). Real sandbox-full containers through the Docker filter (ROOTDEV pytest -m sandbox), written in
the build and run in the close-out after the image rebuild.

Flow per fixture (tests/fixtures/sandbox/gradle-multi, maven-multi): fetch octocat/Hello-World (depth 1,
``egress: strict``, like test_cfn_lint_on_demand.py), a validate job that untars the fixture into /work/repo
and ``git add``s it, a fetch job in refetch mode (the prefetch runs over the fixture and ``deps_changed``
lists its build files), then a strict validate job under the exact validate settings
(``ossbot_jvm_setup validate``): Gradle offline through the init script, Maven online through the proxy.

V-G3 (design §2): a configuration the warm-up never saw must fail with "offline mode"; that proves the
init script's ``gradle.startParameter.offline = true`` reaches the resolver. If it doesn't, the close-out
applies the pre-agreed fallback (validate.DEFAULTS and the prompts say "pass --offline") and this case is
changed to assert it. The fixture uses its own task ``:app:resolveProbeExtra`` instead of
``dependencies --configuration``, because the dependencies report prints unresolved entries and exits 0.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import Path
from typing import Any

from ossbot.sandboxd.jobs import Outcome
from tests.sandbox.conftest import Sandbox

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "sandbox"
GRADLE_CID = "c_SMOKETEST7"
MAVEN_CID = "c_SMOKETEST8"
CLONE_ENV = {"OSSBOT_ARG_REPO": "octocat/Hello-World", "OSSBOT_ARG_CLONE_DEPTH": "1"}
JDK25 = "JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64"


def fixture_tar(name: str) -> bytes:
    """The fixture tree as a tar (uid 10001, gradlew/mvnw/build.sh executable); no .git, nothing outside the tree."""
    buf = io.BytesIO()
    base = FIXTURES / name
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for path in sorted(base.rglob("*")):
            info = archive.gettarinfo(str(path), arcname=str(path.relative_to(base)))
            info.uid = info.gid = 10001
            info.uname = info.gname = ""
            if path.is_file():
                info.mode = 0o755 if path.name in ("gradlew", "mvnw", "build.sh") else 0o644
                with path.open("rb") as fh:
                    archive.addfile(info, fh)
            else:
                info.mode = 0o755
                archive.addfile(info)
    return buf.getvalue()


def validate(
    sandbox: Sandbox, cid: str, commands: list[dict[str, Any]], *, inputs: dict[str, bytes] | None = None,
    timeout: int = 3600,
) -> tuple[Outcome, dict[str, Any]]:  # fmt: skip
    """One strict validate job running ``commands``; returns the outcome and its commands by name."""
    staged: dict[str, Path] = {}
    root = sandbox.root / "inputs" / cid
    root.mkdir(parents=True, exist_ok=True)
    for name, data in {**(inputs or {}), "jvm.json": json.dumps({"commands": commands}).encode()}.items():
        (root / name).write_bytes(data)
        staged[name] = root / name
    plan = sandbox.plan(cid, "validate", entrypoint="validate", repo_mode="rw", egress="strict",
                        env={"OSSBOT_ARG_PLAN_FILE": "jvm.json"}, inputs=staged, timeout=timeout)  # fmt: skip
    out = sandbox.run(plan)
    assert out.exit_code == 0, out.logs[-2000:]
    done = {c["name"]: c for c in json.loads(out.files["validation.json"])["commands"]}
    return out, done


def log(out: Outcome, name: str) -> str:
    return out.files.get(f"logs/{name}.log", b"").decode("utf-8", "replace")


def prepare(sandbox: Sandbox, cid: str, fixture: str) -> dict[str, Any]:
    """Clone, untar the fixture over it, refetch: the task volume an implement stage would have after its
    one re-fetch. Returns the refetch's fetch.json."""
    clone = sandbox.run(sandbox.plan(cid, "fetch", entrypoint="fetch", repo_mode="rw", create_task=True,
                                     egress="strict", env=CLONE_ENV))  # fmt: skip
    assert clone.exit_code == 0, clone.logs[-2000:]
    _, done = validate(sandbox, cid, [{"name": "untar", "cmd": "tar -xf /io/input/fixture.tar && git add -A",
                                       "timeout_s": 120}], inputs={"fixture.tar": fixture_tar(fixture)})  # fmt: skip
    assert done["untar"]["exit_code"] == 0, done["untar"]
    refetch = sandbox.run(sandbox.plan(cid, "fetch", entrypoint="fetch", repo_mode="rw", egress="strict",
                                       env={**CLONE_ENV, "OSSBOT_ARG_CLONE_DEPTH": "200", "OSSBOT_ARG_REFETCH": "1"},
                                       timeout=2400))  # fmt: skip
    assert refetch.exit_code == 0, refetch.logs[-2000:]
    info: dict[str, Any] = json.loads(refetch.files["fetch.json"])
    assert info["refetch"] is True
    return info


def test_gradle_multi_project_builds_offline_after_the_refetch(sandbox: Sandbox) -> None:
    try:
        info = prepare(sandbox, GRADLE_CID, "gradle-multi")
        assert "gradle" in info["ecosystems"] and info["prefetch"]["gradle"] == 0, info
        assert {"settings.gradle", "app/build.gradle", "lib/build.gradle"} <= set(info["deps_changed"]), info
        assert "gradle" not in info["prefetch_failed"]
        out, done = validate(sandbox, GRADLE_CID, [
            {"name": "java-version", "cmd": "java -version 2>&1 | head -1", "timeout_s": 60},
            {"name": "toolchains", "cmd": "bash ./gradlew --no-daemon -q javaToolchains", "timeout_s": 600},
            {"name": "gradle-test", "cmd": "bash ./gradlew --no-daemon test", "timeout_s": 1200},
            {"name": "offline-probe", "cmd": "bash ./gradlew --no-daemon -PossbotProbeExtra :app:resolveProbeExtra",
             "timeout_s": 600},
        ])  # fmt: skip
        assert 'version "21.' in done["java-version"]["tail"], done["java-version"]
        assert done["toolchains"]["exit_code"] == 0, done["toolchains"]
        found = log(out, "toolchains")
        for version in ("17", "21", "25"):
            assert re.search(rf"Language Version:\s+{version}\b", found), found[-2000:]
        assert done["gradle-test"]["exit_code"] == 0, log(out, "gradle-test")[-3000:]
        assert "AppTest > runsOnTheJdk25Toolchain() PASSED" in log(out, "gradle-test")
        probe = log(out, "offline-probe")
        assert done["offline-probe"]["exit_code"] != 0 and "offline mode" in probe, probe[-3000:]  # V-G3
    finally:
        sandbox.cleanup(GRADLE_CID)


def test_maven_multi_module_under_the_validate_settings(sandbox: Sandbox) -> None:
    try:
        info = prepare(sandbox, MAVEN_CID, "maven-multi")
        assert "maven" in info["ecosystems"] and info["prefetch"]["maven"] == 0, info
        assert info["prefetch"]["maven-compile"] == 0, info
        assert {"pom.xml", "app/pom.xml", "core/pom.xml"} <= set(info["deps_changed"]), info
        out, done = validate(sandbox, MAVEN_CID, [
            {"name": "mvn-test", "cmd": "mvn -B -ntp test", "timeout_s": 1200},
            {"name": "provider", "cmd": "ls -d /work/cache/m2/org/apache/maven/surefire/surefire-junit-platform",
             "timeout_s": 60},
            {"name": "jdk25", "cmd": f"{JDK25} mvn -B -ntp -pl jdk25 -am test", "timeout_s": 1200},
            {"name": "tc17", "cmd": "mvn -B -ntp -pl tc17 -am compile", "timeout_s": 600},
        ])  # fmt: skip
        test_log = log(out, "mvn-test")
        assert done["mvn-test"]["exit_code"] == 0, test_log[-3000:]
        runs = [int(n) for n in re.findall(r"Tests run: (\d+), Failures: 0", test_log)]
        assert runs and max(runs) >= 1, test_log[-3000:]  # JUnit 5 tests really ran (V-M6)
        assert done["provider"]["exit_code"] == 0, done["provider"]
        assert done["jdk25"]["exit_code"] == 0, log(out, "jdk25")[-3000:]
        assert "Tests run: 1, Failures: 0" in log(out, "jdk25")
        assert done["tc17"]["exit_code"] == 0, log(out, "tc17")[-3000:]
        assert "Toolchain" in log(out, "tc17") or "toolchain" in log(out, "tc17")
    finally:
        sandbox.cleanup(MAVEN_CID)
