"""Live: a scout-profile Kiro job lists and reads ``/work/repo`` (DESIGN §9.5a; ADR-0011 addendum
"scout repo read"). Needs root, the live sandboxd and the sandbox Kiro key, and all safety probes passed
for the current tuple (``ossctl doctor --probes``), so it runs at close-out.

The task volume is the baked probe fixture (created through the suite's own key-free JobService and the
Docker filter); the live sandboxd snapshots it read-only for the research container, like any scout job.
Synthetic cid, no gatekeeper row (like ``ossctl kiro smoke``). The key-leak check uses the key-shape
pattern only (``probes.KSK_RE``); this test never reads a secret."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from ossbot.common.models import LIVE_TEST_ROLE, Catalog, ModelsFile
from ossbot.common.paths import Paths
from ossbot.common.uds import uds_client
from ossbot.runner import output_parse
from ossbot.sandboxd.probes import KSK_RE
from tests.sandbox.conftest import Sandbox
from tests.sandbox.test_profiles import LIVE_SANDBOXD, live_probes

CID = "c_SCOUTLIST1"
GATEKEEPER = Path("/run/ossbot/gatekeeper.sock")
PROMPT = Path(__file__).resolve().parents[1] / "fixtures" / "scout_list_prompt.md"
TIMEOUT_S = 600


class Listing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[str]
    readme_first_line: str


def _model() -> tuple[str, str]:
    with uds_client(GATEKEEPER, timeout=httpx.Timeout(10.0)) as client:
        resp = client.get("/v1/models/catalog")
    assert resp.status_code == 200, resp.text
    models = ModelsFile(Paths.default().config_dir / "models.yaml")
    resolved = models.resolve(LIVE_TEST_ROLE, Catalog.from_rows(resp.json()))
    return resolved.model, resolved.effort


def _stage_prompt(run_id: str) -> Path:
    """Under ``runner/inputs/smoke/<run_id>`` with the group and modes ``ossctl kiro smoke`` uses."""
    inputs = Paths.default().srv / "runner" / "inputs" / "smoke" / run_id
    inputs.mkdir(parents=True, mode=0o750)
    prompt = inputs / "scout-task.md"
    prompt.write_text(PROMPT.read_text(encoding="utf-8"), encoding="utf-8")
    for p in (inputs.parent, inputs, prompt):
        os.chown(p, -1, inputs.parent.parent.stat().st_gid)
    prompt.chmod(0o640)
    return prompt


def _job(spec: dict[str, Any]) -> dict[str, Any]:
    with uds_client(LIVE_SANDBOXD, timeout=httpx.Timeout(30.0, connect=3.0)) as client:
        resp = client.post("/v1/jobs", json=spec)
        assert resp.status_code in (200, 201, 202), resp.text
        job: dict[str, Any] = resp.json()
        started = time.monotonic()
        while job.get("state") not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() - started < TIMEOUT_S + 60, f"job {spec['run_id']} did not finish"
            time.sleep(2.0)
            job = client.get(f"/v1/jobs/{spec['run_id']}").json()
    return job


def test_scout_lists_and_reads_the_snapshot(sandbox: Sandbox) -> None:
    assert live_probes()["all_passed"] is True, "safety probes not passed: ossctl doctor --probes"
    model, effort = _model()
    sandbox.fixture_task_volume(CID)
    try:
        run_id = f"smoke-{secrets.token_hex(6)}"
        prompt = _stage_prompt(run_id)
        spec = {"run_id": run_id, "cid": CID, "profile": "research", "entrypoint": "kiro", "repo_mode": "snapshot",
                "args": {"role": "scout", "prompt_file": prompt.name, "model": model, "effort": effort},
                "inputs": {prompt.name: str(prompt)}, "timeout_s": TIMEOUT_S}  # fmt: skip
        job = _job(spec)
        assert job.get("state") == "succeeded", job
        art = Path(str(job["artifact_dir"]))
        events = (art / "scout.events.jsonl").read_bytes()
        stderr = (art / "scout.stderr").read_bytes() if (art / "scout.stderr").is_file() else b""
        reads, _ = output_parse.repo_inspection(events)
        assert reads >= 2, reads  # the listing of /work/repo and the README read
        listed = [json.loads(ln) for ln in events.splitlines() if b'"tool_call' in ln]
        roots = [u for e in listed if isinstance(u := (e.get("data") or {}).get("update"), dict)
                 and (u.get("rawInput") or {}).get("path") == "/work/repo"]  # fmt: skip
        assert roots, "no tool call on /work/repo itself"
        out = output_parse.read_events(events, stderr)
        answer = output_parse.parse_model(out.text, Listing)
        assert "README.md" in answer.entries, answer
        assert not KSK_RE.search(events) and not KSK_RE.search(stderr)
    finally:
        sandbox.cleanup(CID)
