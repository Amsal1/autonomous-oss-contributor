"""Runner error mapping of sandboxd's ADR-0025 answers (build-env design §4.7, §5.1): a 503 or a job error
``public_floor_unknown`` is ``PublicFloorUnknown`` (a Deferred, never Interrupted and never a stage failure,
checked before the generic 423/503 branch); a job stopped with ``egress_floor`` is ``ContributionHeld`` like an
egress anomaly; a scheduler tick defers the stage and retries it after ``RETRY_S``."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.runner.errors import (
    ContributionHeld,
    Deferred,
    Interrupted,
    PublicFloorUnknown,
    StageFailed,
    sandbox_run,
)
from ossbot.runner.sbx_client import SbxError

pytestmark = pytest.mark.unit


class Refusing:
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code

    def run(self, spec: dict[str, Any], *, tick: Any = None) -> dict[str, Any]:
        raise SbxError(self.status, self.code)


class Done:
    def __init__(self, error_class: str) -> None:
        self.error_class = error_class

    def run(self, spec: dict[str, Any], *, tick: Any = None) -> dict[str, Any]:
        return {"state": "failed", "error_class": self.error_class, "run_id": spec["run_id"]}


def test_the_503_is_deferred_not_interrupted() -> None:
    with pytest.raises(PublicFloorUnknown) as info:
        sandbox_run(Refusing(503, "public_floor_unknown"), {"run_id": "r"}, None)
    assert isinstance(info.value, Deferred) and not isinstance(info.value, (Interrupted, StageFailed))
    assert PublicFloorUnknown.RETRY_S == 300
    with pytest.raises(Interrupted):  # any other 503 (sandboxd restarting) is unchanged
        sandbox_run(Refusing(503, "draining"), {"run_id": "r"}, None)


def test_the_job_error_is_deferred_too() -> None:
    with pytest.raises(PublicFloorUnknown):
        sandbox_run(Done("public_floor_unknown"), {"run_id": "r"}, None)


def test_a_floor_stop_is_a_hold_like_an_anomaly() -> None:
    for error in ("egress_floor", "egress_anomaly"):
        with pytest.raises(ContributionHeld):
            sandbox_run(Done(error), {"run_id": "r"}, None)
    assert sandbox_run(Done("timeout"), {"run_id": "r"}, None)["error_class"] == "timeout"
