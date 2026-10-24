"""sandbox/entrypoints/lib/jvm_env.sh (sandbox-network Part 1, R1.1, R1.2, R1.4, R1.5; DESIGN §9.6, §9.8,
§9.9): the JVM proxy from HTTPS_PROXY, Gradle offline through an init script (never GRADLE_OPTS), the Maven
settings and toolchains, run with bash in a tmp WORK."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "sandbox" / "entrypoints" / "lib" / "jvm_env.sh"
TOOL_ENV = ROOT / "sandbox" / "entrypoints" / "lib" / "tool_env.sh"
GRADLE_DIR = ROOT / "sandbox" / "gradle"
JDK_HOMES = [f"/usr/lib/jvm/java-{v}-openjdk-amd64" for v in (8, 11, 17, 21, 25)]
PROXY_KEYS = ("systemProp.https.proxyHost=proxy", "systemProp.https.proxyPort=3128", "systemProp.http.proxyHost=proxy",
              "systemProp.http.proxyPort=3128", "systemProp.http.nonProxyHosts=localhost|127.*|[::1]")  # fmt: skip
DRIVER = (
    'source "$1"; ossbot_jvm_setup "$2"; rc=$?; echo "rc=$rc"; echo "GRADLE_OPTS=${GRADLE_OPTS-<unset>}";'
    ' echo "JAVA_TOOL_OPTIONS=${JAVA_TOOL_OPTIONS-<unset>}"; echo "JAVA_HOME=${JAVA_HOME-}";'
    ' echo "MAVEN_OPTS=${MAVEN_OPTS-}"; echo "MAVEN_USER_HOME=${MAVEN_USER_HOME-}"'
)

pytestmark = pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")


def run(work: Path, mode: str, proxy: str | None = "http://proxy:3128") -> tuple[dict[str, str], str]:
    env = {"PATH": "/usr/bin:/bin", "WORK": str(work), "HOME": str(work / ".home"),
           "GRADLE_USER_HOME": str(work / "cache" / "gradle"), "OSSBOT_GRADLE_DIR": str(GRADLE_DIR),
           "GRADLE_OPTS": "--offline", "JAVA_TOOL_OPTIONS": "-Dx=1"}  # fmt: skip
    if proxy is not None:
        env["HTTPS_PROXY"] = proxy
    proc = subprocess.run(["/bin/bash", "-c", DRIVER, "driver", str(LIB), mode], capture_output=True, text=True,
                          env=env, check=False, timeout=30)  # fmt: skip
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return out, proc.stderr


def files(work: Path) -> tuple[str, str, str, Path]:
    gprops = (work / "cache" / "gradle" / "gradle.properties").read_text(encoding="utf-8")
    settings = (work / ".home" / ".m2" / "settings.xml").read_text(encoding="utf-8")
    toolchains = (work / ".home" / ".m2" / "toolchains.xml").read_text(encoding="utf-8")
    return gprops, settings, toolchains, work / "cache" / "gradle" / "init.d" / "ossbot-offline.init.gradle"


def test_online_writes_the_proxy_and_the_jdks_and_no_offline_script(tmp_path: Path) -> None:
    out, _ = run(tmp_path, "online")
    assert out["rc"] == "0"
    gprops, settings, toolchains, init = files(tmp_path)
    for key in PROXY_KEYS:
        assert key in gprops.splitlines(), key
    assert f"org.gradle.java.installations.paths={','.join(JDK_HOMES)}" in gprops.splitlines()
    assert "org.gradle.java.installations.auto-download=true" in gprops.splitlines()  # BE-4.4: with a network
    assert "org.gradle.java.home" not in gprops  # no resolver output: Gradle's own choice
    assert not init.exists()
    assert f"<localRepository>{tmp_path}/cache/m2</localRepository>" in settings
    assert "<offline>false</offline>" in settings
    assert settings.count("<proxy>") == 2 and "<protocol>http</protocol>" in settings
    assert "<protocol>https</protocol>" in settings and "<host>proxy</host><port>3128</port>" in settings
    for home in JDK_HOMES:
        assert f"<jdkHome>{home}</jdkHome>" in toolchains
    assert out["MAVEN_OPTS"] == (f"-Duser.home={tmp_path}/.home -Dhttps.proxyHost=proxy -Dhttps.proxyPort=3128"
                                 " -Dhttp.proxyHost=proxy -Dhttp.proxyPort=3128")  # fmt: skip


@pytest.mark.parametrize("mode", ["validate", "offline"])
def test_validate_and_offline_write_the_offline_init_script(tmp_path: Path, mode: str) -> None:
    out, _ = run(tmp_path, mode)
    assert out["rc"] == "0"
    gprops, settings, toolchains, init = files(tmp_path)
    assert init.read_text(encoding="utf-8") == (GRADLE_DIR / "ossbot-offline.init.gradle").read_text(encoding="utf-8")
    assert "gradle.startParameter.offline = true" in init.read_text(encoding="utf-8")
    assert "org.gradle.java.installations.auto-download=false" in gprops.splitlines()
    assert f"<localRepository>{tmp_path}/cache/m2</localRepository>" in settings
    for v in ("8", "11", "17", "21", "25"):
        assert f"<version>{v}</version>" in toolchains
    if mode == "validate":  # V-M6: Maven stays online in validate (surefire providers), through the proxy
        assert "<offline>false</offline>" in settings and settings.count("<proxy>") == 2
        assert all(key in gprops for key in PROXY_KEYS)
        assert "-Dhttps.proxyHost=proxy" in out["MAVEN_OPTS"]
    else:  # no network in the offline agent-exec: no proxy anywhere
        assert "<offline>true</offline>" in settings and "<proxy>" not in settings
        assert "proxyHost" not in gprops and out["MAVEN_OPTS"] == f"-Duser.home={tmp_path}/.home"


def test_online_after_validate_removes_the_offline_script(tmp_path: Path) -> None:
    run(tmp_path, "validate")
    assert files(tmp_path)[3].exists()
    run(tmp_path, "online")
    assert not files(tmp_path)[3].exists()


@pytest.mark.parametrize("mode", ["online", "validate", "offline"])
def test_environment_after_every_mode(tmp_path: Path, mode: str) -> None:
    out, _ = run(tmp_path, mode)
    assert out["GRADLE_OPTS"] == "<unset>" and out["JAVA_TOOL_OPTIONS"] == "<unset>"
    assert out["JAVA_HOME"] == "/usr/lib/jvm/java-21-openjdk-amd64"
    assert out["MAVEN_USER_HOME"] == f"{tmp_path}/.home/.m2"
    assert out["MAVEN_OPTS"].startswith(f"-Duser.home={tmp_path}/.home")


@pytest.mark.parametrize("mode", ["", "bogus", "online;rm"])
def test_unknown_mode_fails_and_writes_nothing(tmp_path: Path, mode: str) -> None:
    out, err = run(tmp_path, mode)
    assert out["rc"] != "0" and "unknown mode" in err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "proxy",
    [
        None,
        "http://x;rm:1",
        "https://proxy:3128",
        "http://proxy:70000",
        "http://Proxy:3128",
        "http://proxy:0",
        "http://proxy",
        "http://u:p@proxy:3128",
    ],
)
def test_a_malformed_proxy_writes_no_proxy_lines(tmp_path: Path, proxy: str | None) -> None:
    out, err = run(tmp_path, "online", proxy)
    assert out["rc"] == "0" and "no usable HTTPS_PROXY" in err
    gprops, settings, _, _ = files(tmp_path)
    assert "proxy" not in gprops.lower() and "<proxy>" not in settings
    assert out["MAVEN_OPTS"] == f"-Duser.home={tmp_path}/.home"


# ------------------------------------------------------------- the resolver's toolchain.env (build-env §6.3)
TC_DRIVER = 'source "$1"; source "$2"; ossbot_jvm_setup "$3"; rc=$?; echo "rc=$rc"; echo "JAVA_HOME=${JAVA_HOME-}"'


def run_tc(work: Path, mode: str, env_file: str | None, net: str | None = None) -> dict[str, str]:
    if env_file is not None:
        (work / ".home" / ".ossbot").mkdir(parents=True, exist_ok=True)
        (work / ".home" / ".ossbot" / "toolchain.env").write_text(env_file, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "WORK": str(work), "HOME": str(work / ".home"),
           "GRADLE_USER_HOME": str(work / "cache" / "gradle"), "OSSBOT_GRADLE_DIR": str(GRADLE_DIR),
           "HTTPS_PROXY": "http://proxy:3128"}  # fmt: skip
    if net is not None:
        env["OSSBOT_NET"] = net
    proc = subprocess.run(["/bin/bash", "-c", TC_DRIVER, "driver", str(TOOL_ENV), str(LIB), mode],
                          capture_output=True, text=True, env=env, check=False, timeout=30)  # fmt: skip
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def test_the_resolvers_jdks_reach_java_home_gradle_and_maven(tmp_path: Path) -> None:
    temurin = f"{tmp_path}/cache/mise/installs/java/temurin-22.0.2+9"
    env_file = (f"JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64\nOSSBOT_GRADLE_JAVA_HOME={temurin}\n"
                f"OSSBOT_JDKS={temurin}\n")  # fmt: skip
    out = run_tc(tmp_path, "online", env_file)
    assert out["rc"] == "0" and out["JAVA_HOME"] == "/usr/lib/jvm/java-25-openjdk-amd64"  # floci: no prefix needed
    gprops, _, toolchains, _ = files(tmp_path)
    assert f"org.gradle.java.home={temurin}" in gprops.splitlines()
    assert f"org.gradle.java.installations.paths={','.join([*JDK_HOMES, temurin])}" in gprops.splitlines()
    assert (f"<version>22</version><vendor>temurin</vendor></provides><configuration><jdkHome>{temurin}</jdkHome>"
            in toolchains)  # fmt: skip
    assert "<version>25</version><vendor>openjdk</vendor>" in toolchains


@pytest.mark.parametrize("line", [
    "JAVA_HOME=/usr/lib/jvm/java-22-openjdk-amd64",  # not an image JDK
    "JAVA_HOME=/tmp/evil",
    "JAVA_HOME={work}/cache/mise/../../../etc",
    "JAVA_HOME={work}/repo/jdk",
    "JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64 -XX:x",
])  # fmt: skip
def test_a_malformed_java_home_line_falls_back_to_21(tmp_path: Path, line: str) -> None:
    out = run_tc(tmp_path, "offline", line.format(work=tmp_path) + "\nOSSBOT_JDKS=/tmp/x\n")
    assert out["rc"] == "0" and out["JAVA_HOME"] == "/usr/lib/jvm/java-21-openjdk-amd64"
    gprops, _, toolchains, _ = files(tmp_path)
    assert f"org.gradle.java.installations.paths={','.join(JDK_HOMES)}" in gprops.splitlines()
    assert "/tmp" not in toolchains and "/tmp" not in gprops


def test_the_env_file_is_never_sourced(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    out = run_tc(tmp_path, "offline", f"touch {marker}\nJAVA_HOME=$(touch {marker})\n`touch {marker}`\n")
    assert out["JAVA_HOME"] == "/usr/lib/jvm/java-21-openjdk-amd64" and not marker.exists()


@pytest.mark.parametrize(("net", "offline_gradle"), [("web", False), ("base", True), (None, True)])
def test_validate_runs_gradle_online_only_with_web(tmp_path: Path, net: str | None, offline_gradle: bool) -> None:
    """§8.2: validate with OSSBOT_NET=web runs Gradle online (no init script, auto-download on)."""
    run_tc(tmp_path, "validate", None, net)
    gprops, settings, _, init = files(tmp_path)
    assert init.exists() is offline_gradle
    want = "false" if offline_gradle else "true"
    assert f"org.gradle.java.installations.auto-download={want}" in gprops.splitlines()
    assert "<offline>false</offline>" in settings  # Maven online in validate either way (V-M6)


def test_existing_symlinks_are_replaced_not_followed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    targets = [work / "cache" / "gradle" / "gradle.properties", work / ".home" / ".m2" / "settings.xml",
               work / ".home" / ".m2" / "toolchains.xml",
               work / "cache" / "gradle" / "init.d" / "ossbot-offline.init.gradle"]  # fmt: skip
    for link in targets:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(victim, link)
    out, _ = run(work, "validate")
    assert out["rc"] == "0"
    assert victim.read_text(encoding="utf-8") == "keep\n"
    for path in targets:
        assert path.is_file() and not path.is_symlink(), path
        assert oct(path.stat().st_mode & 0o777) == "0o644", path
