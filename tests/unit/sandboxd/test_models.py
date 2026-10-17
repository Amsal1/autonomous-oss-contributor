"""Model validation against the gatekeeper catalog (DESIGN §10.2, N6)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from ossbot.common.clock import FakeClock
from ossbot.sandboxd.jobs import ApiError
from ossbot.sandboxd.models import CatalogCache, UnknownModel
from ossbot.sandboxd.profiles import JobSpec
from tests.unit.sandboxd.conftest import Sbx, pass_all_probes


def _spec(sbx: Sbx, model: str) -> JobSpec:
    return JobSpec.model_validate(
        {
            "run_id": "r1",
            "cid": "c_01ABCDEFGH",
            "profile": "research",
            "entrypoint": "kiro",
            "args": {"role": "vetter", "prompt_file": "v.md", "model": model, "effort": "low"},
            "repo_mode": "none",
            "inputs": {"v.md": sbx.runner_input("v.md", b"x")},
            "timeout_s": 60,
        }
    )


def test_catalog_id_accepted(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    assert sbx.jobs.admit(_spec(sbx, "claude-test")).env["MODEL"] == "claude-test"


def test_forced_id_accepted(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    sbx.jobs.admit(_spec(sbx, "forced-x"))


def test_unknown_id_400(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    with pytest.raises(ApiError) as exc:
        sbx.jobs.admit(_spec(sbx, "claude-unknown"))
    assert (exc.value.status, exc.value.error_class) == (400, "unknown_model")


def test_catalog_cached_five_minutes() -> None:
    clock = FakeClock()
    rows: list[dict[str, Any]] = [{"model_id": "a"}]
    calls = []

    def fetch() -> list[dict[str, Any]]:
        calls.append(1)
        return list(rows)

    cache = CatalogCache(fetch, clock)
    cache.validate("a")
    rows.append({"model_id": "b"})
    with pytest.raises(UnknownModel):
        cache.validate("b")  # still cached
    clock.advance(timedelta(minutes=5))
    cache.validate("b")
    assert len(calls) == 2


def test_refresh_failure_fails_closed() -> None:
    clock = FakeClock()
    ok = [True]

    def fetch() -> list[dict[str, Any]]:
        if not ok[0]:
            raise OSError("gatekeeper down")
        return [{"model_id": "a"}]

    cache = CatalogCache(fetch, clock)
    cache.validate("a")
    ok[0] = False
    clock.advance(minutes=6)
    with pytest.raises(UnknownModel):
        cache.validate("a")
