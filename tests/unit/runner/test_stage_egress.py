"""Every fetch/validate job carries its target's egress decision (open-discovery design §12.3, fail closed).

``job()`` refuses a fetch/validate job without ``egress`` (``StageFailed("egress_required")``, nothing
submitted); each call site (``fetch.run``, the scout depth-1 clone, ``validate.run``) sends
``target(sc, upstream).egress``: ``strict`` for a strict discovered repo and a strict config target,
``mode`` for a default target; a failed target lookup submits no job.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.config import ResolvedTarget, TargetsConfig, resolve_target
from ossbot.common.schemas import State
from ossbot.gatekeeper.context import AppContext
from ossbot.runner.errors import StageFailed
from ossbot.runner.pipeline import StageContext
from ossbot.runner.stages import common, fetch, scout, validate
from tests.conftest import insert_contribution
from tests.fakes.fake_github import FakeGitHub
from tests.unit.gatekeeper.test_proposals import sample
from tests.unit.runner.conftest import FakeSbx, fenced

pytestmark = pytest.mark.unit

CONFIG_REPO = "langfuse/langfuse"
DISCOVERED_REPO = "acme/tool"
STAGES = ("fetch", "scout", "validate")


def _validation(spec: dict[str, Any], out: Path) -> int:
    (out / "validation.json").write_text(json.dumps({"commands": []}))
    (out / "logs").mkdir(exist_ok=True)
    (out / "logs" / "ossbot-diff.log").write_text("")
    return 0


def _discovered(ctx: AppContext, repo: str, *, strict: bool) -> None:
    now = "2026-10-21T00:00:00Z"
    ctx.conn.execute(
        "INSERT INTO discovered_targets (repo, source, status, egress_strict, first_seen, updated_at)"
        " VALUES (?, 'owner', 'kept', ?, ?, ?)",
        (repo, int(strict), now, now),
    )
    ctx.conn.commit()


def _run(stage: str, sc: StageContext, ctx: AppContext, sbx: FakeSbx, repo: str) -> None:
    if stage == "fetch":
        cid = insert_contribution(ctx, State.FETCHING, upstream=repo)
        fetch.run(sc, sc.gk.contribution(cid))
    elif stage == "scout":
        sbx.kiro("scout", fenced(sample()))
        cid = insert_contribution(ctx, State.VETTED, upstream=repo)
        scout.run(sc, sc.gk.contribution(cid))
    else:
        sbx.handlers["validate"] = _validation
        cid = insert_contribution(ctx, State.VALIDATING, upstream=repo, base_sha="1" * 40)
        validate.run(sc, sc.gk.contribution(cid))


def _egress_of(sbx: FakeSbx) -> list[Any]:
    return [j.get("egress", "<absent>") for j in sbx.jobs if j["profile"] in {"fetch", "validate"}]


@pytest.mark.parametrize("profile", ["fetch", "validate"])
def test_job_without_egress_fails_the_stage_and_submits_nothing(sc: StageContext, sbx: FakeSbx, profile: str) -> None:
    with pytest.raises(StageFailed) as info:
        common.job(sc, "c_01ABCDEFGH", profile, profile, profile, {}, "rw")
    assert info.value.error_class == "egress_required"
    assert sbx.jobs == [] and sbx.calls == []


def test_egress_is_sent_only_when_given(sc: StageContext, sbx: FakeSbx) -> None:
    common.job(sc, "c_01ABCDEFGH", "fetch", "fetch", "fetch", {"repo": CONFIG_REPO}, "rw", egress="strict")
    assert sbx.jobs[-1]["egress"] == "strict"
    common.job(sc, "c_01ABCDEFGH", "package", "package", "package", {}, "rw")
    assert "egress" not in sbx.jobs[-1]


@pytest.mark.parametrize("stage", STAGES)
def test_default_target_sends_mode(sc: StageContext, ctx: AppContext, sbx: FakeSbx, stage: str) -> None:
    _run(stage, sc, ctx, sbx, CONFIG_REPO)
    assert _egress_of(sbx) == ["mode"]


@pytest.mark.parametrize("stage", STAGES)
def test_strict_config_target_sends_strict(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def strict(targets: TargetsConfig, repo: str) -> ResolvedTarget | None:
        found = resolve_target(targets, repo)
        return replace(found, egress="strict") if found is not None else None

    monkeypatch.setattr(common, "resolve_target", strict)  # the runner's view of `egress: strict` in targets.yaml
    _run(stage, sc, ctx, sbx, CONFIG_REPO)
    assert _egress_of(sbx) == ["strict"]


@pytest.mark.parametrize(("stage", "strict"), [(s, flag) for s in STAGES for flag in (True, False)])
def test_discovered_target_sends_its_egress(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx, github: FakeGitHub, stage: str, strict: bool
) -> None:
    """A DB-kept repo resolves through GET /v1/targets/resolve; ossctl targets strict sets egress_strict."""
    github.add_repo(DISCOVERED_REPO, default_branch="main", language="Go")
    _discovered(ctx, DISCOVERED_REPO, strict=strict)
    _run(stage, sc, ctx, sbx, DISCOVERED_REPO)
    assert _egress_of(sbx) == ["strict" if strict else "mode"]


@pytest.mark.parametrize("stage", STAGES)
def test_failed_target_lookup_submits_no_job(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx, github: FakeGitHub, stage: str
) -> None:
    github.add_repo("nobody/nothing", default_branch="main")
    with pytest.raises(StageFailed) as info:
        _run(stage, sc, ctx, sbx, "nobody/nothing")
    assert info.value.error_class == "not_a_target"
    assert _egress_of(sbx) == []
