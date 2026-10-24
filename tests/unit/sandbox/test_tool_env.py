"""sandbox/entrypoints/lib/tool_env.sh (build-env design §6.2 M3, §6.3, BE-4.3, BE-4.8, C-N3): the shared
toolchain exports of fetch, validate and exec, the resolver call with the offline switches unset, the env-file
reader (the same rule as sandbox_tools/toolchains.valid_env and execd.tc_valid) and the PATH."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ossbot.sandbox_tools import execd
from ossbot.sandbox_tools import toolchains as tc
from tests.unit.sandbox_tools.test_toolchains import BAD, GOOD

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "sandbox" / "entrypoints" / "lib" / "tool_env.sh"
SWITCHES = ("UV_OFFLINE", "PIP_NO_INDEX", "GOPROXY", "npm_config_offline")

pytestmark = pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")


def bash(work: Path, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = f'set -euo pipefail\nsource "{LIB}"\n{script}\n'
    base = {"PATH": "/usr/bin:/bin", "WORK": str(work), "HOME": str(work / ".home")}
    return subprocess.run(["/bin/bash", "-c", full], capture_output=True, text=True, env={**base, **(env or {})},
                          check=False, timeout=30)  # fmt: skip


def exported(work: Path, mode: str, env: dict[str, str] | None = None) -> dict[str, str]:
    out = bash(work, f"ossbot_tool_env {mode}\nenv", env)
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


@pytest.mark.parametrize("mode", ["online", "offline"])
def test_shared_exports(tmp_path: Path, mode: str) -> None:
    env = exported(tmp_path, mode)
    assert env["MISE_USE_VERSIONS_HOST"] == "false" and env["MISE_USE_VERSIONS_HOST_TRACK"] == "false"  # BE-4.8
    assert env["MISE_DATA_DIR"] == f"{tmp_path}/cache/mise"
    assert env["MISE_SHARED_INSTALL_DIRS"] == "/opt/mise/installs"  # the image's tools as the read-only fallback
    assert env["MISE_SYSTEM_CONFIG_FILE"] == "/opt/mise/config/config.toml"
    assert env["MISE_GLOBAL_CONFIG_FILE"] == f"{tmp_path}/.home/.ossbot/mise.toml"
    assert env["MISE_TRUSTED_CONFIG_PATHS"] == f"{tmp_path}/repo" and env["MISE_YES"] == "1"
    assert env["MISE_RUBY_COMPILE"] == "false" and env["GOTOOLCHAIN"] == "local"
    assert env["RUSTUP_HOME"] == f"{tmp_path}/cache/rustup" and env["CARGO_HOME"] == f"{tmp_path}/cache/cargo"
    assert env["COREPACK_HOME"] == f"{tmp_path}/cache/corepack" and env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] == "0"
    assert env["UV_PYTHON_INSTALL_DIR"] == f"{tmp_path}/cache/uv-python"
    assert env["UV_PYTHON_INSTALL_MIRROR"] == tc.PYTHON_MIRROR
    offline = {"MISE_OFFLINE": "1", "RUSTUP_AUTO_INSTALL": "0", "COREPACK_ENABLE_NETWORK": "0"}
    if mode == "offline":
        assert env["UV_PYTHON_DOWNLOADS"] == "never" and offline.items() <= env.items()
    else:
        assert env["UV_PYTHON_DOWNLOADS"] == "automatic" and not set(offline) & set(env)
    assert (tmp_path / ".home" / ".ossbot").is_dir()


def test_online_drops_inherited_offline_switches(tmp_path: Path) -> None:
    env = exported(tmp_path, "online", {"MISE_OFFLINE": "1", "RUSTUP_AUTO_INSTALL": "0"})
    assert "MISE_OFFLINE" not in env and "RUSTUP_AUTO_INSTALL" not in env


def test_the_same_set_as_execd(tmp_path: Path) -> None:
    """One export set (§6.3): every toolchain variable of execd's fixed_env equals tool_env.sh's."""
    for mode, online in (("offline", False), ("online", True)):
        shell = exported(tmp_path, mode)
        fixed = execd.fixed_env(str(tmp_path), online=online)
        for key, value in fixed.items():
            if key.startswith(("MISE_", "RUSTUP_", "COREPACK_", "UV_PYTHON")) or key in ("GOTOOLCHAIN", "CARGO_HOME"):
                assert shell.get(key) == value, (mode, key)
        for key in ("MISE_OFFLINE", "RUSTUP_AUTO_INSTALL", "COREPACK_ENABLE_NETWORK"):
            assert (key in shell) is (key in fixed), (mode, key)


def test_bad_mode_and_missing_work(tmp_path: Path) -> None:
    assert bash(tmp_path, "ossbot_tool_env bogus").returncode == 2
    out = subprocess.run(["/bin/bash", "-c", f'source "{LIB}"; ossbot_tool_env online'], capture_output=True,
                         text=True, env={"PATH": "/usr/bin:/bin"}, check=False)  # fmt: skip
    assert out.returncode == 2 and "WORK and HOME" in out.stderr


def _fake_resolver(tmp_path: Path, rc: int = 0) -> Path:
    fake = tmp_path / "fake-resolver.py"
    fake.write_text(
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(tmp_path / 'seen.json')!r}, 'w'))\n"
        f"sys.exit({rc})\n"
    )
    return fake


def test_cn3_resolver_runs_without_the_offline_switches(tmp_path: Path) -> None:
    """C-N3: the switches validate exports for its plan commands never reach the resolver."""
    fake = _fake_resolver(tmp_path)
    out = bash(tmp_path, f'ossbot_offline_switches\nossbot_toolchains install 600 "{tmp_path}/r.json"\nenv',
               {"TOOLCHAINS_BIN": str(fake)})  # fmt: skip
    assert out.returncode == 0, out.stderr
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert not set(SWITCHES) & set(seen["env"])
    after = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    assert {
        "UV_OFFLINE": "1",
        "PIP_NO_INDEX": "1",
        "GOPROXY": "off",
        "npm_config_offline": "true",
    }.items() <= after.items()
    assert seen["argv"] == ["--repo", f"{tmp_path}/repo", "--state", f"{tmp_path}/.home/.ossbot", "--mode", "install",
                            "--budget", "600", "--work", str(tmp_path), "--report", f"{tmp_path}/r.json"]  # fmt: skip


def test_a_failing_resolver_is_one_stderr_line_and_the_job_continues(tmp_path: Path) -> None:
    fake = _fake_resolver(tmp_path, rc=1)
    out = bash(tmp_path, "ossbot_toolchains select 0; echo continued", {"TOOLCHAINS_BIN": str(fake)})
    assert out.returncode == 0 and out.stdout.endswith("continued\n")
    assert "the toolchain resolver failed; the image defaults apply" in out.stderr
    assert bash(tmp_path, "ossbot_toolchains install x").returncode == 2


def _write_env(work: Path, text: str) -> None:
    (work / ".home" / ".ossbot").mkdir(parents=True, exist_ok=True)
    (work / ".home" / ".ossbot" / "toolchain.env").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("key", list(GOOD))
def test_tc_read_accepts_what_the_resolver_accepts(key: str) -> None:
    work = Path("/work")
    value = GOOD[key]
    out = subprocess.run(["/bin/bash", "-c", f'source "{LIB}"; _ossbot_tc_valid "$1" "$2" && echo ok', "x", key,
                          value], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "WORK": str(work)},
                         check=False)  # fmt: skip
    assert out.stdout.strip() == "ok" and tc.valid_env(key, value) and execd.tc_valid(key, value)


@pytest.mark.parametrize(("key", "value"), BAD)
def test_tc_read_refuses_what_the_resolver_refuses(key: str, value: str) -> None:
    out = subprocess.run(["/bin/bash", "-c", f'source "{LIB}"; _ossbot_tc_valid "$1" "$2" && echo ok', "x", key,
                          value], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "WORK": "/work"},
                         check=False)  # fmt: skip
    assert out.stdout.strip() == ""


def test_tc_read_first_valid_occurrence_and_symlinks(tmp_path: Path) -> None:
    jdk = f"{tmp_path}/cache/mise/installs/java/temurin-22.0.2+9"
    _write_env(tmp_path, f"JAVA_HOME={jdk}\nJAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64\nGOTOOLCHAIN=auto\n")
    out = bash(tmp_path, "ossbot_tc_read JAVA_HOME; echo .; ossbot_tc_read GOTOOLCHAIN; echo .")
    assert out.stdout.split("\n")[:4] == [jdk, ".", ".", ""]
    real = tmp_path / ".home" / ".ossbot" / "toolchain.env"
    real.rename(tmp_path / "elsewhere")
    real.symlink_to(tmp_path / "elsewhere")
    assert bash(tmp_path, "ossbot_tc_read JAVA_HOME").stdout == ""


def test_tool_paths(tmp_path: Path) -> None:
    node = f"{tmp_path}/cache/mise/installs/node/22.23.4/bin"
    _write_env(tmp_path, f"OSSBOT_PATH_PREPEND={tmp_path}/cache/corepack-bin/bin:{node}\nRUSTUP_TOOLCHAIN=1.98.0\n")
    out = bash(tmp_path, 'JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64 ossbot_tool_paths venv; echo "$PATH"; '
                         'echo "$RUSTUP_TOOLCHAIN"', {"RUSTUP_TOOLCHAIN": "stale"})  # fmt: skip
    path, toolchain = out.stdout.splitlines()
    assert path == (f"{tmp_path}/cache/corepack-bin/bin:{node}:/usr/lib/jvm/java-25-openjdk-amd64/bin:"
                    f"{tmp_path}/venv/bin:/opt/mise/shims:/opt/rust/cargo/bin:/usr/bin:/bin")  # fmt: skip
    assert toolchain == "1.98.0"
    _write_env(tmp_path, "OSSBOT_PATH_PREPEND=/usr/bin\n")  # refused: only toolchain dirs are prepended
    out = bash(tmp_path, 'ossbot_tool_paths novenv; echo "$PATH"; echo "${RUSTUP_TOOLCHAIN-unset}"',
               {"RUSTUP_TOOLCHAIN": "x"})  # fmt: skip
    assert out.stdout.splitlines() == ["/usr/lib/jvm/java-21-openjdk-amd64/bin:/opt/mise/shims:/opt/rust/cargo/bin:"
                                       "/usr/bin:/bin", "unset"]  # fmt: skip
    assert bash(tmp_path, "ossbot_tool_paths").returncode == 2


def test_entrypoints_source_tool_env_before_jvm_env() -> None:
    for name in ("fetch.sh", "validate.sh", "exec.sh"):
        text = (ROOT / "sandbox" / "entrypoints" / name).read_text(encoding="utf-8")
        assert text.index('lib/tool_env.sh"') < text.index('lib/jvm_env.sh"'), name
