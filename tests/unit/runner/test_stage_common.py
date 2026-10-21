"""``stages.common.target`` (post-v1 open discovery, design §8.5): a config target resolves locally with
no gatekeeper call; a discovered repo through ``GET /v1/targets/resolve``; anything else fails the stage
as ``not_a_target``; an unreachable gatekeeper keeps the ``GkError(503)`` path."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.common.config import OPEN_CATEGORY, OPEN_PRIORITY
from ossbot.gatekeeper.context import AppContext
from ossbot.runner.gk_client import GkError
from ossbot.runner.llm import StageFailed
from ossbot.runner.pipeline import StageContext
from ossbot.runner.stages.common import target
from tests.unit.gatekeeper.test_discovered_targets import WORKER, facts

REPO = "openfaas/faas"  # not in config/targets.yaml


def spy(sc: StageContext, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = sc.gk.resolve_target

    def wrapped(repo: str) -> dict[str, Any]:
        calls.append(repo)
        return real(repo)

    monkeypatch.setattr(sc.gk, "resolve_target", wrapped)
    return calls


def test_a_config_target_needs_no_call(sc: StageContext, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = spy(sc, monkeypatch)
    got = target(sc, "langfuse/langfuse")
    assert got.repo == "langfuse/langfuse" and got.category != OPEN_CATEGORY and calls == []


def test_a_discovered_repo_resolves_through_the_gatekeeper(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.discovered.record(facts(REPO), WORKER)
    calls = spy(sc, monkeypatch)
    got = target(sc, REPO)
    assert calls == [REPO]
    assert (got.repo, got.category, got.priority, got.egress) == (REPO, OPEN_CATEGORY, OPEN_PRIORITY, "mode")
    defaults = ctx.config.targets.defaults
    assert got.labels == tuple(defaults.labels) and got.cla_signed is defaults.cla_signed


def test_an_unknown_repo_fails_as_not_a_target(sc: StageContext) -> None:
    with pytest.raises(StageFailed) as info:
        target(sc, "nobody/nothing")
    assert info.value.error_class == "not_a_target"


def test_an_unreachable_gatekeeper_keeps_the_503_path(sc: StageContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(repo: str) -> dict[str, Any]:
        raise GkError(503, "gatekeeper_unreachable")

    monkeypatch.setattr(sc.gk, "resolve_target", down)
    with pytest.raises(GkError) as info:
        target(sc, REPO)
    assert info.value.status == 503
