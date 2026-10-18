"""CR-001 in sandboxd: lifecycle admission, drain with timeout, crash recovery at start."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from ossbot.sandboxd.jobs import INTERRUPTED, ApiError, JobService
from ossbot.sandboxd.profiles import JobSpec
from ossbot.sandboxd.runtime.fake import FakeContainer, FakeRuntime
from tests.conftest import write_flag
from tests.unit.sandboxd.conftest import Sbx


def _fetch(run_id: str, cid: str) -> JobSpec:
    return JobSpec.model_validate(
        {"run_id": run_id, "cid": cid, "profile": "fetch", "entrypoint": "fetch", "args": {"repo": "owner/repo"},
         "repo_mode": "rw", "timeout_s": 600}
    )  # fmt: skip


class Gate:
    """Fetch containers stay 'running' until released or killed; wait() blocks like docker wait."""

    def __init__(self, runtime: FakeRuntime) -> None:
        self.release: dict[str, threading.Event] = {}
        self.started = threading.Semaphore(0)
        self.abandoned = threading.Event()
        runtime.handlers["fetch"] = self._handler
        original_wait = runtime.wait

        def wait(container: str, timeout_s: int) -> int:
            c = runtime._require(container)
            if c.spec.entrypoint == "fetch":
                ev = self.release[c.spec.run_id]
                deadline = time.monotonic() + timeout_s
                while c.status == "running" and not ev.wait(0.02):
                    if self.abandoned.is_set():
                        threading.Event().wait()  # the "crashed" process's thread: parked, writes nothing
                    if time.monotonic() > deadline:
                        raise TimeoutError(container)
                if c.status == "running":
                    c.status, c.exit_code = "exited", 0
                    out = c.volume_path(runtime, "/io/output")
                    if out is not None:
                        (out / "result.json").write_text('{"ok":true,"error_class":null}\n')
            return original_wait(container, timeout_s)

        runtime.wait = wait  # type: ignore[method-assign]

    def _handler(self, runtime: FakeRuntime, container: FakeContainer) -> int:
        self.release.setdefault(container.spec.run_id, threading.Event())
        self.started.release()
        return -1  # still running


def _background(sbx: Sbx) -> JobService:
    sbx.jobs.background = True
    return sbx.jobs


def _row(jobs: JobService, run_id: str) -> dict[str, Any]:
    return jobs.get(run_id)


@pytest.mark.parametrize("state", ["draining", "down"])
def test_new_jobs_refused_while_draining_or_down(sbx: Sbx, state: str) -> None:
    write_flag(sbx.settings.control_dir / "lifecycle", {"state": state, "scope": "distro", "reason": "t"})
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_fetch("r1", "c_01ABCDEFGH"))
    assert exc.value.status == 423 and exc.value.error_class == state


def test_kill_still_wins_over_lifecycle(sbx: Sbx) -> None:
    write_flag(sbx.settings.control_dir / "lifecycle", {"state": "down", "scope": "distro"})
    write_flag(sbx.settings.control_dir / "killed", {"level": "soft", "reason": "x"})
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_fetch("r1", "c_01ABCDEFGH"))
    assert exc.value.error_class == "killed"


def test_drain_lets_finishers_finish_and_interrupts_the_rest(sbx: Sbx) -> None:
    gate = Gate(sbx.runtime)
    jobs = _background(sbx)
    jobs.submit(_fetch("fast", "c_01ABCDEFGH"))
    assert gate.started.acquire(timeout=5)
    jobs.submit(_fetch("slow", "c_01ABCDEFGJ"))
    assert gate.started.acquire(timeout=5)
    assert jobs.running() == ["fast", "slow"]

    threading.Timer(0.2, gate.release["fast"].set).start()  # finishes inside the drain window
    result = jobs.drain(1)
    assert result["finished"] == 1 and result["interrupted"] == 1 and result["interrupted_run_ids"] == ["slow"]
    assert _row(jobs, "fast")["state"] == "succeeded"
    slow = _row(jobs, "slow")
    assert slow["state"] == "failed" and slow["error_class"] == INTERRUPTED and slow["artifact_dir"] is None
    assert jobs.running() == [] and sbx.runtime.containers == {}
    assert "ossbot-task-c_01abcdefgj" in sbx.runtime.volumes  # kept: the runner re-queues from here


def test_drain_while_killed_does_not_wait(sbx: Sbx) -> None:
    gate = Gate(sbx.runtime)
    jobs = _background(sbx)
    jobs.submit(_fetch("slow", "c_01ABCDEFGH"))
    assert gate.started.acquire(timeout=5)
    write_flag(sbx.settings.control_dir / "killed", {"level": "soft", "reason": "x"})
    started = time.monotonic()
    result = jobs.drain(60)
    assert time.monotonic() - started < 20
    assert result["killed"] is True and result["interrupted"] == 1


def test_admission_closed_during_drain_even_without_flag(sbx: Sbx) -> None:
    gate = Gate(sbx.runtime)
    jobs = _background(sbx)
    jobs.submit(_fetch("slow", "c_01ABCDEFGH"))
    assert gate.started.acquire(timeout=5)
    errors: list[ApiError] = []

    def late() -> None:
        time.sleep(0.2)
        try:
            jobs.submit(_fetch("late", "c_01ABCDEFGJ"))
        except ApiError as exc:
            errors.append(exc)

    t = threading.Thread(target=late)
    t.start()
    jobs.drain(0.5)
    t.join()
    assert errors and errors[0].error_class == "draining"


def test_recover_after_crash_interrupts_rows_and_removes_leftovers(sbx: Sbx) -> None:
    """A sandboxd that died mid-job (SIGKILL, power loss) left a 'running' row, its containers
    and per-run volumes. The next start marks the row interrupted first, then cleans up."""
    gate = Gate(sbx.runtime)
    jobs = _background(sbx)
    jobs.submit(_fetch("r9", "c_01ABCDEFGH"))
    assert gate.started.acquire(timeout=5)
    # the "new process": same database and Docker, fresh in-memory state
    fresh = JobService(
        store=jobs.store, runtime=sbx.runtime, pins=jobs.pins, catalog=jobs.catalog, gatekeeper=jobs.gatekeeper,
        scanner=jobs.scanner, clock=jobs.clock, settings=jobs.settings, trust_mode=jobs.trust_mode,
        networks=jobs.networks, background=False,
    )  # fmt: skip
    report = fresh.recover()
    assert report["interrupted"] == ["r9"] and report["containers_removed"] >= 1
    row = fresh.get("r9")
    assert row["state"] == "failed" and row["error_class"] == INTERRUPTED
    assert not any(c.spec.run_id == "r9" for c in sbx.runtime.containers.values())
    assert "ossbot-in-r9" not in sbx.runtime.volumes and "ossbot-out-r9" not in sbx.runtime.volumes
    assert "ossbot-task-c_01abcdefgh" in sbx.runtime.volumes
    assert fresh.recover() == {"interrupted": [], "containers_removed": 0}  # idempotent
    gate.abandoned.set()  # end the "dead" process's thread without it recording anything


async def test_drain_api(sbx: Sbx) -> None:
    import httpx

    from ossbot.sandboxd.app import PEER_KEY, create_app
    from ossbot.sandboxd.jobs import ProbeRunner

    app = create_app(sbx.jobs, ProbeRunner(sbx.jobs), frozenset({0}))

    async def with_peer(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await app({**scope, PEER_KEY: 0} if scope["type"] == "http" else scope, receive, send)

    transport = httpx.ASGITransport(app=with_peer)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://sandboxd") as c:
        assert (await c.get("/v1/health")).json() == {"ok": True, "running": 0, "running_run_ids": []}
        assert (await c.post("/v1/drain", json={"timeout_s": 0})).json()["interrupted"] == 0
        assert (await c.post("/v1/drain", json={"timeout_s": -1})).status_code == 422
