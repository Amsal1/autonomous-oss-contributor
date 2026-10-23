"""Doctor's JVM toolchains line (sandbox-network Part 1, R1.5; DESIGN §10.1): JDK 17, 21 (the default
java) and 25 and the pinned Maven in the pinned sandbox-full image."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from ossbot.ctl import doctor

REF = "ossbot/sandbox-full@sha256:" + "a" * 64
GOOD = ('openjdk version "21.0.12" 2026-07-21\njava-1.21.0-openjdk-amd64\njava-17-openjdk-amd64\n'
        "java-21-openjdk-amd64\njava-25-openjdk-amd64\nApache Maven 3.9.16 (abc)\n")  # fmt: skip


class Pins:
    def __init__(self, ref: str | None) -> None:
        self._ref = ref

    def ref(self, name: str) -> str | None:
        return self._ref if name == "sandbox-full" else None


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"stdout": GOOD, "rc": 0, "ref": REF, "calls": []}
    monkeypatch.setattr(doctor, "load_image_pins", lambda path: Pins(state["ref"]))
    monkeypatch.setattr(doctor, "_lock_scalar", lambda section, key: "3.9.16" if (section, key) == (
        "maven", "version") else None)  # fmt: skip

    def run(argv: list[str], timeout: float = 20.0, user: str | None = None) -> subprocess.CompletedProcess[str]:
        state["calls"].append((argv, timeout))
        return subprocess.CompletedProcess(argv, state["rc"], state["stdout"], "")

    monkeypatch.setattr(doctor, "_run", run)
    return state


def test_pass_with_default_21_three_jdks_and_the_pinned_maven(rig: dict[str, Any]) -> None:
    res = doctor.check_sandbox_toolchains(quick=False)
    assert res.status == doctor.PASS and "JDK 17/21/25" in res.detail and "Apache Maven 3.9.16" in res.detail
    ((argv, timeout),) = rig["calls"]
    assert argv[:7] == [doctor.DOCKER, "run", "--rm", "--network", "none", "--read-only", "--tmpfs"]
    assert REF in argv and timeout == 60


def test_fail_when_the_default_java_is_25(rig: dict[str, Any]) -> None:
    rig["stdout"] = GOOD.replace('version "21.0.12"', 'version "25.0.4"')
    res = doctor.check_sandbox_toolchains(quick=False)
    assert res.status == doctor.FAIL and len(res.detail) <= 200


@pytest.mark.parametrize("missing", ["java-17-openjdk-amd64\n", "java-25-openjdk-amd64\n", "Apache Maven 3.9.16"])
def test_fail_when_a_jdk_or_maven_is_missing(rig: dict[str, Any], missing: str) -> None:
    rig["stdout"] = GOOD.replace(missing, "")
    assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL


def test_fail_on_another_maven_version_or_a_failed_run(rig: dict[str, Any]) -> None:
    rig["stdout"] = GOOD.replace("3.9.16", "3.8.7")
    assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL
    rig["stdout"], rig["rc"] = GOOD, 125
    assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL


def test_warn_when_the_image_is_not_pinned(rig: dict[str, Any]) -> None:
    rig["ref"] = None
    res = doctor.check_sandbox_toolchains(quick=False)
    assert res.status == doctor.WARN and "not pinned" in res.detail and rig["calls"] == []


def test_quick_mode_runs_no_container(rig: dict[str, Any]) -> None:
    res = doctor.check_sandbox_toolchains(quick=True)
    assert res.status == doctor.PASS and res.detail == "pinned (JDK 17/21/25, Maven 3.9.16; verified at build)"
    assert rig["calls"] == []
