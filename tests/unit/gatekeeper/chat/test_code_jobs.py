"""The assistant's code jobs, gatekeeper side (DESIGN §15.6; owner decision F007-R1-2): tier gate, limits
and queueing, kill/pause, the runner hand-off and the result caps, redaction and secret-file drop."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.control_flags import HermesTier
from ossbot.gatekeeper.chat import code_jobs
from ossbot.gatekeeper.chat.code_jobs import CodeJobs, CodeRefused, input_path
from ossbot.gatekeeper.chat.media import MediaStore
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_WORKER, write_flag
from tests.unit.gatekeeper.api_helpers import client

SECRET = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.artifacts = tmp / "artifacts"
        self.media = MediaStore(tmp / "chat")
        self.stop: str | None = None
        self.now = 1000.0
        self.wall = 5000.0
        self.jobs = CodeJobs(self.artifacts, self.media, redact=lambda t: t.replace(SECRET, "[REDACTED]"),
                             stopped=lambda: self.stop, now=lambda: self.now, wall=lambda: self.wall)  # fmt: skip

    def run_dir(self, run_id: str, result: dict[str, Any], files: dict[str, bytes] | None = None) -> None:
        d = self.artifacts / "sys-chat" / "runs" / run_id
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(result))
        for name, data in (files or {}).items():
            (d / name).write_bytes(data)


OK = {"ok": True, "error_class": None, "exit_code": 0, "timed_out": False, "stdout_bytes": 5, "stderr_bytes": 0,
      "files": [], "dropped": []}  # fmt: skip


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_submit_claim_finish(rig: Rig) -> None:
    att = rig.media.put("tg-1-1", "Photo 1.JPG", b"\xff\xd8jpeg")
    job = rig.jobs.submit("python", "print(1)", [att], 30)
    assert rig.jobs.view(job) == {"job_id": job.id, "status": "queued", "language": "python"}
    claimed = rig.jobs.claim()
    assert claimed is not None and claimed["kind"] == "code" and claimed["source"] == "main.py"
    assert base64.b64decode(claimed["code_b64"]) == b"print(1)" and claimed["timeout_s"] == 30
    name = f"{att}-photo_1.jpg"  # the id first: the path is known before the code is written
    assert claimed["files"] == [{"name": name, "b64": base64.b64encode(b"\xff\xd8jpeg").decode()}]
    assert input_path(att, "Photo 1.JPG") == f"/io/input/{name}"
    rig.run_dir("code-r1", OK, {"stdout.txt": b"1\n", "stderr.txt": b"", "out-plot.png": b"\x89PNG..."})
    assert rig.jobs.finish(job.id, run_id="code-r1", ok=True, error=None)
    view = rig.jobs.view(job)
    assert view["status"] == "done" and view["exit_code"] == 0 and view["stdout"] == "1\n"
    (out,) = view["files"]
    assert out["name"] == "plot.png" and out["bytes"] == 7 and rig.media.get(out["id"]) == ("plot.png", b"\x89PNG...")
    assert job.files == []  # inputs are dropped once the job ran
    assert not rig.jobs.finish(job.id, run_id="code-r1", ok=True, error=None)


def test_one_running_two_queued_and_twenty_per_hour(rig: Rig) -> None:
    first = rig.jobs.submit("bash", "echo 1", [], 10)
    assert rig.jobs.claim() is not None
    rig.jobs.submit("bash", "echo 2", [], 10)
    rig.jobs.submit("bash", "echo 3", [], 10)
    with pytest.raises(CodeRefused) as info:
        rig.jobs.submit("bash", "echo 4", [], 10)
    assert (info.value.status, info.value.reason) == (429, "queue_full")
    assert rig.jobs.claim() is None  # one running job at a time
    rig.jobs.finish(first.id, run_id=None, ok=False, error="boom")
    assert rig.jobs.claim() is not None
    hourly = Rig(rig.artifacts.parent / "h")
    for i in range(code_jobs.PER_HOUR):
        job = hourly.jobs.submit("bash", f"echo {i}", [], 5)
        hourly.jobs.claim()
        hourly.jobs.finish(job.id, run_id=None, ok=False, error="x")
    with pytest.raises(CodeRefused, match="rate_limited"):
        hourly.jobs.submit("bash", "echo late", [], 5)
    hourly.wall += 3601
    hourly.jobs.submit("bash", "echo next hour", [], 5)


@pytest.mark.parametrize(
    ("kw", "status"),
    [
        ({"code": "  "}, 400), ({"code": "x" * (code_jobs.CODE_MAX + 1)}, 400), ({"timeout_s": 121}, 400),
        ({"timeout_s": 0}, 400), ({"files": ["a_0000000000000000"]}, 404),
        ({"files": [f"a_{i:016x}" for i in range(6)]}, 400),
    ],
)  # fmt: skip
def test_bad_requests(rig: Rig, kw: dict[str, Any], status: int) -> None:
    args: dict[str, Any] = {"language": "python", "code": "print(1)", "attachment_ids": [], "timeout_s": 10}
    args.update({"attachment_ids" if k == "files" else k: v for k, v in kw.items()})
    with pytest.raises(CodeRefused) as info:
        rig.jobs.submit(**args)
    assert info.value.status == status


def test_kill_cancels_queued_jobs_and_blocks_new_ones(rig: Rig) -> None:
    running = rig.jobs.submit("bash", "sleep 60", [], 60)
    rig.jobs.claim()
    queued = rig.jobs.submit("bash", "echo later", [], 5)
    rig.stop = "killed"
    assert rig.jobs.claim() is None
    got = rig.jobs.get(queued.id)
    assert got is not None and got.status == "cancelled" and got.error == "stopped: killed"
    with pytest.raises(CodeRefused) as info:
        rig.jobs.submit("bash", "echo", [], 5)
    assert info.value.status == 423
    # the runner's check_running stops the running job; its failure report still lands
    assert rig.jobs.finish(running.id, run_id=None, ok=False, error="sandbox: Interrupted")
    assert rig.jobs.view(running)["status"] == "failed"


def test_a_lost_runner_fails_the_job(rig: Rig) -> None:
    job = rig.jobs.submit("bash", "echo", [], 10)
    rig.jobs.claim()
    rig.now += 10 + code_jobs.CLAIM_GRACE_S + 1
    got = rig.jobs.get(job.id)
    assert got is not None and got.status == "failed" and got.error == "runner_lost"


def test_result_caps_redaction_and_secret_files(rig: Rig) -> None:
    job = rig.jobs.submit("python", "print()", [], 10)
    rig.jobs.claim()
    files = {f"out-f{i}.csv": b"a,b\n" for i in range(7)}
    files |= {"out-big.png": b"\x00" * (code_jobs.FILES_OUT_TOTAL + 1), "out-run.exe": b"MZ",
              "out-leak.txt": f"token {SECRET}".encode(), "stdout.txt": f"x {SECRET} ".encode() + b"y" * 40000,
              "stderr.txt": b"warn"}  # fmt: skip
    rig.run_dir("code-r2", {**OK, "stdout_bytes": 90000}, files)
    rig.jobs.finish(job.id, run_id="code-r2", ok=True, error=None)
    view = rig.jobs.view(job)
    assert SECRET not in view["stdout"] and "[REDACTED]" in view["stdout"] and view["stdout_truncated"] is True
    assert len(view["stdout"].encode()) <= code_jobs.STREAM_MAX + 16 and view["stderr"] == "warn"
    assert len(view["files"]) == code_jobs.FILES_OUT_MAX
    assert "run.exe" in view["dropped"] and "leak.txt (secret)" in view["dropped"] and "big.png" in view["dropped"]
    assert all(rig.media.get(f["id"]) is not None for f in view["files"])


def test_failed_entrypoint_and_unreadable_results(rig: Rig) -> None:
    job = rig.jobs.submit("python", "print()", [], 10)
    rig.jobs.claim()
    rig.run_dir("code-r3", {"ok": False, "error_class": "no_source"})
    rig.jobs.finish(job.id, run_id="code-r3", ok=True, error=None)
    assert rig.jobs.view(job)["error"] == "no_source"
    other = rig.jobs.submit("python", "print()", [], 10)
    rig.jobs.claim()
    rig.jobs.finish(other.id, run_id="code-missing", ok=True, error=None)
    assert rig.jobs.view(other)["status"] == "failed" and "result_unreadable" in rig.jobs.view(other)["error"]


# ------------------------------------------------------------------ API: roles and the tier gate
def _tier(ctx: AppContext, tier: HermesTier) -> None:
    assert ctx.chat is not None
    ctx.chat.tier = lambda: tier


BODY = {"kind": "code", "language": "python", "code": "print(2 + 2)", "timeout_s": 20}


@pytest.mark.parametrize("tier", ["locked", "research"])
async def test_refused_below_self_improve(ctx: AppContext, tier: HermesTier) -> None:
    _tier(ctx, tier)
    async with client(ctx, UID_REQUESTER) as c:
        resp = await c.post("/v1/chat/sandbox-jobs", json=BODY)
    assert resp.status_code == 403 and resp.json()["error"] == "tier"


async def test_hermes_submits_and_polls_the_runner_claims(ctx: AppContext) -> None:
    _tier(ctx, "self_improve")
    async with client(ctx, UID_REQUESTER) as c:
        resp = await c.post("/v1/chat/sandbox-jobs", json=BODY)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        assert (await c.get(f"/v1/chat/sandbox-jobs/{job_id}")).json()["status"] == "queued"
        assert (await c.post("/v1/chat/jobs/claim")).status_code == 403
        bad = {**BODY, "env": {"X": "1"}}
        assert (await c.post("/v1/chat/sandbox-jobs", json=bad)).status_code == 400  # extra=forbid
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/chat/sandbox-jobs", json=BODY)).status_code == 403
        claimed = (await c.post("/v1/chat/jobs/claim")).json()["job"]
        assert claimed["kind"] == "code" and claimed["job_id"] == job_id
        done = await c.post(f"/v1/chat/jobs/{job_id}/result", json={"run_id": None, "ok": False, "error": "x"})
        assert done.status_code == 200
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.get(f"/v1/chat/sandbox-jobs/{job_id}")).status_code == 403
    rows = ctx.conn.execute("SELECT event FROM audit_events WHERE event LIKE 'chat_code_job%' ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == ["chat_code_job_submitted", "chat_code_job"]


async def test_api_refuses_while_killed(ctx: AppContext) -> None:
    _tier(ctx, "self_improve")
    write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x", "actor": "t", "ts": "2026-10-19T00:00:00Z"})
    async with client(ctx, UID_REQUESTER) as c:
        resp = await c.post("/v1/chat/sandbox-jobs", json=BODY)
    assert resp.status_code == 423
