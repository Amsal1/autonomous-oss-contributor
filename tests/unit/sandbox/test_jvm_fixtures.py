"""The JVM sandbox fixtures (tests/fixtures/sandbox/; run by tests/sandbox/test_jvm_offline.py in the
close-out) are pinned: the committed Gradle wrapper jar and the wrapper's distribution checksum equal
config/versions.lock.yaml gradle_fixture (verified against services.gradle.org, 2026-10-23)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ossbot.common.yamlio import safe_load

ROOT = Path(__file__).resolve().parents[3]
LOCK = safe_load((ROOT / "config" / "versions.lock.yaml").read_bytes())
GRADLE = ROOT / "tests" / "fixtures" / "sandbox" / "gradle-multi"
MAVEN = ROOT / "tests" / "fixtures" / "sandbox" / "maven-multi"


def test_gradle_wrapper_jar_and_distribution_are_pinned() -> None:
    pin = LOCK["gradle_fixture"]
    jar = (GRADLE / "gradle" / "wrapper" / "gradle-wrapper.jar").read_bytes()
    assert hashlib.sha256(jar).hexdigest() == pin["wrapper_jar_sha256"]
    props = (GRADLE / "gradle" / "wrapper" / "gradle-wrapper.properties").read_text(encoding="utf-8")
    assert f"distributionSha256Sum={pin['distribution_sha256']}" in props.splitlines()
    url = f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{pin['version']}-bin.zip"
    assert url in props.splitlines()
    assert (GRADLE / "gradlew").read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_gradle_fixture_shape() -> None:
    assert "include 'lib', 'app'" in (GRADLE / "settings.gradle").read_text(encoding="utf-8")
    assert "JavaLanguageVersion.of(17)" in (GRADLE / "lib" / "build.gradle").read_text(encoding="utf-8")
    app = (GRADLE / "app" / "build.gradle").read_text(encoding="utf-8")
    assert "JavaLanguageVersion.of(25)" in app and "implementation project(':lib')" in app
    assert "hasProperty('ossbotProbeExtra')" in app and "useJUnitPlatform()" in app


def test_maven_fixture_shape() -> None:
    parent = (MAVEN / "pom.xml").read_text(encoding="utf-8")
    assert re.findall(r"<module>(\w+)</module>", parent) == ["core", "app", "tc17", "jdk25"]
    assert "<jdk>[25,)</jdk>" in parent  # jdk25 joins the reactor only on JDK 25
    assert "<maven.compiler.release>25</maven.compiler.release>" in (MAVEN / "jdk25" / "pom.xml").read_text(
        encoding="utf-8"
    )
    tc17 = (MAVEN / "tc17" / "pom.xml").read_text(encoding="utf-8")
    assert "maven-toolchains-plugin" in tc17 and "<version>17</version>" in tc17
    assert "junit-jupiter" in (MAVEN / "app" / "pom.xml").read_text(encoding="utf-8")
