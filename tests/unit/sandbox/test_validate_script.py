"""validate.sh and exec.sh (sandbox-network Part 1, R1.2, R1.8; DESIGN §9.8, §9.9): no GRADLE_OPTS (not a
JVM option: it killed every ./gradlew), the JVM setup of lib/jvm_env.sh, and pipefail for the plan's commands."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINTS = ROOT / "sandbox" / "entrypoints"
VALIDATE_SH = (ENTRYPOINTS / "validate.sh").read_text(encoding="utf-8")
EXEC_SH = (ENTRYPOINTS / "exec.sh").read_text(encoding="utf-8")
SOURCE_LIB = 'source "$(dirname -- "$0")/lib/jvm_env.sh"'


def code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_no_entrypoint_or_executor_sets_gradle_opts() -> None:
    for path in [*ENTRYPOINTS.rglob("*.sh"), ROOT / "src" / "ossbot" / "sandbox_tools" / "execd.py"]:
        assert "GRADLE_OPTS=" not in code(path.read_text(encoding="utf-8")), path
        assert '"GRADLE_OPTS"' not in path.read_text(encoding="utf-8"), path


def test_validate_runs_the_jvm_setup_and_pipefail() -> None:
    body = code(VALIDATE_SH)
    assert SOURCE_LIB in body
    assert re.search(r"^ossbot_jvm_setup validate$", body, re.MULTILINE)
    assert body.index("GRADLE_USER_HOME=") < body.index("ossbot_jvm_setup validate") < body.index('cd "$WORK/repo"')
    assert 'bash -o pipefail -lc "$cmd"' in body
    assert 'bash -lc "$cmd"' not in body


def test_exec_runs_the_offline_jvm_setup_before_execd() -> None:
    body = code(EXEC_SH)
    assert "export WORK=/work HOME=/work/.home GRADLE_USER_HOME=/work/cache/gradle" in body
    assert SOURCE_LIB in body
    assert body.index("ossbot_jvm_setup offline") < body.index("exec /opt/ossbot/bin/ossbot-execd")


def test_exec_validates_the_network_and_passes_it_to_execd() -> None:
    """ADR-0024: OSSBOT_EXEC_NETWORK must be empty or web (else exit 3 before execd starts); online uses the
    online JVM setup and execd --network web."""
    body = code(EXEC_SH)
    check = body.index("[[ ! $NET =~ ^(web)?$ ]]")
    assert check < body.index("exit 3") < body.index("ossbot_jvm_setup")
    assert "ossbot_jvm_setup online" in body and "NETWORK=web" in body and "NETWORK=none" in body
    assert body.index("ossbot_jvm_setup online") < body.index("exec /opt/ossbot/bin/ossbot-execd")
    assert '--network "$NETWORK"' in body


# ------------------------------------------------------------------- netshim (build-env design §3.2)
SOURCE_NETSHIM = 'source "$(dirname -- "$0")/lib/netshim.sh"'
NETSHIM_SH = ENTRYPOINTS / "lib" / "netshim.sh"


def test_fetch_and_validate_start_netshim_first_and_validate_writes_the_gitconfig() -> None:
    fetch = code((ENTRYPOINTS / "fetch.sh").read_text(encoding="utf-8"))
    assert SOURCE_NETSHIM in fetch and fetch.index("ossbot_netshim_start") < fetch.index('rm -rf "$REPO"')
    body = code(VALIDATE_SH)
    assert SOURCE_NETSHIM in body
    assert body.index("ossbot_netshim_start") < body.index("ossbot_jvm_setup validate") < body.index("timeout --kill")
    assert body.index("ossbot_gitconfig") < body.index('cd "$WORK/repo"')


def test_exec_starts_netshim_only_with_web_and_always_writes_the_gitconfig() -> None:
    body = code(EXEC_SH)
    web = body.split("if [[ $NET == web ]]; then", 1)[1].split("else", 1)[0]
    offline = body.split("if [[ $NET == web ]]; then", 1)[1].split("else", 1)[1].split("fi", 1)[0]
    assert "ossbot_netshim_start" in web and "ossbot_netshim_start" not in offline
    assert body.index("ossbot_gitconfig") < body.index("if [[ $NET == web ]]; then")


def _run_lib(tmp_path: Path, script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = f'set -euo pipefail\nsource "{NETSHIM_SH}"\nNETSHIM_BIN="{tmp_path}/fake-netshim"\n{script}\n'
    return subprocess.run(["/bin/bash", "-c", full], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **env}, check=False)  # fmt: skip


def _fake(tmp_path: Path, rc: int) -> Path:
    fake = tmp_path / "fake-netshim"
    fake.write_text(f'#!/bin/bash\necho "$*" >>"{tmp_path}/calls"\nexit {rc}\n')
    fake.chmod(0o755)
    return tmp_path / "calls"


def test_netshim_start_daemonizes_with_a_proxy_and_a_network(tmp_path: Path) -> None:
    calls = _fake(tmp_path, 0)
    if not [p for p in Path("/sys/class/net").iterdir() if p.name != "lo"]:
        return  # a host without a NIC can't model a networked container
    out = _run_lib(tmp_path, "ossbot_netshim_start", {"HTTPS_PROXY": "http://proxy:3128"})
    assert out.returncode == 0 and calls.read_text() == "--daemon --pidfile /tmp/netshim.pid --log /tmp/netshim.log\n"
    calls.unlink()
    out = _run_lib(tmp_path, "ossbot_netshim_start", {})
    assert out.returncode == 0 and not calls.exists()  # no proxy: nothing to translate to


def test_netshim_bind_failure_is_one_stderr_line_and_the_job_continues(tmp_path: Path) -> None:
    _fake(tmp_path, 1)
    if not [p for p in Path("/sys/class/net").iterdir() if p.name != "lo"]:
        return
    out = _run_lib(tmp_path, "ossbot_netshim_start; echo continued", {"HTTPS_PROXY": "http://proxy:3128"})
    assert out.returncode == 0 and out.stdout == "continued\n"
    assert out.stderr.count("\n") == 1 and "netshim: not started" in out.stderr


def test_gitconfig_sends_ssh_github_urls_over_https(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").symlink_to(tmp_path / "elsewhere")  # replaced, never followed
    out = _run_lib(tmp_path, "ossbot_gitconfig", {"HOME": str(home)})
    assert out.returncode == 0 and not (home / ".gitconfig").is_symlink() and not (tmp_path / "elsewhere").exists()
    assert oct((home / ".gitconfig").stat().st_mode & 0o777) == "0o644"
    got = subprocess.run(["/usr/bin/git", "config", "--file", str(home / ".gitconfig"), "--get-all",
                          "url.https://github.com/.insteadof"], capture_output=True, text=True, check=True)  # fmt: skip
    assert got.stdout.split() == ["git@github.com:", "ssh://git@github.com/"]


def test_netshim_is_baked_into_the_full_image_only() -> None:
    full = (ROOT / "sandbox" / "images" / "full" / "Dockerfile").read_text(encoding="utf-8")
    llm = (ROOT / "sandbox" / "images" / "llm" / "Dockerfile").read_text(encoding="utf-8")
    assert "src/ossbot/sandbox_tools/netshim.py /opt/ossbot/bin/ossbot-netshim" in full
    assert "sandbox/entrypoints/lib/netshim.sh" in full
    assert "netshim" not in llm
    images = (ROOT / "scripts" / "lib" / "images.sh").read_text(encoding="utf-8")
    full_build = images.split("_build sandbox-full sandbox/images/full", 1)[1].split("_build sandbox-llm", 1)[0]
    assert "src/ossbot/sandbox_tools/netshim.py" in full_build
