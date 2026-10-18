"""CR-001 item 6c: SIGKILL sandboxd mid-job (battery death, force shutdown), restart, reconcile.

A child process runs a real validate job (``sleep``) through the Docker filter exactly like
sandboxd. It is SIGKILLed while the container runs. A fresh JobService on the same database
(what the restarted sandboxd does in ``main``) must mark the job ``interrupted`` and remove its
container and per-run volumes, while the task volume stays for the re-queue. Gatekeeper-side
crash safety (no duplicate public writes after a restart mid-submission) is
tests/unit/gatekeeper/test_submit.py::test_restart_reconciles_executing.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ossbot.sandboxd.argv import Volumes
from ossbot.sandboxd.jobs import INTERRUPTED
from tests.sandbox.conftest import make_box, require_provisioned

CID = "c_CRASHTEST1"
REPO = Path(__file__).resolve().parents[2]


def _docker(*args: str) -> str:
    return subprocess.run(["/usr/bin/docker", *args], capture_output=True, text=True, check=False).stdout


def _running(run_id: str) -> bool:
    return bool(_docker("ps", "-q", "--filter", f"label=ossbot.run={run_id}", "--filter", "status=running").strip())


def test_sigkill_mid_job_then_recover(tmp_path: Path) -> None:
    require_provisioned()
    root = tmp_path / "crash"
    root.mkdir()
    box = make_box(root)
    run_id = f"crash-{os.getpid()}"
    plan = {"commands": [{"name": "long", "cmd": "sleep 600", "timeout_s": 900}]}
    (root / "inputs" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    box.fixture_task_volume(CID)
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.sandbox.crash_child", str(root), run_id, CID], cwd=REPO,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 180
        while not _running(run_id):
            assert child.poll() is None, "the child job ended before it could be killed"
            assert time.monotonic() < deadline, "the validate container never started"
            time.sleep(1)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
        assert box.svc.get(run_id)["state"] == "running"  # what the dead process left behind

        fresh = make_box(root)  # the restarted sandboxd
        report = fresh.svc.recover(sweep_orphans=False)  # never touch other tests' containers
        assert report["interrupted"] == [run_id]
        row = fresh.svc.get(run_id)
        assert row["state"] == "failed" and row["error_class"] == INTERRUPTED and row["artifact_dir"] is None
        assert not _docker("ps", "-aq", "--filter", f"label=ossbot.run={run_id}").strip()
        vols = Volumes(run_id, CID)
        names = set(_docker("volume", "ls", "--format", "{{.Name}}").split())
        assert vols.input not in names and vols.output not in names
        assert vols.task in names  # kept for the re-queue on up
        assert fresh.svc.recover(sweep_orphans=False)["interrupted"] == []
    finally:
        if child.poll() is None:
            child.kill()
        for cid_container in _docker("ps", "-aq", "--filter", f"label=ossbot.run={run_id}").split():
            _docker("rm", "-f", cid_container)
        box.cleanup(CID)
