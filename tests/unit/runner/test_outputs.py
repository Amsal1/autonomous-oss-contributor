"""The coder's report and words (sandbox-network Part 1, R1.5, R1.6, R1.8; DESIGN §9.8): ``sandbox_gaps``
for checks the sandbox couldn't run, ``needs_network`` only for a NEW dependency, pipefail, the JDK homes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ossbot.runner import outputs
from ossbot.runner.outputs import SCHEMA_HINTS, ImplementReport

ROOT = Path(__file__).resolve().parents[3]
CODER = (ROOT / "prompts" / "coder.md").read_text(encoding="utf-8")
PLANNER = (ROOT / "prompts" / "planner.md").read_text(encoding="utf-8")


def test_sandbox_gaps_bounds() -> None:
    ok = ImplementReport.model_validate({"sandbox_gaps": ["g" * 500] * 10})
    assert len(ok.sandbox_gaps) == 10
    assert ImplementReport().sandbox_gaps == []
    for bad in ({"sandbox_gaps": ["g"] * 11}, {"sandbox_gaps": ["g" * 501]}, {"sandbox_gap": ["x"]}):
        with pytest.raises(ValidationError):
            ImplementReport.model_validate(bad)


def test_needs_network_keeps_its_charset() -> None:
    assert outputs.NEEDS_NETWORK_PATTERN == r"^[A-Za-z0-9@/._:=<>~^ -]{1,200}$"
    ImplementReport.model_validate({"needs_network": ["com.google.guava:guava:33.3.1-jre", "left-pad@1.3.0"]})
    with pytest.raises(ValidationError):
        ImplementReport.model_validate({"needs_network": ["JDK 25 (toolchain); the gradle wrapper"]})


def test_coder_hint_names_sandbox_gaps_and_the_needs_network_charset() -> None:
    hint = SCHEMA_HINTS["coder"]
    assert '"sandbox_gaps": ["<= 10, one line each"]' in hint
    assert "only NEW dependencies your change adds" in hint and "A-Za-z0-9@/._:=<>~^ space -" in hint
    assert '"commands_run"' in hint and '"needs_research"' in hint


def test_coder_prompt_states_the_new_dependency_rule_gaps_and_pipefail() -> None:
    assert "`needs_network` is only for a NEW dependency that your change adds" in CODER
    assert "letters, digits and `@/._:=<>~^ -` only" in CODER
    assert "put one line per gap in `sandbox_gaps`" in CODER and "don't list it in `needs_network`" in CODER
    assert "with `pipefail` set" in CODER
    assert '"sandbox_gaps": []' in CODER  # the schema block
    assert "If you need a dependency, list it in `needs_network`" not in CODER  # the old, too broad rule


@pytest.mark.parametrize("prompt", [CODER, PLANNER], ids=["coder", "planner"])
def test_prompts_name_the_jdk_homes_and_the_java25_prefix(prompt: str) -> None:
    assert "/usr/lib/jvm/java-{17,21,25}-openjdk-amd64" in prompt
    assert "prefix the command with `JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64`" in prompt
    assert re.search(r"Maven 3\.9", prompt)


def test_planner_prompt_states_pipefail_and_jvm_offline() -> None:
    assert "Validation runs with `pipefail`" in PLANNER
    assert "Gradle runs offline from the fetched cache" in PLANNER
    assert "Maven may download its test providers through the registry proxy" in PLANNER
