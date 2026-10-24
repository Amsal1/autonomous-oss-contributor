"""The profile x mode x strict x implement_network matrix of build-env design §2.3 (ADR-0024, ADR-0025), as sandboxd
decides it at admission: fetch/validate of a mode-egress repo are ``web`` exactly in the code-web modes, strict
repos stay ``base`` in every mode; agent-exec is networked only in a code-web mode, for a non-strict repo, when
the gatekeeper's implement_network decision says web; key-holding containers (agent-llm, research) gain nothing
in any mode; the meter's guard policy is ``notes`` only for web jobs of ``sandbox_full``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ossbot.common.config import EgressAnomaly
from ossbot.common.control_flags import CODE_WEB_MODES, EGRESS_MODES
from ossbot.sandboxd.egress_meter import EgressMeter
from ossbot.sandboxd.profiles import POOLS, JobSpec
from tests.unit.sandboxd.conftest import Sbx, pass_all_probes

pytestmark = pytest.mark.unit
CID = "c_01ABCDEFGH"
# the gatekeeper's per-run answer for each implement_network setting (ADR-0024 table: on_request without a
# grant and off are "none"; auto and on_request with a grant are "web" in a code-web mode)
IMPLEMENT = {"off": "none", "on_request_no_grant": "none", "on_request_grant": "web", "auto": "web"}


def _spec(sbx: Sbx, profile: str, egress: str | None, run_id: str = "r1") -> JobSpec:
    body: dict[str, Any] = {"run_id": run_id, "cid": CID, "profile": profile, "entrypoint": profile,
                            "repo_mode": "rw", "timeout_s": 600, "args": {}}  # fmt: skip
    if egress is not None:
        body["egress"] = egress
    if profile == "fetch":
        body["args"] = {"repo": "owner/repo"}
    elif profile == "validate":
        body["inputs"] = {"validate.json": sbx.runner_input("validate.json", b'{"commands": []}')}
    elif profile == "coder":
        body.update(
            {
                "entrypoint": "kiro",
                "inputs": {"code.md": sbx.runner_input("code.md", b"implement")},
                "args": {"role": "coder", "prompt_file": "code.md", "model": "claude-test", "effort": "high"},
            }
        )
    elif profile == "research":
        body.update(
            {
                "entrypoint": "kiro",
                "repo_mode": "snapshot",
                "inputs": {"plan.md": sbx.runner_input("plan.md", b"plan this")},
                "args": {"role": "planner", "prompt_file": "plan.md", "model": "claude-test", "effort": "high"},
            }
        )
    return JobSpec.model_validate(body)


@pytest.mark.parametrize("mode", EGRESS_MODES)
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("profile", ["fetch", "validate"])
def test_fetch_and_validate(sbx: Sbx, mode: str, strict: bool, profile: str) -> None:
    sbx.mode[0] = mode
    plan = sbx.jobs.admit(_spec(sbx, profile, "strict" if strict else "mode"))
    want = "web" if mode in CODE_WEB_MODES and not strict else "base"
    assert plan.net == want and plan.env["OSSBOT_NET"] == want
    assert plan.exec_network == "none" and plan.exec_env == {}
    assert plan.egress == ("strict" if strict else "mode")


@pytest.mark.parametrize("mode", EGRESS_MODES)
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("setting", sorted(IMPLEMENT))
def test_agent_exec(sbx: Sbx, mode: str, strict: bool, setting: str) -> None:
    pass_all_probes(sbx)
    sbx.mode[0] = mode
    sbx.gatekeeper.egress_answer = IMPLEMENT[setting]
    plan = sbx.jobs.admit(_spec(sbx, "coder", "strict" if strict else "mode"))
    want = "web" if mode in CODE_WEB_MODES and not strict and IMPLEMENT[setting] == "web" else "none"
    assert plan.exec_network == want
    assert plan.exec_env == ({"OSSBOT_EXEC_NETWORK": "web"} if want == "web" else {})
    assert plan.net == ("web" if want == "web" else "base")
    assert "OSSBOT_NET" not in plan.env  # agent-llm's env: unchanged by any mode (it holds the key)
    if strict or mode not in CODE_WEB_MODES:
        assert sbx.gatekeeper.egress_calls == []  # never even asked (two independent checks, fail closed)


@pytest.mark.parametrize("mode", EGRESS_MODES)
def test_key_holding_research_gains_nothing(sbx: Sbx, mode: str) -> None:
    pass_all_probes(sbx)
    sbx.mode[0] = mode
    plan = sbx.jobs.admit(_spec(sbx, "research", None))
    assert plan.net == "base" and plan.exec_network == "none" and "OSSBOT_NET" not in plan.env


def test_agent_llm_argv_is_identical_in_every_mode_and_answer(sbx: Sbx) -> None:
    """agent-llm (the Kiro key) keeps one shape: no mode or implement_network answer changes its network,
    mounts or env; only its sibling agent-exec gets the web."""
    pass_all_probes(sbx)
    shapes = set()
    for i, (mode, answer) in enumerate((m, a) for m in EGRESS_MODES for a in ("none", "web")):
        sbx.mode[0], sbx.gatekeeper.egress_answer = mode, answer
        run_id = f"r{i}"
        sbx.jobs.submit(_spec(sbx, "coder", "mode", run_id))
        argv = next(a for a in sbx.runtime.argvs if "--name" in a and a[a.index("--name") + 1] == f"ossbot-{run_id}")
        shapes.add(tuple(x.replace(run_id, "R") for x in argv))
    assert len(shapes) == 1


def test_pools_of_key_holders_are_unchanged() -> None:
    """The llm and research /28 pools are the ADR-0017 ones; agent-exec sits in pool_fetch, strict in pool_strict."""
    assert POOLS["exec"] == POOLS["fetch"] == POOLS["validate"] == "10.213.0.0/18"
    assert POOLS["fetch-strict"] == POOLS["validate-strict"] == "10.213.192.0/19"
    assert POOLS["llm"] == POOLS["catalog"] == "10.213.128.0/18" and POOLS["research"] == "10.213.64.0/18"


@pytest.mark.parametrize("mode", EGRESS_MODES)
@pytest.mark.parametrize("net", ["web", "base"])
def test_guard_policy_per_cell(tmp_path: Path, mode: str, net: str) -> None:
    log = tmp_path / "access.log"
    log.write_text("", encoding="utf-8")
    meter = EgressMeter(log, tmp_path / "runs", EgressAnomaly, sleep=lambda s: None, current_mode=lambda: mode)
    meter.register("r1", CID, "validate", "10.213.0.16/28", 0.0, "mode", mode, net)
    doc = meter.finish("r1")
    assert doc is not None and doc["net"] == net
    assert doc["policy"] == ("notes" if (mode, net) == ("sandbox_full", "web") else "hold")
