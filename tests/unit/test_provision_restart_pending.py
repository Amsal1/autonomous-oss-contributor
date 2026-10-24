"""F-SN-1 (build-env design §11, BE-9, AC 16): provision remembers a pending service restart across a failed run.

``scripts/lib/services.sh`` writes ``$OSSBOT_PROVISION_STATE/restart-pending`` where it sets ``OSSBOT_APP_CHANGED``
or ``OSSBOT_UNITS_CHANGED``; ``step_enable_start`` restarts the running services when the marker exists (a
``daemon-reload`` first for ``units``) and removes it only after the restart and is-active loops succeeded. The
harness (``tests/fixtures/provision/restart_pending.sh``) sources the real scripts with stubbed ``systemctl``,
``_flag_level``, ``mark_changed``, ``log``, ``warn`` and ``die``."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

HARNESS = REPO_ROOT / "tests" / "fixtures" / "provision" / "restart_pending.sh"
SERVICES = [
    "ossbot-floorlog.service", "ossbot-dockerfilter.service", "ossbot-gatekeeper.service", "ossbot-proxy.service",
    "ossbot-sandboxd.service", "ossbot-runner.service", "ossbot-hermes.service",
]  # fmt: skip
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name != "posix", reason="needs bash (Linux)")


def _run(state: Path, calls: Path, scenario: str, **env: str) -> tuple[int, list[str]]:
    calls.unlink(missing_ok=True)
    proc = subprocess.run([BASH, str(HARNESS), str(state), str(calls), scenario], capture_output=True, text=True,
                          env={**os.environ, **env}, timeout=60, check=False)  # fmt: skip
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode, lines


def _restarts(lines: list[str]) -> list[str]:
    return [ln.split()[-1] for ln in lines if ln.startswith("systemctl restart ")]


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "provision"
    return state, tmp_path / "calls.txt", state / "restart-pending"


def test_1_marker_written_on_change_survives_a_failed_run(paths: tuple[Path, Path, Path]) -> None:
    state, calls, marker = paths
    code, lines = _run(state, calls, "app-then-die")
    assert code == 1 and lines[-1] == "die build sandbox-full failed"
    assert marker.read_text() == "app\n"
    assert oct(marker.stat().st_mode & 0o777) == "0o600" and oct(state.stat().st_mode & 0o777) == "0o700"
    _run(state, calls, "app-then-die")  # marked twice: still one line
    assert marker.read_text() == "app\n"


def test_2_the_next_unchanged_run_restarts_and_clears(paths: tuple[Path, Path, Path]) -> None:
    state, calls, marker = paths
    _run(state, calls, "app-then-die")
    code, lines = _run(state, calls, "enable-start")
    assert code == 0, lines
    assert "log restart pending from an earlier run (app)" in lines
    assert _restarts(lines) == SERVICES
    assert "systemctl daemon-reload" not in lines
    assert not marker.exists()


def test_3_a_units_marker_reloads_systemd_first(paths: tuple[Path, Path, Path]) -> None:
    state, calls, marker = paths
    code, lines = _run(state, calls, "install-unit")
    assert code == 0 and "units_changed 1" in lines and marker.read_text() == "units\n"
    code, lines = _run(state, calls, "enable-start")
    assert code == 0 and "log restart pending from an earlier run (units)" in lines
    reload = lines.index("systemctl daemon-reload")
    first = next(i for i, ln in enumerate(lines) if ln.startswith("systemctl restart "))
    assert reload < first and _restarts(lines) == SERVICES and not marker.exists()


def test_4_a_failing_restart_keeps_the_marker(paths: tuple[Path, Path, Path]) -> None:
    state, calls, marker = paths
    _run(state, calls, "app-then-die")
    code, lines = _run(state, calls, "enable-start", FAIL_RESTART="1")
    assert code == 1 and lines[-1] == "die restart ossbot-floorlog.service failed"
    assert marker.read_text() == "app\n"
    code, lines = _run(state, calls, "enable-start")  # the run after that retries and clears
    assert code == 0 and _restarts(lines) == SERVICES and not marker.exists()


def test_5_no_marker_no_change_no_restart(paths: tuple[Path, Path, Path]) -> None:
    state, calls, marker = paths
    code, lines = _run(state, calls, "enable-start")
    assert code == 0 and _restarts(lines) == [] and not any("restart pending" in ln for ln in lines)
    assert not marker.exists()


def test_this_runs_own_change_is_not_called_earlier(paths: tuple[Path, Path, Path]) -> None:
    state, calls, marker = paths
    code, lines = _run(state, calls, "enable-start-app-changed")
    assert code == 0 and _restarts(lines) == SERVICES
    assert not any("restart pending" in ln for ln in lines) and not marker.exists()


def test_a_planted_symlink_counts_as_pending_and_is_never_followed(paths: tuple[Path, Path, Path],
                                                                   tmp_path: Path) -> None:  # fmt: skip
    state, calls, marker = paths
    state.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    marker.symlink_to(victim)
    code, lines = _run(state, calls, "enable-start")
    assert code == 0 and "log restart pending from an earlier run (app, units)" in lines
    assert _restarts(lines) == SERVICES and not marker.exists() and victim.read_text() == "keep\n"
    marker.symlink_to(victim)
    _run(state, calls, "app-then-die")  # marking replaces the link instead of writing through it
    assert not marker.is_symlink() and marker.read_text() == "app\n" and victim.read_text() == "keep\n"


def test_services_sh_marks_both_app_changes_and_install_unit() -> None:
    text = (REPO_ROOT / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    assert text.count("OSSBOT_APP_CHANGED=1\n") == 2
    assert text.count("restart_pending_mark app\n") == 2
    assert "OSSBOT_UNITS_CHANGED=1; restart_pending_mark units" in text
    assert "/var/lib/ossbot/provision" in text and "/var/lib/ossbot/control" not in text.split("_flag_level", 1)[0]
