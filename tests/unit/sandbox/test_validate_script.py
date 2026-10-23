"""validate.sh and exec.sh (sandbox-network Part 1, R1.2, R1.8; DESIGN §9.8, §9.9): no GRADLE_OPTS (not a
JVM option: it killed every ./gradlew), the JVM setup of lib/jvm_env.sh, and pipefail for the plan's commands."""

from __future__ import annotations

import re
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
