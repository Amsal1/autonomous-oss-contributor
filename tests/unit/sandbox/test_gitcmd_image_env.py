"""Image pins (DESIGN §10.1, R4-N6): the git commands baked into sandbox-full equal ossbot.common.gitcmd,
and every Dockerfile pin equals config/versions.lock.yaml."""

from __future__ import annotations

import base64
import hashlib
import re
import tomllib
from pathlib import Path

import pytest

from ossbot.common import gitcmd
from ossbot.common.schemas import SANDBOX_JOB_ENTRYPOINTS
from ossbot.common.yamlio import safe_load

ROOT = Path(__file__).resolve().parents[3]
IMAGES = ROOT / "sandbox" / "images"
LOCK = safe_load((ROOT / "config" / "versions.lock.yaml").read_bytes())
MISE_TOML = tomllib.loads((IMAGES / "full-base" / "mise.toml").read_text(encoding="utf-8"))
MISE_LOCK = tomllib.loads((IMAGES / "full-base" / "mise.lock").read_text(encoding="utf-8"))


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
    # build-env §6.4: the mise tools' pins live in mise.toml (the ARGs are gone)
    for name in ("TOFU_VERSION", "KIND_VERSION", "GITLEAKS_VERSION"):
        assert f"ARG {name}" not in full
    tools = MISE_TOML["tools"]
    assert tools["opentofu"] == LOCK["tofu"]["version"] and tools["kind"] == LOCK["kind"]["version"]
    assert tools["gitleaks"] == LOCK["gitleaks"]["version"]
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
# layers are appended after it, so no toolchain layer above is rebuilt. Baseline 12856539 plus two changes: the
# poppler-utils pin 24.02.0-1ubuntu9.10 -> 9.11 (2026-10-24; 9.10 left the noble index) and build-env §6.4 (M8,
# 2026-10-24): the mise tools from the committed mise.toml + mise.lock (MISE_LOCKED=1) instead of `mise use
# ... node@lts go@latest`, the TOFU/KIND/GITLEAKS ARGs moved into mise.toml, MISE_USE_VERSIONS_HOST(_TRACK)=false
# (accepted: the base rebuilds once).
BASE_PREFIX_SHA256 = "99e4b1e143bb5aab087a36538568d274833df7ad0f3744ad6688c1d1d4c90309"
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
    for name in ("OPENJDK25_VERSION", "MAVEN_VERSION", "MAVEN_SHA512", "OPENJDK11_VERSION", "OPENJDK8_VERSION",
                 "RUSTUP_VERSION", "RUSTUP_SHA256", "RUST_VERSION", "RUST_MANIFEST_SHA256",
                 "COREPACK_FALLBACK_VERSION", "COREPACK_FALLBACK_SHA512"):  # fmt: skip
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


# ----------------------------------------------------------------- build-env design §6.4 (M8, AC 21, AC 23)
def test_mise_tools_are_exact_versions_equal_to_the_lock() -> None:
    tools = MISE_TOML["tools"]
    pinned = {k: v for k, v in LOCK["mise_tools"].items() if k not in ("lockfile", "source", "verified_at")}
    assert tools == pinned and len(tools) == 12
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in tools.values())
    assert LOCK["mise_tools"]["lockfile"] == "sandbox/images/full-base/mise.lock"
    assert LOCK["mise_tools"]["verified_at"] and LOCK["mise_tools"]["source"]
    raw = (IMAGES / "full-base" / "mise.toml").read_text(encoding="utf-8")
    assert not re.search(r"\b(lts|latest)\b", raw)  # the Dockerfile asserts the same at build


def test_mise_lock_has_a_url_and_checksum_for_each_tool() -> None:
    locked = MISE_LOCK["tools"]
    assert set(locked) == set(MISE_TOML["tools"])
    for tool, version in MISE_TOML["tools"].items():
        (entry,) = locked[tool]
        assert entry["version"] == version, tool
        platform = entry["platforms.linux-x64"]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", platform["checksum"]), tool
        assert platform["url"].startswith("https://"), tool
    gitleaks = locked["gitleaks"][0]["platforms.linux-x64"]["checksum"]
    assert gitleaks == f"sha256:{LOCK['gitleaks']['sha256']}"  # the lockfile agrees with the earlier pin


def test_the_base_installs_mise_tools_locked_and_never_asks_the_versions_host() -> None:
    base = _dockerfile("full-base")
    assert "COPY --chown=0:0 --chmod=0644 sandbox/images/full-base/mise.toml /opt/mise/config/config.toml" in base
    assert "COPY --chown=0:0 --chmod=0644 sandbox/images/full-base/mise.lock /opt/mise/config/mise.lock" in base
    assert " && MISE_LOCKED=1 mise install --yes \\\n" in base
    assert "! grep -Eqw 'lts|latest' /opt/mise/config/config.toml" in base
    assert "mise ls --global --json" in base and "mise use" not in base
    assert re.search(r"^    MISE_USE_VERSIONS_HOST=false \\$", base, re.MULTILINE)  # BE-4.8, the build too
    assert re.search(r"^    MISE_USE_VERSIONS_HOST_TRACK=false \\$", base, re.MULTILINE)
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!sandbox/images/full-base/mise.toml" in ignore and "!sandbox/images/full-base/mise.lock" in ignore
    images = (ROOT / "scripts" / "lib" / "images.sh").read_text(encoding="utf-8")
    base_build = images.split("_build sandbox-full-base", 1)[1].split("base=$OSSBOT_IMAGE_TAG", 1)[0]
    assert "sandbox/images/full-base/mise.toml" in base_build and "sandbox/images/full-base/mise.lock" in base_build
    assert "TOFU_VERSION" not in images and "lock_get tofu" not in images


def test_jdk_8_11_rust_and_corepack_pins_equal_lock() -> None:
    base = _dockerfile("full-base")
    assert _arg(base, "OPENJDK11_VERSION") == LOCK["openjdk_11"]["apt_version"]
    assert _arg(base, "OPENJDK8_VERSION") == LOCK["openjdk_8"]["apt_version"]
    assert _arg(base, "RUSTUP_VERSION") == LOCK["rustup"]["version"]
    assert _arg(base, "RUSTUP_SHA256") == LOCK["rustup"]["sha256"]
    assert LOCK["rustup"]["url"].endswith(f"/{LOCK['rustup']['version']}/x86_64-unknown-linux-gnu/rustup-init")
    assert _arg(base, "RUST_VERSION") == LOCK["rust_default"]["version"]
    assert _arg(base, "RUST_MANIFEST_SHA256") == LOCK["rust_default"]["manifest_sha256"]
    assert _arg(base, "COREPACK_FALLBACK_VERSION") == LOCK["corepack_fallback"]["version"]
    assert _arg(base, "COREPACK_FALLBACK_SHA512") == LOCK["corepack_fallback"]["sha512"]
    integrity = LOCK["corepack_fallback"]["integrity"].removeprefix("sha512-")
    assert base64.b64decode(integrity).hex() == LOCK["corepack_fallback"]["sha512"]  # the npm integrity, as hex
    for key in ("openjdk_11", "openjdk_8", "rustup", "rust_default", "corepack_fallback", "mise_tools",
                "gradle_java_matrix", "fixtures_build_env"):  # fmt: skip
        assert LOCK[key]["source"] and LOCK[key]["verified_at"] == "2026-10-24", key


def test_rust_manifest_is_checked_before_rustup_init_in_the_same_run() -> None:
    base = _dockerfile("full-base")
    step = base.split("RUN curl -fsSL -o /tmp/rustup-init", 1)[1].split("\nRUN ", 1)[0]
    assert 'echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum -c -' in step
    check = 'echo "${RUST_MANIFEST_SHA256}  /tmp/channel-rust-${RUST_VERSION}.toml" | sha256sum -c -'
    assert check in step and step.index(check) < step.index("/tmp/rustup-init -y")
    assert '--default-toolchain "${RUST_VERSION}" -c rustfmt,clippy' in step
    assert "RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo" in step
    corepack = base.split("RUN mkdir -p /opt/ossbot/corepack", 1)[1].split("\nRUN ", 1)[0]
    assert (
        'echo "${COREPACK_FALLBACK_SHA512}  /opt/ossbot/corepack/corepack-${COREPACK_FALLBACK_VERSION}.tgz"' in corepack
    )
    assert "| sha512sum -c -" in corepack


def test_jdk_8_and_11_keep_21_the_default() -> None:
    base = _dockerfile("full-base")
    step = base.split('"openjdk-11-jdk-headless=${OPENJDK11_VERSION}"', 1)[1].split("\n# Rust", 1)[0]
    assert '"openjdk-8-jdk-headless=${OPENJDK8_VERSION}"' in step and "update-alternatives --set" in step
    assert "grep -q 'version \"21\\.'" in step and "/usr/lib/jvm/java-8-openjdk-amd64/bin/javac" in step


def test_thin_top_bakes_the_resolver_and_tool_env() -> None:
    top = _dockerfile("full")
    assert "src/ossbot/sandbox_tools/toolchains.py /opt/ossbot/bin/ossbot-toolchains" in top
    assert "sandbox/entrypoints/lib/tool_env.sh /opt/ossbot/entrypoints/lib/" in top
    images = (ROOT / "scripts" / "lib" / "images.sh").read_text(encoding="utf-8")
    full_build = images.split("_build sandbox-full sandbox/images/full", 1)[1].split("_build sandbox-llm", 1)[0]
    assert "src/ossbot/sandbox_tools/toolchains.py" in full_build
    assert "toolchains" not in _dockerfile("llm")  # never in the key-holding image


def test_every_entrypoint_exists_and_matches_the_shared_constant() -> None:
    present = {p.stem for p in (ROOT / "sandbox" / "entrypoints").glob("*.sh")}
    assert present == set(SANDBOX_JOB_ENTRYPOINTS)
