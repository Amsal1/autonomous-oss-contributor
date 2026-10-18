"""Child process for test_crash_safety.py: runs one long validate job the way sandboxd does
(row first, then the four-step lifecycle) until the parent SIGKILLs it mid-job.

    python -m tests.sandbox.crash_child <root> <run_id> <cid>
"""

from __future__ import annotations

import sys
from pathlib import Path

from ossbot.common.clock import to_iso
from ossbot.sandboxd.jobs import Plan
from tests.sandbox.conftest import make_box


def main(root: str, run_id: str, cid: str) -> int:
    box = make_box(Path(root))
    svc = box.svc
    plan = Plan(
        run_id=run_id, cid=cid, template="validate", entrypoint="validate", repo_mode="rw",
        env={"OSSBOT_ARG_PLAN_FILE": "plan.json"}, inputs={"plan.json": Path(root) / "inputs" / "plan.json"},
        timeout_s=900, cpus=1.0, memory_mb=512, dest=svc.settings.artifacts_dir / cid / "runs" / run_id,
    )  # fmt: skip
    with svc.store.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (run_id, cid, profile, entrypoint, state, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, cid, "validate", "validate", "queued", to_iso(svc.clock.now())),
        )
    with svc._idle:
        svc._running.add(run_id)
    svc._run_job(plan)  # never returns normally: the parent kills this process mid-job
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
