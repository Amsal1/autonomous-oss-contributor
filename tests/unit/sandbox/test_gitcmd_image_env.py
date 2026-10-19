"""Image pins (DESIGN §10.1, R4-N6): the git commands baked into sandbox-full equal ossbot.common.gitcmd,
and every Dockerfile pin equals config/versions.lock.yaml."""

from __future__ import annotations

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
    full = _dockerfile("full")
    assert _env(full, "OSSBOT_FORMAT_PATCH") == gitcmd.FORMAT_PATCH
    assert _env(full, "OSSBOT_NUMSTAT") == gitcmd.NUMSTAT


@pytest.mark.parametrize("image", ["full", "llm", "proxy"])
def test_base_image_pinned_by_lock_digest(image: str) -> None:
    froms = re.findall(r"^FROM (\S+)", _dockerfile(image), re.MULTILINE)
    assert froms == [f"ubuntu:24.04@{LOCK['base_images']['ubuntu_24_04']}"]


def test_tool_pins_equal_lock() -> None:
    full, llm, proxy = _dockerfile("full"), _dockerfile("llm"), _dockerfile("proxy")
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
    for entry in ("fetch", "exec", "validate", "package", "unbundle", "snapshot", "probe"):
        assert f"sandbox/entrypoints/{entry}.sh" in full, entry
    assert "src/ossbot/sandbox_tools/exec_mcp.py /opt/ossbot/bin/ossbot-exec-mcp" in llm
    for entry in ("kiro", "list_models", "probe"):
        assert f"sandbox/entrypoints/{entry}.sh" in llm, entry
    assert "execd.py" not in llm and "build-essential" not in llm


def test_every_entrypoint_exists_and_matches_the_shared_constant() -> None:
    present = {p.stem for p in (ROOT / "sandbox" / "entrypoints").glob("*.sh")}
    assert present == set(SANDBOX_JOB_ENTRYPOINTS)
