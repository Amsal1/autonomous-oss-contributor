"""Read-only snapshot for research jobs (DESIGN §10.2 step 2, H1, N3).

A ``snapshot`` helper (task volume read-only at ``/work``, ``ossbot-ro-<run_id>`` rw at
``/snap``) runs ``snapshot.sh``; then a ``probe`` container with the ro volume mounted read-only
lists everything that is not a regular file or directory. Any output fails the job with
``error_class=snapshot_unsafe`` and pauses the pipeline.
"""

from __future__ import annotations

from ossbot.sandboxd.argv import Volumes, probe_spec, snapshot_spec
from ossbot.sandboxd.runtime.base import ContainerRuntime, Mount

SNAPSHOT_TIMEOUT_S = 900
CHECK_TIMEOUT_S = 120


class SnapshotError(RuntimeError):
    def __init__(self, error_class: str, detail: str = "") -> None:
        super().__init__(f"{error_class}: {detail}")
        self.error_class = error_class


def postcheck_clean(output: bytes) -> bool:
    """The post-check prints one line per non-regular, non-directory entry; clean = nothing."""
    return output.strip() == b""


def take_snapshot(runtime: ContainerRuntime, run_id: str, cid: str, full_image: str, created: list[str]) -> None:
    """Fill ``ossbot-ro-<run_id>`` from the task volume and verify it. ``created`` collects names."""
    vols = Volumes(run_id, cid)
    snap = snapshot_spec(run_id, cid, full_image)
    runtime.run(snap)
    created.append(snap.name)
    try:
        code = runtime.wait(snap.name, SNAPSHOT_TIMEOUT_S)
    except TimeoutError as exc:
        raise SnapshotError("snapshot_timeout") from exc
    if code != 0:
        raise SnapshotError("snapshot_failed", f"exit {code}")
    check = probe_spec(
        run_id, cid, full_image, "snapshot_check", (Mount(vols.ro, "/work/repo", read_only=True),), suffix="snapcheck"
    )
    runtime.run(check)
    created.append(check.name)
    try:
        code = runtime.wait(check.name, CHECK_TIMEOUT_S)
    except TimeoutError as exc:
        raise SnapshotError("snapshot_unsafe", "post-check timed out") from exc
    output = runtime.logs(check.name, None)
    if code != 0 or not postcheck_clean(output):
        raise SnapshotError("snapshot_unsafe", "non-regular entries in the snapshot")
