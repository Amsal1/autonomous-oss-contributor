"""Doctor's toolchains line (sandbox-network Part 1, R1.5; DESIGN §10.1; build-env design §6.4, N5): JDK 8, 11,
17, 21 (the default java) and 25, the pinned Maven, rustup and rustc, the netshim and resolver binaries and no
lts/latest in the mise config of the pinned sandbox-full image."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from ossbot.ctl import doctor

REF = "ossbot/sandbox-full@sha256:" + "a" * 64
GOOD = ('openjdk version "21.0.12" 2026-07-21\njava-1.21.0-openjdk-amd64\njava-11-openjdk-amd64\n'
        "java-17-openjdk-amd64\njava-21-openjdk-amd64\njava-25-openjdk-amd64\njava-8-openjdk-amd64\n"
        "Apache Maven 3.9.16 (abc)\nrustup 1.29.2 (2a1b3c4d 2026-10-01)\nrustc 1.99.1 (abcdef012 2026-10-16)\n"
        "netshim=ok\nresolver=ok\nmise=pinned\n")  # fmt: skip
LOCK = {("maven", "version"): "3.9.16", ("rustup", "version"): "1.29.2", ("rust_default", "version"): "1.99.1"}


class Pins:
    def __init__(self, ref: str | None) -> None:
        self._ref = ref

    def ref(self, name: str) -> str | None:
        return self._ref if name == "sandbox-full" else None


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"stdout": GOOD, "rc": 0, "ref": REF, "calls": []}
    monkeypatch.setattr(doctor, "load_image_pins", lambda path: Pins(state["ref"]))
    monkeypatch.setattr(doctor, "_lock_scalar", lambda section, key: LOCK.get((section, key)))

    def run(argv: list[str], timeout: float = 20.0, user: str | None = None) -> subprocess.CompletedProcess[str]:
        state["calls"].append((argv, timeout))
        return subprocess.CompletedProcess(argv, state["rc"], state["stdout"], "")

    monkeypatch.setattr(doctor, "_run", run)
    return state


def test_pass_with_every_fallback_toolchain(rig: dict[str, Any]) -> None:
    res = doctor.check_sandbox_toolchains(quick=False)
    assert res.status == doctor.PASS, res.detail
    assert "JDK 8/11/17/21/25" in res.detail and "Apache Maven 3.9.16" in res.detail
    assert "rustup 1.29.2, rustc 1.99.1" in res.detail and "mise tools pinned" in res.detail
    ((argv, timeout),) = rig["calls"]
    assert argv[:7] == [doctor.DOCKER, "run", "--rm", "--network", "none", "--read-only", "--tmpfs"]
    assert REF in argv and timeout == 60 and res.name == "toolchains (sandbox-full)"


def test_fail_when_the_default_java_is_25(rig: dict[str, Any]) -> None:
    rig["stdout"] = GOOD.replace('version "21.0.12"', 'version "25.0.4"')
    res = doctor.check_sandbox_toolchains(quick=False)
    assert res.status == doctor.FAIL and len(res.detail) <= 200


@pytest.mark.parametrize("missing", ["java-8-openjdk-amd64\n", "java-11-openjdk-amd64\n", "java-17-openjdk-amd64\n",
                                     "java-25-openjdk-amd64\n", "Apache Maven 3.9.16", "rustup 1.29.2",
                                     "rustc 1.99.1", "netshim=ok", "resolver=ok"])  # fmt: skip
def test_fail_when_a_toolchain_is_missing(rig: dict[str, Any], missing: str) -> None:
    rig["stdout"] = GOOD.replace(missing, "")
    assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL


def test_fail_on_a_floating_mise_config(rig: dict[str, Any]) -> None:
    rig["stdout"] = GOOD.replace("mise=pinned", "mise=floating")
    assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL


def test_fail_on_another_version_or_a_failed_run(rig: dict[str, Any]) -> None:
    for old, new in (("3.9.16", "3.8.7"), ("rustc 1.99.1", "rustc 1.98.0"), ("rustup 1.29.2", "rustup 1.28.2")):
        rig["stdout"] = GOOD.replace(old, new)
        assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL, old
    rig["stdout"], rig["rc"] = GOOD, 125
    assert doctor.check_sandbox_toolchains(quick=False).status == doctor.FAIL


def test_warn_when_the_image_is_not_pinned(rig: dict[str, Any]) -> None:
    rig["ref"] = None
    res = doctor.check_sandbox_toolchains(quick=False)
    assert res.status == doctor.WARN and "not pinned" in res.detail and rig["calls"] == []


def test_quick_mode_runs_no_container(rig: dict[str, Any]) -> None:
    res = doctor.check_sandbox_toolchains(quick=True)
    assert res.status == doctor.PASS
    assert res.detail == "pinned (JDK 8/11/17/21/25, Maven 3.9.16, Rust 1.99.1; verified at build)"
    assert rig["calls"] == []
