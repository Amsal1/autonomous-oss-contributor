"""Prompt texts of build-env design §8.3 (BE-5.3): the coder installs what it needs when its executor has the
web, ``needs_network`` is for offline runs only, the per-repo toolchains are preset (no ``JAVA_HOME`` prefix
rule), the planner no longer says validation runs offline, and the runner's network line says the same."""

from __future__ import annotations

from pathlib import Path

import pytest

from ossbot.runner.stages import implement

ROOT = Path(__file__).resolve().parents[3]
CODER = (ROOT / "prompts" / "coder.md").read_text(encoding="utf-8")
PLANNER = (ROOT / "prompts" / "planner.md").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit


def test_coder_prompt_has_the_declared_toolchains_line() -> None:
    assert (
        "The repo's declared toolchains (JDK, Node and its package manager, Python, Go, Rust, Terraform, Ruby) are"
        " installed and `JAVA_HOME`, Gradle and Maven point at them; the image also has JDK 8, 11, 17, 21 and 25"
        " and Maven 3.9."
    ) in CODER


def test_coder_prompt_installs_what_it_needs_with_the_web() -> None:
    assert (
        "Your commands run in a separate container without credentials, with `pipefail` set. When"
        " `echo $OSSBOT_EXEC_NETWORK` prints `web`, it has the public web: install what the build needs"
        " (dependencies, `mise install <tool>@<version>`, wrapper downloads, `rustup toolchain install`) and"
        " continue; don't use `needs_network`. Otherwise it is offline: builds run from the caches fetch prepared,"
        " and `needs_network` is for a NEW dependency or toolchain your change adds (a manifest, lockfile or"
        " toolchain file)."
    ) in CODER
    assert "put one line per gap in `sandbox_gaps`" in CODER  # the gaps bullet stays, for what still fails
    assert "still can't run after trying" in CODER


@pytest.mark.parametrize("prompt", [CODER, PLANNER], ids=["coder", "planner"])
def test_no_java_home_prefix_rule(prompt: str) -> None:
    assert "JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64" not in prompt
    assert "prefix the command with" not in prompt
    assert "/usr/lib/jvm/java-{17,21,25}-openjdk-amd64" not in prompt


def test_coder_prompt_drops_the_offline_by_default_text() -> None:
    assert "By default it has no network" not in CODER
    assert "Gradle runs offline by itself" not in CODER


def test_planner_prompt_says_validation_has_the_web_in_the_web_modes() -> None:
    assert "with the repo's declared toolchains preset" in PLANNER
    assert "In egress modes sandbox_open and sandbox_full it has the web" in PLANNER
    assert "run offline from prefetched caches" not in PLANNER  # the old "validation commands run offline"
    assert "Gradle runs offline from the fetched cache" not in PLANNER


def test_network_line_says_install_and_continue() -> None:
    assert implement.NETWORK_LINE == (
        "This run should have network (check `echo $OSSBOT_EXEC_NETWORK`): the public web through a proxy, allowed"
        " by the owner. Install what the build needs and continue; report in `sandbox_gaps` only what still fails"
        " after trying."
    )
    assert "If a download fails" not in implement.NETWORK_LINE
