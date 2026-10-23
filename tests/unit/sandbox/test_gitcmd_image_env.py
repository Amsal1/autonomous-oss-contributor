"""Image pins (DESIGN §10.1, R4-N6): the git commands baked into sandbox-full equal ossbot.common.gitcmd,
and every Dockerfile pin equals config/versions.lock.yaml."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from ossbot.common import gitcmd
from ossbot.common.schemas import SANDBOX_JOB_ENTRYPOINTS
from ossbot.common.yamlio import safe_load

ROOT = Path(__file__).resolve().parents[3]
IMAGES = ROOT / "sandbox" / "images"
LOCK = safe_load((ROOT / "config" / "versions.lock.yaml").read_bytes())


def _dockerfile(name: str) -> str:
    return (IMAGES / name / "Dockerfile").read_text(encoding="utf-8")


def _env(text: str, name: str) -> str:
    match = re.search(rf'^ENV {name}="([^"]*)"$', text, re.MULTILINE)
    assert match, name
    return match.group(1)


def _arg(text: str, name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", text, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_sandbox_full_env_equals_gitcmd() -> None:
    full = _dockerfile("full-base")
    assert _env(full, "OSSBOT_FORMAT_PATCH") == gitcmd.FORMAT_PATCH
    assert _env(full, "OSSBOT_NUMSTAT") == gitcmd.NUMSTAT


@pytest.mark.parametrize("image", ["full-base", "llm", "proxy"])
def test_base_image_pinned_by_lock_digest(image: str) -> None:
    froms = re.findall(r"^FROM (\S+)", _dockerfile(image), re.MULTILINE)
    assert froms == [f"ubuntu:24.04@{LOCK['base_images']['ubuntu_24_04']}"]


def test_sandbox_full_is_a_thin_top_over_its_base() -> None:
    """Scripts live only in the top, toolchains only in the base (a script change never rebuilds them)."""
    top, base = _dockerfile("full"), _dockerfile("full-base")
    assert re.findall(r"^FROM (\S+)", top, re.MULTILINE) == ["${BASE}"]
    assert "apt-get" not in top and "mise" not in top and "uv pip" not in top
    assert "sandbox/entrypoints" not in base and "src/ossbot" not in base
    assert "chmod -R" not in base  # one journaled metadata write per toolchain inode (VERIFICATION 2026-10-19)


def test_tool_pins_equal_lock() -> None:
    full, llm, proxy = _dockerfile("full-base"), _dockerfile("llm"), _dockerfile("proxy")
    assert _arg(full, "UV_VERSION") == LOCK["uv"]["version"]
    assert _arg(full, "UV_SHA256") == LOCK["uv"]["sha256"]
    assert _arg(full, "MISE_VERSION") == LOCK["mise"]["version"]
    assert _arg(full, "MISE_SHA256") == LOCK["mise"]["sha256"]
    assert _arg(full, "TOFU_VERSION") == LOCK["tofu"]["version"]
    assert _arg(full, "KIND_VERSION") == LOCK["kind"]["version"]
    assert _arg(full, "GITLEAKS_VERSION") == LOCK["gitleaks"]["version"]
    assert (
        _arg(llm, "KIRO_CLI_VERSION")
        == LOCK["kiro_cli"]["version"]
        == LOCK["images"]["sandbox_llm"]["kiro_cli_version"]
    )
    assert _arg(llm, "KIRO_CLI_URL") == LOCK["kiro_cli"]["headless_tar_gz_url"]
    assert _arg(llm, "KIRO_CLI_SHA256") == LOCK["kiro_cli"]["headless_tar_gz_sha256"]
    assert _arg(proxy, "SQUID_VERSION") == LOCK["squid"]["apt_version"]
    assert _arg(full, "POPPLER_VERSION") == LOCK["poppler_utils"]["apt_version"]


@pytest.mark.parametrize("image", ["full", "llm"])
def test_fixed_directories_with_m2_owners(image: str) -> None:
    text = _dockerfile(image)
    assert "install -d -o 10001 -g 10001 -m 0700 /work /work/repo /io/output /run/exec /snap /home/agent" in text
    assert "install -d -o 0 -g 0 -m 0755 /io/input /in" in text
    assert re.search(r"^USER 10001:10001$", text, re.MULTILINE)


def test_tools_and_entrypoints_baked_where_needed() -> None:
    full, llm = _dockerfile("full"), _dockerfile("llm")
    assert "src/ossbot/sandbox_tools/numstat2json.py /opt/ossbot/bin/numstat2json" in full
    assert "src/ossbot/sandbox_tools/execd.py /opt/ossbot/bin/ossbot-execd" in full
    assert "build-probe-tar.sh /tmp/fixtures/probe-repo /opt/ossbot/fixtures/probe-repo.tar" in full
    for entry in ("fetch", "exec", "validate", "package", "unbundle", "snapshot", "probe", "extract_text", "run_code"):
        assert f"sandbox/entrypoints/{entry}.sh" in full, entry
    assert "src/ossbot/sandbox_tools/exec_mcp.py /opt/ossbot/bin/ossbot-exec-mcp" in llm
    for entry in ("kiro", "list_models", "probe"):
        assert f"sandbox/entrypoints/{entry}.sh" in llm, entry
    assert "execd.py" not in llm and "build-essential" not in llm


# sha256 of sandbox/images/full-base/Dockerfile up to and including the code-job venv RUN: the JDK 25 / Maven
# layers are appended after it, so no toolchain layer above is rebuilt. Baseline 12856539 plus one change: the
# poppler-utils pin 24.02.0-1ubuntu9.10 -> 9.11 (2026-10-24; 9.10 left the noble index).
BASE_PREFIX_SHA256 = "5a6f72efd7386c48a5a20a0cb50f7360a26e47d791c8230ed9a0abf2846b3b3f"
VENV_RUN_END = " && find /opt/ossbot/code-py -perm /go+w -exec chmod go-w {} +\n"


def test_jdk25_and_maven_pins_equal_lock() -> None:
    full = _dockerfile("full-base")
    assert _arg(full, "OPENJDK25_VERSION") == LOCK["openjdk_25"]["apt_version"]
    assert _arg(full, "MAVEN_VERSION") == LOCK["maven"]["version"]
    assert _arg(full, "MAVEN_SHA512") == LOCK["maven"]["sha512"]
    assert LOCK["maven"]["url"].endswith(f"/{LOCK['maven']['version']}/binaries/apache-maven-"
                                         f"{LOCK['maven']['version']}-bin.tar.gz")  # fmt: skip
    assert "assertion-backed" in LOCK["openjdk_25"]["default_java"]
    assert re.search(r"^ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64$", full, re.MULTILINE)
    assert "grep -q 'version \"21\\.'" in full and "update-alternatives --set" in full
    assert 'echo "${MAVEN_SHA512}  /tmp/maven.tar.gz" | sha512sum -c -' in full


def test_new_base_layers_come_after_the_code_job_venv() -> None:
    """Only the new layers rebuild: the earlier text is the byte-identical baseline prefix, and no new ARG
    (a RUN after an ARG uses it implicitly and misses the cache) is declared above the venv RUN."""
    raw = (IMAGES / "full-base" / "Dockerfile").read_bytes()
    text = raw.decode("utf-8")
    end = text.index(VENV_RUN_END) + len(VENV_RUN_END)
    assert hashlib.sha256(raw[: len(text[:end].encode("utf-8"))]).hexdigest() == BASE_PREFIX_SHA256
    prefix, rest = text[:end], text[end:]
    for name in ("OPENJDK25_VERSION", "MAVEN_VERSION", "MAVEN_SHA512"):
        assert f"ARG {name}=" not in prefix and f"ARG {name}=" in rest
    assert "openjdk-25-jdk-headless" in rest and "openjdk-25" not in prefix
    assert "/usr/local/bin/mvn" in rest


def test_thin_top_bakes_the_jvm_library_and_the_gradle_init_scripts() -> None:
    top = _dockerfile("full")
    assert "sandbox/entrypoints/lib/jvm_env.sh /opt/ossbot/entrypoints/lib/" in top
    assert re.search(r"sandbox/gradle/ossbot-prefetch\.init\.gradle sandbox/gradle/ossbot-offline\.init\.gradle"
                     r" \\\n\s+/opt/ossbot/gradle/", top)  # fmt: skip
    assert "install -d -o 0 -g 0 -m 0755 /opt/ossbot/gradle" in top
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!sandbox/gradle/" in ignore  # the build context carries them


def test_every_entrypoint_exists_and_matches_the_shared_constant() -> None:
    present = {p.stem for p in (ROOT / "sandbox" / "entrypoints").glob("*.sh")}
    assert present == set(SANDBOX_JOB_ENTRYPOINTS)
