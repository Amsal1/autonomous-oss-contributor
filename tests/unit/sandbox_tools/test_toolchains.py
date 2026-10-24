"""The per-repo toolchain resolver (build-env design §6.1, §6.2, §6.3, D6, AC 11, C-N4): one fixture per
declaration file of BE-4.1 and per precedence rule (tests/fixtures/toolchains/<case>/), the Gradle run matrix,
Maven release selection, Go's minimum semantics, the install budget, malformed and oversize input, the index
resolution (trimmed live indexes of 2026-10-24 under tests/fixtures/toolchains/indexes/) and the validated env
file. The installers are faked: each call is recorded and creates what the real installer would."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from ossbot.sandbox_tools import execd
from ossbot.sandbox_tools import toolchains as tc

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "toolchains"
INDEXES = {"node": "node-index.json", "go": "go-dl.json", "java": "mise-java-ga-linux-x86_64.json",
           "terraform": "terraform-index.json"}  # fmt: skip
BINARIES = {"java": "bin/java", "node": "bin/node", "go": "bin/go", "ruby": "bin/ruby", "gradle": "bin/gradle",
            "terraform": "terraform", "opentofu": "tofu"}  # fmt: skip
SYSTEM_PYTHON = (3, 12, 3)


class Fake:
    """The installers: records (argv, env, cwd, timeout) and creates their results under the task cache."""

    def __init__(self, cache: Path, *, fail: tuple[str, ...] = (), node_corepack: bool = True,
                 sleep: float = 0.0) -> None:  # fmt: skip
        self.cache = cache
        self.calls: list[tuple[list[str], dict[str, str], str, int]] = []
        self.fail = fail
        self.node_corepack = node_corepack
        self.sleep = sleep

    def argvs(self) -> list[list[str]]:
        return [c[0] for c in self.calls]

    def __call__(self, argv: list[str], env: dict[str, str], cwd: str, timeout: int) -> tuple[int, str]:
        self.calls.append((argv, env, cwd, timeout))
        if self.sleep:
            time.sleep(self.sleep)
        if any(f in " ".join(argv) for f in self.fail):
            return 1, "simulated installer failure"
        c = self.cache
        if argv[:2] == [tc.MISE, "install"] and len(argv) == 3:
            tool, version = argv[2].split("@", 1)
            name = version if "." in version.split("-")[-1] else f"{version}.9.9"  # a major: mise resolves it
            home = c / "mise" / "installs" / tool / name
            binary = home / BINARIES[tool]
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n")
            if tool == "node" and self.node_corepack:
                (home / "bin" / "corepack").write_text("#!/bin/sh\n")
        elif argv[:3] == [tc.UV, "python", "install"]:
            (c / "uv-python" / "cpython-3.11.14-linux-x86_64-gnu").mkdir(parents=True, exist_ok=True)
        elif argv[:3] == [tc.RUSTUP, "toolchain", "link"]:
            (c / "rustup" / "toolchains" / argv[3]).mkdir(parents=True, exist_ok=True)
        elif argv[:2] == [tc.RUSTUP, "default"]:
            (c / "rustup" / "settings.toml").write_text(f'default_toolchain = "{argv[2]}"\n')
        elif argv[:3] == [tc.RUSTUP, "toolchain", "install"]:
            rustc = c / "rustup" / "toolchains" / f"{argv[3]}-x86_64-unknown-linux-gnu" / "bin" / "rustc"
            rustc.parent.mkdir(parents=True, exist_ok=True)
            rustc.write_text("")
        elif argv[0].endswith("/npm") and tc.COREPACK_FALLBACK in argv:
            (c / "corepack-bin" / "bin").mkdir(parents=True, exist_ok=True)
            (c / "corepack-bin" / "bin" / "corepack").write_text("")
        elif argv[0].endswith("corepack") and argv[1:] == ["install"]:
            (c / "corepack" / "v1" / "pnpm" / "12.15.0").mkdir(parents=True, exist_ok=True)
        elif argv[0].endswith("corepack") and argv[1] == "enable":
            (Path(argv[3]) / argv[4]).parent.mkdir(parents=True, exist_ok=True)
            (Path(argv[3]) / argv[4]).write_text("")
        return 0, "ok"


def index_fetch(url: str) -> bytes | None:
    name = next(k for k, v in tc.INDEXES.items() if v == url)
    return (FIXTURES / "indexes" / INDEXES[name]).read_bytes()


class Run:
    def __init__(self, tmp: Path, case: str | None, **kw: Any) -> None:
        self.work = tmp / "work"
        self.repo = self.work / "repo"
        if case is not None:
            shutil.copytree(FIXTURES / case, self.repo)
        else:
            self.repo.mkdir(parents=True)
        self.cache = self.work / "cache"
        self.state = self.work / ".home" / ".ossbot"
        self.fake = kw.pop("fake", None) or Fake(self.cache)
        self.kw = kw

    def go(self, mode: str = "install", *, fetch: Any = index_fetch, budget: int = 600,
           system_python: tuple[int, ...] | None = SYSTEM_PYTHON) -> Run:  # fmt: skip
        ctx = tc.Ctx(repo=self.repo, state=self.state, cache=self.cache, work=str(self.work), mode=mode, budget=budget,
                     run=self.fake, fetch=fetch, system_python=system_python, **self.kw)  # fmt: skip
        self.report = tc.run(ctx).to_json()
        self.env = tc.parse_env((self.state / "toolchain.env").read_text(), str(self.work))
        self.raw_env = (self.state / "toolchain.env").read_text()
        self.mise = (self.state / "mise.toml").read_text()
        assert json.loads((self.state / "toolchains.json").read_text()) == self.report
        assert self.report.get("error") is None, self.report
        return self

    def entries(self, tool: str) -> list[dict[str, Any]]:
        return [e for e in self.report["tools"] if e["tool"] == tool]

    def entry(self, tool: str) -> dict[str, Any]:
        found = self.entries(tool)
        assert len(found) == 1, (tool, self.report["tools"])
        return found[0]

    def mise_path(self, tool: str, name: str) -> str:
        return str(self.cache / "mise" / "installs" / tool / name)

    def installs(self) -> list[str]:
        return [a[2] for a in self.fake.argvs() if a[:2] == [tc.MISE, "install"] and len(a) == 3]


def jdk(n: int) -> str:
    return f"/usr/lib/jvm/java-{n}-openjdk-amd64"


# ---------------------------------------------------------------------------------- one per declaration file
def test_mise_toml_java_vendor_major_and_node_major_resolve_to_exact_versions(tmp_path: Path) -> None:
    r = Run(tmp_path, "mise-toml").go()
    assert r.installs() == ["java@temurin-22.0.2+9", "node@22.23.4"]  # exact versions only (M3)
    java = r.entry("java")
    assert java["chosen"] == "temurin-22.0.2+9" and java["resolved_by"] == "index" and java["source"] == "mise.toml"
    assert r.env["JAVA_HOME"] == r.mise_path("java", "temurin-22.0.2+9")
    assert r.env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(21)  # no wrapper: unknown Gradle -> 21
    assert r.mise_path("node", "22.23.4") + "/bin" in r.env["OSSBOT_PATH_PREPEND"].split(":")
    assert 'node = "22.23.4"' in r.mise


def test_dot_mise_toml_go_exact(tmp_path: Path) -> None:
    r = Run(tmp_path, "dot-mise-toml").go()
    assert r.installs() == ["go@1.26.10"]
    assert r.env["OSSBOT_PATH_PREPEND"] == r.mise_path("go", "1.26.10") + "/bin"
    assert r.env["GOTOOLCHAIN"] == "local"


def test_tool_versions_exact_java_and_node_major(tmp_path: Path) -> None:
    r = Run(tmp_path, "tool-versions").go()
    assert r.installs() == ["java@temurin-21.0.5+11", "node@20.20.2"]
    assert r.entry("java")["resolved_by"] == "declared" and r.entry("node")["resolved_by"] == "index"


def test_java_version_uses_the_image_jdk(tmp_path: Path) -> None:
    r = Run(tmp_path, "java-version").go()
    assert r.env["JAVA_HOME"] == jdk(17) and r.fake.calls == []
    assert r.entry("java")["action"] == "image" and r.entry("maven")["chosen"] == "/opt/maven 3.9.16"


def test_java_version_with_a_vendor_installs_that_vendor(tmp_path: Path) -> None:
    r = Run(tmp_path, "java-version-vendor").go()
    (install,) = r.installs()
    assert install.startswith("java@corretto-21.") and r.env["JAVA_HOME"].startswith(
        r.mise_path("java", "corretto-21.")
    )


def test_sdkmanrc_java_suffix_maps_to_the_vendor(tmp_path: Path) -> None:
    r = Run(tmp_path, "sdkmanrc").go()
    (install,) = r.installs()
    assert install.startswith("java@temurin-21.0.") and r.entry("java")["source"] == ".sdkmanrc"
    assert r.entry("maven")["declared"] == "3.9.6"


def test_cn4_sdkmanrc_gradle_without_a_wrapper_installs_gradle(tmp_path: Path) -> None:
    r = Run(tmp_path, "sdkmanrc-gradle").go()
    assert r.installs() == ["gradle@8.5"]
    assert r.mise_path("gradle", "8.5") + "/bin" in r.env["OSSBOT_PATH_PREPEND"].split(":")
    assert next(e for e in r.entries("gradle") if e.get("source") == ".sdkmanrc")["action"] == "install"


def test_cn4_sdkmanrc_gradle_with_a_wrapper_is_only_reported(tmp_path: Path) -> None:
    r = Run(tmp_path, "sdkmanrc-gradle-with-wrapper").go()
    assert r.installs() == []
    (entry,) = [e for e in r.entries("gradle") if e.get("source") == ".sdkmanrc"]
    assert entry["action"] == "report" and "gradle-wrapper.properties" in entry["hint"]


def test_gradle_daemon_jvm_properties_pick_the_daemon_jvm(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-daemon-jvm").go()
    assert r.installs() == ["java@temurin-22.0.2+9"]
    assert r.env["OSSBOT_GRADLE_JAVA_HOME"] == r.mise_path("java", "temurin-22.0.2+9")
    assert r.env["OSSBOT_JDKS"] == r.mise_path("java", "temurin-22.0.2+9")


def test_gradle_toolchain_jdk_is_installed_and_listed(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-toolchain").go()
    assert r.installs() == ["java@temurin-22.0.2+9"]
    assert r.env["OSSBOT_JDKS"] == r.mise_path("java", "temurin-22.0.2+9")
    assert r.env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(25)  # Gradle 9.8.2 runs on 25: the highest image JDK
    assert r.env["JAVA_HOME"] == jdk(21)


def test_kotlin_jvm_toolchain_is_seen(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-jvm-toolchain").go()
    assert r.installs() == ["java@temurin-23.0.2+7"]


def test_version_catalog_toolchain_key(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-libs-toml").go()
    assert r.installs() == [] and [e["chosen"] for e in r.entries("java-toolchain")] == ["21"]


def test_gradle_source_compatibility_without_a_toolchain_joins_the_compile_set(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-source-compat").go()
    assert [e["chosen"] for e in r.entries("java-toolchain")] == ["17"]
    assert r.env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(21)  # Gradle 8.5 runs on <= 21


def test_gradle_source_compatibility_is_ignored_next_to_a_toolchain(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-source-compat-with-toolchain").go()
    assert [e["chosen"] for e in r.entries("java-toolchain")] == ["21"]  # not 8


def test_gradle_property_toolchain_is_not_seen_and_left_to_gradle(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-property-toolchain").go()
    assert r.installs() == []
    (entry,) = r.entries("java-toolchain")
    assert entry["declared"] == "computed" and "auto-download" in entry["hint"]


def test_old_gradle_wrapper_gets_a_jvm_it_can_run_on(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-old-wrapper").go()
    assert r.env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(17)  # Gradle 7.6 runs on <= 19; .java-version 21 can't
    assert r.env["JAVA_HOME"] == jdk(17)  # the wrapper's client JVM too


def test_maven_release_25_is_the_floci_case(tmp_path: Path) -> None:
    r = Run(tmp_path, "maven-release").go()
    assert r.env["JAVA_HOME"] == jdk(25) and r.fake.calls == []
    assert r.entry("maven")["chosen"] == "mvnw" and "pom.xml maven.compiler.release" in r.entry("java")["source"]


def test_maven_compiler_plugin_release_property_is_resolved(tmp_path: Path) -> None:
    assert Run(tmp_path, "maven-compiler-plugin").go().env["JAVA_HOME"] == jdk(17)


def test_maven_source_target_1_8(tmp_path: Path) -> None:
    assert Run(tmp_path, "maven-source-target").go().env["JAVA_HOME"] == jdk(8)


def test_maven_need_is_the_max_over_the_modules(tmp_path: Path) -> None:
    r = Run(tmp_path, "maven-module-release").go()
    assert r.env["JAVA_HOME"] == jdk(21)  # root ${jdk} = 11, core 21


def test_maven_toolchains_plugin_jdk_is_installed(tmp_path: Path) -> None:
    r = Run(tmp_path, "maven-toolchains-plugin").go()
    assert r.installs() == ["java@temurin-23.0.2+7"]  # once, for JAVA_HOME and the toolchain entry
    assert r.env["JAVA_HOME"] == r.env["OSSBOT_JDKS"] == r.mise_path("java", "temurin-23.0.2+7")


def test_repo_toolchains_xml_versions_join_the_need_and_its_jdk_home_is_ignored(tmp_path: Path) -> None:
    r = Run(tmp_path, "mvn-repo-toolchains").go()
    assert r.installs() == ["java@temurin-23.0.2+7"]
    assert r.env["OSSBOT_JDKS"] == r.mise_path("java", "temurin-23.0.2+7")
    assert "/Users/author" not in r.raw_env and "/Users/author" not in json.dumps(r.report)


def test_mvn_jvm_config_is_a_hint_only(tmp_path: Path) -> None:
    r = Run(tmp_path, "mvn-jvm-config").go()
    assert {"tool": "java", "source": ".mvn/jvm.config", "hint": "preview"} in r.report["tools"]
    assert r.env["JAVA_HOME"] == jdk(21)


def test_maven_4_wrapper_needs_java_17(tmp_path: Path) -> None:
    r = Run(tmp_path, "maven-wrapper-4").go()
    assert r.env["JAVA_HOME"] == jdk(17) and r.entry("maven")["declared"] == "4.0.0"


def test_workflow_java_version_is_a_hint_for_the_image_jdk(tmp_path: Path) -> None:
    r = Run(tmp_path, "workflow-hint").go()
    assert r.env["JAVA_HOME"] == jdk(11) and r.fake.calls == []


@pytest.mark.parametrize(("case", "install", "by"), [
    ("nvmrc", "node@22.23.4", "index"), ("nvmrc-lts", "node@20.20.2", "index"),
    ("node-version", "node@20.20.2", "declared"), ("engines", "node@20.20.2", None),
])  # fmt: skip
def test_node_declarations(tmp_path: Path, case: str, install: str, by: str | None) -> None:
    r = Run(tmp_path, case).go()
    assert r.installs() == [install]
    if by:
        assert r.entry("node")["resolved_by"] == by


@pytest.mark.parametrize("case", ["nvmrc-image", "engines-image"])
def test_node_declarations_the_image_satisfies(tmp_path: Path, case: str) -> None:
    r = Run(tmp_path, case).go()
    assert r.installs() == [] and r.entry("node")["action"] == "image" and "OSSBOT_PATH_PREPEND" not in r.env


def test_package_manager_through_corepack(tmp_path: Path) -> None:
    r = Run(tmp_path, "package-manager").go()
    node_bin = r.mise_path("node", "22.23.4") + "/bin"
    corepack = [a for a in r.fake.argvs() if a[0] == f"{node_bin}/corepack"]
    assert corepack[0] == [f"{node_bin}/corepack", "install"]
    shims = str(r.cache / "corepack-bin" / "bin")
    assert corepack[1] == [f"{node_bin}/corepack", "enable", "--install-directory", shims, "pnpm"]
    assert r.env["OSSBOT_PATH_PREPEND"].split(":")[:2] == [shims, node_bin]
    # warmed once at install (pnpm >= 11 fetches its platform binary on first use), with the chosen Node first
    (warm,) = [c for c in r.fake.calls if c[0] == [f"{shims}/pnpm", "--version"]]
    assert warm[1]["PATH"].startswith(f"{node_bin}:") and (r.state / "pnpm-12.15.0.warm").is_file()
    pm_calls = [c for c in r.fake.calls if c[0][0].endswith(("/corepack", "/pnpm"))]
    assert len(pm_calls) == 3 and {c[2] for c in pm_calls} == {str(r.state / "pm")}  # never in the repo
    scratch = json.loads((r.state / "pm" / "package.json").read_text())
    assert scratch["packageManager"] == "pnpm@12.15.0" and not (r.repo / "pnpm-lock.yaml").exists()
    assert r.entry("pnpm")["action"] == "install"
    calls = len(r.fake.calls)
    r.go("select", fetch=None)  # offline: nothing runs again
    assert len(r.fake.calls) == calls and r.entry("pnpm")["action"] == "cached"
    install_env = next(c[1] for c in r.fake.calls if c[0][1:] == ["install"] and c[0][0].endswith("corepack"))
    assert install_env["COREPACK_HOME"] == str(r.cache / "corepack")
    assert r.entry("pnpm")["chosen"] == "pnpm@12.15.0" and r.entry("pnpm")["action"] == "cached"  # the 2nd run


def test_package_manager_without_a_bundled_corepack_uses_the_baked_fallback(tmp_path: Path) -> None:
    run = Run(tmp_path, "package-manager")
    run.fake.node_corepack = False
    r = run.go()
    node_bin = r.mise_path("node", "22.23.4") + "/bin"
    fallback = next(a for a in r.fake.argvs() if tc.COREPACK_FALLBACK in a)
    assert fallback == [f"{node_bin}/npm", "i", "-g", "--offline", "--prefix", str(r.cache / "corepack-bin"),
                        "/opt/ossbot/corepack/corepack-0.36.0.tgz"]  # fmt: skip
    assert [str(r.cache / "corepack-bin" / "bin" / "corepack"), "install"] in r.fake.argvs()


@pytest.mark.parametrize(("case", "spec"), [("python-version", "3.11"), ("requires-python", ">=3.11,<3.12")])
def test_python_through_uv(tmp_path: Path, case: str, spec: str) -> None:
    r = Run(tmp_path, case).go()
    ((argv, env, _, _),) = r.fake.calls
    assert argv == [tc.UV, "python", "install", spec]
    assert env["UV_PYTHON_INSTALL_DIR"] == str(r.cache / "uv-python") and env["UV_PYTHON_DOWNLOADS"] == "automatic"
    assert env["UV_PYTHON_INSTALL_MIRROR"] == tc.PYTHON_MIRROR  # github_read hosts, no new egress (V-U1)
    assert r.entry("python")["path"].endswith("cpython-3.11.14-linux-x86_64-gnu")


def test_requires_python_the_image_satisfies(tmp_path: Path) -> None:
    r = Run(tmp_path, "requires-python-image").go()
    assert r.fake.calls == [] and r.entry("python")["action"] == "image"


def test_the_images_uv_managed_python_counts_as_installed(tmp_path: Path) -> None:
    managed = tmp_path / "uv-python"
    (managed / "cpython-3.13.9-linux-x86_64-gnu" / "bin").mkdir(parents=True)
    r = Run(tmp_path, None, image_pythons=managed)
    (r.repo / ".python-version").write_text("3.13\n")
    r.go()
    py = r.entry("python")
    assert r.fake.calls == [] and py["action"] == "image" and py["chosen"] == "3.13.9"
    assert py["path"] == str(managed / "cpython-3.13.9-linux-x86_64-gnu" / "bin")


def test_go_mod_only_is_a_minimum_and_the_image_satisfies_it(tmp_path: Path) -> None:
    """N6: toolchain go1.26.10 with the image's 1.27.3 -> the image, nothing installed (Go's own choice)."""
    r = Run(tmp_path, "go-mod-only").go()
    assert r.fake.calls == [] and r.entry("go")["chosen"] == tc.IMAGE_GO and r.entry("go")["action"] == "image"


def test_go_mod_above_the_image_installs_that_version(tmp_path: Path) -> None:
    r = Run(tmp_path, "go-mod-newer").go()
    assert r.installs() == ["go@1.28.1"]


def test_go_mise_minor_resolves_to_the_newest_patch(tmp_path: Path) -> None:
    r = Run(tmp_path, "go-mise-minor").go()
    assert r.installs() == ["go@1.26.10"] and r.entry("go")["resolved_by"] == "index"


def test_rust_toolchain_toml_channel_and_components(tmp_path: Path) -> None:
    r = Run(tmp_path, "rust-toolchain-toml").go()
    argvs = r.fake.argvs()
    assert argvs[0] == [tc.RUSTUP, "toolchain", "link", "ossbot-1.99.1", tc.RUST_IMAGE_DIR]
    assert argvs[1] == [tc.RUSTUP, "default", "ossbot-1.99.1"]
    assert argvs[2] == [tc.RUSTUP, "toolchain", "install", "1.98.0", "--profile", "minimal", "--no-self-update",
                        "-c", "rustfmt"]  # fmt: skip
    assert r.fake.calls[2][1]["RUSTUP_HOME"] == str(r.cache / "rustup")
    assert r.env["RUSTUP_TOOLCHAIN"] == "1.98.0" and r.env["RUSTUP_HOME"] == f"{r.work}/cache/rustup"


@pytest.mark.parametrize("case", ["rust-toolchain-legacy", "cargo-only"])
def test_rust_stable_or_none_uses_the_linked_image_toolchain(tmp_path: Path, case: str) -> None:
    r = Run(tmp_path, case).go()
    assert [a[1:3] for a in r.fake.argvs()] == [["toolchain", "link"], ["default", "ossbot-1.99.1"]]
    assert r.env["RUSTUP_TOOLCHAIN"] == "ossbot-1.99.1" and r.entry("rust")["action"] == "image"


def test_rust_offline_select_reuses_what_fetch_installed(tmp_path: Path) -> None:
    r = Run(tmp_path, "rust-toolchain-toml").go()
    calls = len(r.fake.calls)
    r.go("select", fetch=None)
    assert len(r.fake.calls) == calls and r.env["RUSTUP_TOOLCHAIN"] == "1.98.0"
    assert r.entry("rust")["action"] == "cached"


def test_terraform_version_exact(tmp_path: Path) -> None:
    assert Run(tmp_path, "terraform-version").go().installs() == ["terraform@1.5.7"]


def test_terraform_required_version_constraint_takes_the_newest_match(tmp_path: Path) -> None:
    r = Run(tmp_path, "tf-required").go()
    assert r.installs() == ["terraform@1.5.7"] and r.entry("terraform")["resolved_by"] == "index"


def test_terraform_required_version_the_image_satisfies(tmp_path: Path) -> None:
    r = Run(tmp_path, "tf-required-image").go()
    assert r.installs() == [] and r.entry("terraform")["action"] == "image"


def test_opentofu_from_mise_and_the_repo_mise_install(tmp_path: Path) -> None:
    r = Run(tmp_path, "opentofu-mise").go()
    assert r.installs() == ["opentofu@1.12.0"]
    assert r.fake.argvs()[-1] == [tc.MISE, "install"] and r.entry("mise")["exit_code"] == 0
    calls = len(r.fake.calls)
    r.go()  # the same config again: the repo's mise install doesn't run twice
    assert len(r.fake.calls) == calls


@pytest.mark.parametrize(("case", "version"), [("ruby-version", "3.3.5"), ("gemfile", "3.2.2")])
def test_ruby_precompiled_through_mise(tmp_path: Path, case: str, version: str) -> None:
    r = Run(tmp_path, case).go()
    ((argv, env, _, _),) = r.fake.calls
    assert argv == [tc.MISE, "install", f"ruby@{version}"] and env["MISE_RUBY_COMPILE"] == "false"
    assert env["MISE_DATA_DIR"] == str(r.cache / "mise") and f'ruby = "{version}"' in r.mise


# ------------------------------------------------------------------------------------------ precedence
@pytest.mark.parametrize(("case", "java_home"), [
    ("prec-java-mise-over-java-version", jdk(21)),  # mise 21 >= need 11
    ("prec-java-version-over-pom", jdk(21)),  # .java-version 21 >= need 17
    ("prec-pom-need-over-explicit", jdk(21)),  # .java-version 17 < need 21: exactly the need
])  # fmt: skip
def test_java_home_precedence(tmp_path: Path, case: str, java_home: str) -> None:
    assert Run(tmp_path, case).go().env["JAVA_HOME"] == java_home


def test_daemon_jvm_properties_win_over_the_explicit_version(tmp_path: Path) -> None:
    r = Run(tmp_path, "prec-daemon-over-explicit").go()
    assert r.env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(21) and r.env["JAVA_HOME"] == jdk(17)


def test_explicit_version_wins_over_the_run_matrix(tmp_path: Path) -> None:
    assert Run(tmp_path, "prec-explicit-over-matrix").go().env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(17)


def test_nvmrc_wins_over_engines(tmp_path: Path) -> None:
    assert Run(tmp_path, "prec-nvmrc-over-engines").go().installs() == ["node@20.20.2"]


def test_go_mise_exact_wins_over_the_go_mod_minimum(tmp_path: Path) -> None:
    assert Run(tmp_path, "prec-go-mise-over-gomod").go().installs() == ["go@1.26.10"]


# ------------------------------------------------------------------------------------- Gradle matrix
MATRIX = [("9.8.2", 25), ("9.0.0", 21), ("8.14.3", 21), ("8.5", 21), ("8.4", 17), ("7.6", 17), ("7.2", 11),
          ("6.9", 11), ("4.10", 8)]  # fmt: skip


@pytest.mark.parametrize(("gradle", "best"), MATRIX)
def test_gradle_run_matrix_picks_the_highest_image_jdk(tmp_path: Path, gradle: str, best: int) -> None:
    r = Run(tmp_path, None)
    (r.repo / "gradle" / "wrapper").mkdir(parents=True)
    (r.repo / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{gradle}-all.zip\n"
    )
    (r.repo / "build.gradle").write_text("")
    assert r.go().env["OSSBOT_GRADLE_JAVA_HOME"] == jdk(best)


def test_gradle_run_matrix_matches_the_pinned_table() -> None:
    from ossbot.common.yamlio import safe_load

    lock = safe_load((Path(__file__).resolve().parents[3] / "config" / "versions.lock.yaml").read_bytes())
    for key, value in lock["gradle_java_matrix"].items():
        if key.startswith("java_"):
            assert tc.GRADLE_RUN[int(key[5:])] == tc.vkey(value), key


# ------------------------------------------------------------------------------------------ budget
def test_install_budget_leaves_the_rest_as_budget_exceeded_gaps(tmp_path: Path) -> None:
    run = Run(tmp_path, "mise-toml", fake=None)
    run.fake.sleep = 2.2
    r = run.go(budget=2)
    assert r.installs() == ["java@temurin-22.0.2+9"]  # the first ran; the node install never started
    node = r.entry("node")
    assert node["gap"] == "budget_exceeded" and "node 22: budget_exceeded" in r.report["gaps"]
    assert 1 <= r.fake.calls[0][3] <= 2  # timeout = min(900, remaining)
    assert r.entry("mise")["gap"] == "budget_exceeded"  # the repo's own mise install too


def test_main_exits_0_on_gaps_and_2_on_bad_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake(tmp_path / "work" / "cache", fail=("java@",))
    monkeypatch.setattr(tc.Resolver, "_run", lambda self, argv, env, cwd, timeout: fake(argv, env, cwd, timeout))
    monkeypatch.setattr(tc, "fetch_url", index_fetch)
    shutil.copytree(FIXTURES / "gradle-toolchain", tmp_path / "work" / "repo")
    report = tmp_path / "out" / "toolchains.json"
    report.parent.mkdir()
    args = ["--repo", str(tmp_path / "work" / "repo"), "--state", str(tmp_path / "work" / ".home" / ".ossbot"),
            "--mode", "install", "--work", str(tmp_path / "work"), "--report", str(report)]  # fmt: skip
    assert tc.main(args) == 0
    doc = json.loads(report.read_text())
    (entry,) = [e for e in doc["tools"] if e["tool"] == "java-toolchain"]
    assert entry["gap"] == "install_failed" and entry["path"] == jdk(25)  # the smallest image JDK >= 22
    assert tc.main([*args[:-2], "--budget", "3601"]) == 2
    assert tc.main(["--repo", "x"]) == 2


# ------------------------------------------------------------------------------- indexes and the cache
def test_missing_index_passes_the_major_to_mise(tmp_path: Path) -> None:
    r = Run(tmp_path, "mise-toml").go(fetch=lambda url: None)
    assert r.installs() == ["java@temurin-22", "node@22"]
    assert r.entry("java")["resolved_by"] == "mise" and r.entry("node")["resolved_by"] == "mise"
    assert r.env["JAVA_HOME"] == r.mise_path("java", "temurin-22.9.9")  # what mise installed for the major


def test_indexes_are_cached_once_per_task_and_reused_offline(tmp_path: Path) -> None:
    fetched: list[str] = []

    def fetch(url: str) -> bytes | None:
        fetched.append(url)
        return index_fetch(url)

    r = Run(tmp_path, "mise-toml").go(fetch=fetch)
    assert sorted(fetched) == sorted([tc.INDEXES["java"], tc.INDEXES["node"]])
    assert (r.cache / "indexes" / "java.json").is_file()

    def boom(url: str) -> bytes | None:
        raise AssertionError("select never fetches")

    r.go("select", fetch=boom)
    assert r.entry("java")["action"] == "cached" and r.env["JAVA_HOME"] == r.mise_path("java", "temurin-22.0.2+9")


def test_select_without_an_install_falls_back_with_a_gap(tmp_path: Path) -> None:
    r = Run(tmp_path, "gradle-toolchain").go("select", fetch=None)
    assert r.fake.calls == []
    (entry,) = r.entries("java-toolchain")
    assert entry["gap"] == "not_installed" and entry["path"] == jdk(25)


def test_the_index_filter_skips_musl_and_other_feature_builds() -> None:
    entries = json.loads((FIXTURES / "indexes" / "mise-java-ga-linux-x86_64.json").read_text())
    assert any(e["features"] == ["musl"] and e["version"] == "22.0.2+9" for e in entries)
    resolver = tc.Resolver(tc.Ctx(repo=Path("/nonexistent"), state=Path("/nonexistent"), cache=Path("/nonexistent"),
                                  work="/work", mode="install", budget=0, fetch=index_fetch))  # fmt: skip
    assert resolver.java_version("temurin", 22) == "22.0.2+9"
    assert resolver.java_version("temurin", 99) is None


# ----------------------------------------------------------------------------- malformed and hostile input
def test_malformed_files_are_skipped_never_fatal(tmp_path: Path) -> None:
    r = Run(tmp_path, "malformed").go()
    skipped = {s["file"]: s["reason"] for s in r.report["skipped"]}
    assert skipped == {"mise.toml": "unparsable", "pom.xml": "unparsable", "package.json": "unparsable"}
    assert any(e.get("gap") == "invalid" and e["source"] == ".java-version" for e in r.entries("java"))
    assert r.entry("node")["gap"] == "invalid"
    assert r.env["JAVA_HOME"] == jdk(21)
    assert r.fake.argvs() == [[tc.MISE, "install"]]  # only the repo's own mise install (mise reports its config)


def test_a_doctype_is_refused(tmp_path: Path) -> None:
    r = Run(tmp_path, "doctype").go()
    assert r.report["skipped"] == [{"file": "pom.xml", "reason": "doctype"}]


def test_oversize_and_symlinked_files_are_not_read(tmp_path: Path) -> None:
    run = Run(tmp_path, None)
    (run.repo / "pom.xml").write_text("<project>" + " " * tc.MAX_FILE + "</project>")
    outside = tmp_path / "outside.txt"
    outside.write_text("23\n")
    os.symlink(outside, run.repo / ".java-version")
    os.symlink(tmp_path, run.repo / "linked")
    r = run.go()
    assert {"file": "pom.xml", "reason": "oversize"} in r.report["skipped"]
    assert r.env["JAVA_HOME"] == jdk(21) and r.fake.calls == []  # the symlinked 23 was never seen


def test_file_count_limit(tmp_path: Path) -> None:
    run = Run(tmp_path, None)
    (run.repo / ".sdkmanrc").write_text("maven=3.9.6\n")  # read 1
    (run.repo / ".java-version").write_text("17\n")  # read 2
    for i in range(tc.MAX_FILES + 5):  # the walk stops at 400 matches: reads 3..400, then the limit
        (run.repo / f"m{i:03}").mkdir()
        (run.repo / f"m{i:03}" / "pom.xml").write_text("<project/>")
    r = run.go()
    assert {"file": os.path.join("m398", "pom.xml"), "reason": "file_limit"} in r.report["skipped"]
    assert r.env["JAVA_HOME"] == jdk(17)


# ----------------------------------------------------------------------------------- the env file (§6.3)
GOOD = {
    "JAVA_HOME": "/usr/lib/jvm/java-25-openjdk-amd64",
    "OSSBOT_GRADLE_JAVA_HOME": "/work/cache/mise/installs/java/temurin-22.0.2+9",
    "OSSBOT_JDKS": (
        "/work/cache/mise/installs/java/temurin-22.0.2+9,/work/cache/mise/installs/java/corretto-21.0.9.10.1"
    ),
    "OSSBOT_PATH_PREPEND": "/work/cache/corepack-bin/bin:/work/cache/mise/installs/node/22.23.4/bin",
    "GOTOOLCHAIN": "local",
    "RUSTUP_TOOLCHAIN": "1.98.0",
    "RUSTUP_HOME": "/work/cache/rustup",
    "CARGO_HOME": "/work/cache/cargo",
    "COREPACK_HOME": "/work/cache/corepack",
    "UV_PYTHON_INSTALL_DIR": "/work/cache/uv-python",
    "UV_PYTHON_INSTALL_MIRROR": tc.PYTHON_MIRROR,
    "MISE_GLOBAL_CONFIG_FILE": "/work/.home/.ossbot/mise.toml",
}
BAD = [
    ("JAVA_HOME", "/usr/lib/jvm/java-22-openjdk-amd64"),
    ("JAVA_HOME", "/work/cache/mise/../../etc"),
    ("JAVA_HOME", "/work/cache/mise/./x"),
    ("JAVA_HOME", "/tmp/jdk"),
    ("JAVA_HOME", "/work/repo/jdk"),
    ("JAVA_HOME", "/usr/lib/jvm/java-21-openjdk-amd64 -Dx"),
    ("OSSBOT_PATH_PREPEND", "/work/cache/mise/bin:/usr/bin"),
    ("OSSBOT_PATH_PREPEND", "/work/cache/mise/bin:"),
    ("OSSBOT_PATH_PREPEND", ""),
    ("OSSBOT_JDKS", "/work/cache/mise/a,/home/x"),
    ("GOTOOLCHAIN", "auto"),
    ("RUSTUP_TOOLCHAIN", "-x"),
    ("RUSTUP_HOME", "/work/cache/rustup/x"),
    ("UV_PYTHON_INSTALL_MIRROR", "https://example.com/x"),
    ("MISE_GLOBAL_CONFIG_FILE", "/work/repo/mise.toml"),
    ("LD_PRELOAD", "/work/cache/mise/x.so"),
    ("PATH", "/work/cache/mise/bin"),
    ("JAVA_HOME", "/work/cache/mise/" + "a" * 2000),
    ("JAVA_HOME", "/work/cache/mise/a;b"),
    ("JAVA_HOME", "/work/cache/mise/a$(id)"),
]


@pytest.mark.parametrize("key", list(GOOD))
def test_every_valid_key_passes_all_three_readers(key: str) -> None:
    assert tc.valid_env(key, GOOD[key]) and execd.tc_valid(key, GOOD[key])


@pytest.mark.parametrize(("key", "value"), BAD)
def test_invalid_values_fail_all_readers(key: str, value: str) -> None:
    assert not tc.valid_env(key, value) and not execd.tc_valid(key, value)


def test_parse_env_drops_bad_lines_and_keeps_the_first_occurrence() -> None:
    text = "\n".join([f"{k}={v}" for k, v in GOOD.items()] + [f"{k}={v}" for k, v in BAD] + ["garbage", "=x"])
    text += "\nJAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64\n"
    assert tc.parse_env(text) == GOOD


def test_the_written_env_file_only_has_valid_lines(tmp_path: Path) -> None:
    r = Run(tmp_path, "package-manager").go()
    lines = [ln for ln in r.raw_env.splitlines() if ln]
    assert len(lines) == len(r.env) and all(tc.valid_env(*ln.split("=", 1), str(r.work)) for ln in lines)
    for name in ("toolchain.env", "mise.toml", "toolchains.json"):
        assert oct((r.state / name).stat().st_mode & 0o777) == "0o644"


def test_outputs_replace_a_planted_symlink(tmp_path: Path) -> None:
    run = Run(tmp_path, None)
    run.state.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    os.symlink(victim, run.state / "toolchain.env")
    run.go()
    assert victim.read_text() == "keep" and not (run.state / "toolchain.env").is_symlink()


# ----------------------------------------------------------------------------------------- the evaluators
@pytest.mark.parametrize(("version", "rng", "want"), [
    ("24.22.1", ">=18", True), ("24.22.1", ">=18 <21", False), ("20.20.2", ">=18 <21", True),
    ("20.1.0", "^20 || >= 22", True), ("21.0.0", "^20 || >= 22", False), ("22.0.0", "~22.1", False),
    ("22.1.5", "~22.1", True), ("20.0.0", "18.x || 20.x", True), ("19.9.9", "18 - 19", True),
    ("20.0.0", "18 - 19", False), ("22.3.0", ">22", False), ("23.0.0", ">22", True), ("22.3.0", ">22.3", False),
    ("22.4.0", "<=22.3", False),
    ("0.2.5", "^0.2.3", True), ("0.3.0", "^0.2.3", False), ("1.0.0", "*", True), ("1.0.0", "bogus!", None),
])  # fmt: skip
def test_npm_ranges(version: str, rng: str, want: bool | None) -> None:
    assert tc.npm_satisfies(version, rng) is want


@pytest.mark.parametrize(("version", "spec", "want"), [
    ((3, 12, 3), ">=3.11,<3.12", False), ((3, 11, 9), ">=3.11,<3.12", True), ((3, 12, 3), ">=3.9", True),
    ((3, 12, 3), "==3.12.*", True), ((3, 12, 3), "~=3.11", True), ((3, 12, 3), "~=3.11.2", False),
    ((3, 12, 3), "!=3.12.3", False), ((3, 12, 3), "<3", False), ((3, 12, 3), "3.12", None),
])  # fmt: skip
def test_pep440_specifiers(version: tuple[int, ...], spec: str, want: bool | None) -> None:
    assert tc.pep440_satisfies(version, spec) is want


@pytest.mark.parametrize(("version", "constraint", "want"), [
    ("1.17.0", "~> 1.5.0", False), ("1.5.7", "~> 1.5.0", True), ("1.17.0", "~> 1.5", True),
    ("1.17.0", ">= 1.3, < 2.0", True), ("1.17.0", "!= 1.17.0", False), ("1.17.0", "1.17.0", True),
    ("1.17.0", "x", None),
])  # fmt: skip
def test_terraform_constraints(version: str, constraint: str, want: bool | None) -> None:
    assert tc.tf_satisfies(version, constraint) is want


def test_go_version_order_follows_go() -> None:
    assert tc.go_version("1.27.3") > tc.go_version("1.26.10") > tc.go_version("1.26") > tc.go_version("1.26rc1")
    assert tc.go_version("go1.26.10") == (1, 26, 10, 0) and tc.go_version("1.x") is None


@pytest.mark.parametrize(("value", "sdkman", "want"), [
    ("21", False, (21, None, None)), ("temurin-21.0.2+13", False, (21, "temurin", "21.0.2+13")),
    ("corretto-21", False, (21, "corretto", None)), ("openjdk-25", False, (25, None, None)),
    ("1.8", False, (8, None, None)), ("21.0.2-tem", True, (21, "temurin", None)),
    ("17.0.10-zulu", True, (17, "zulu", None)), ("22.1.0.1.r21-gln", True, (22, None, None)),
    ("7", False, None), ("lts", False, None), ("21; rm", False, None),
])  # fmt: skip
def test_java_declarations(value: str, sdkman: bool, want: tuple[int, str | None, str | None] | None) -> None:
    spec = tc.parse_java(value, "x", sdkman)
    assert (None if spec is None else (spec.feature, spec.vendor, spec.exact)) == want


def test_resolver_is_stdlib_only() -> None:
    import ast

    tree = ast.parse(Path(tc.__file__).read_text(encoding="utf-8"))
    modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not modules & {"ossbot", "pydantic", "yaml", "ruamel", "httpx", "requests"}
