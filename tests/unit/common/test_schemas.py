"""Post-v1 open discovery API schemas (design §8.4, §11): bounds, extra=forbid, URL shapes, round-trip."""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError

from ossbot.common.config import TargetsConfig, discovered_target, parse_yaml_model, resolve_target
from ossbot.common.schemas import (
    DISCOVERY_URL_RE,
    DiscoveredDecision,
    DiscoveredFacts,
    DiscoveredView,
    DiscoveryRequest,
    ResolvedTargetView,
)
from tests.conftest import REPO_ROOT


def _facts(**kw: Any) -> dict[str, Any]:
    return {"repo": "argoproj/argo-cd", "source": "search", "status": "candidate", **kw}


def test_facts_minimal_and_full() -> None:
    f = DiscoveredFacts.model_validate(_facts())
    assert (f.kept_check, f.tried, f.skip_reason) == (False, False, None)
    full = DiscoveredFacts.model_validate(
        _facts(topic="gitops", stars=24440, language="Go", license_spdx="Apache-2.0",
               pushed_at="2026-10-20T08:00:00Z", ci_on_pr="yes", repo_score=81.5, tried=True)
    )  # fmt: skip
    assert full.stars == 24440 and full.ci_on_pr == "yes"


@pytest.mark.parametrize(
    "bad",
    [
        {"bogus": 1},
        {"status": "kept"},  # a worker may only send candidate or skipped
        {"status": "never"},
        {"source": "owner"},
        {"repo": "not a repo"},
        {"topic": "Bad_Topic"},
        {"stars": -1},
        {"stars": 10_000_001},
        {"language": "9lang"},
        {"license_spdx": "MIT<script>"},
        {"pushed_at": "yesterday"},
        {"ci_on_pr": "maybe"},
        {"repo_score": 201.0},
        {"skip_reason": "stars_below_min"},  # only with status skipped
        {"status": "skipped"},  # skip_reason missing
        {"status": "skipped", "skip_reason": "Stars"},
        {"status": "skipped", "skip_reason": "x" * 41},
        {"status": "skipped", "skip_reason": "egress_extra_needed:A.io"},
    ],
)
def test_facts_reject(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DiscoveredFacts.model_validate(_facts(**bad))


@pytest.mark.parametrize(
    "reason", ["stars_below_min", "no_contributing", "egress_extra_needed_strict:a.io,b.io", "license:busl-1.1"]
)
def test_skip_reason_pattern_accepts(reason: str) -> None:
    f = DiscoveredFacts.model_validate(_facts(status="skipped", skip_reason=reason))
    assert f.skip_reason == reason


def test_decision_schema() -> None:
    assert DiscoveredDecision.model_validate({"repo": "a/b", "decision": "unstrict"}).decision == "unstrict"
    for bad in ({"repo": "a/b", "decision": "approve"}, {"repo": "a/b", "decision": "keep", "x": 1}):
        with pytest.raises(ValidationError):
            DiscoveredDecision.model_validate(bad)


def test_view_rejects_extra() -> None:
    row = {"repo": "a/b", "source": "search", "status": "candidate", "first_seen": "t", "updated_at": "t"}
    assert DiscoveredView.model_validate(row).egress_strict is False
    with pytest.raises(ValidationError):
        DiscoveredView.model_validate({**row, "secret": 1})


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://github.com/argoproj/argo-cd", True),
        ("https://github.com/argoproj/argo-cd/", True),
        ("https://github.com/argoproj/argo-cd/issues/123", True),
        ("https://github.com/argoproj/argo-cd/issues/123/", True),
        ("https://github.com/argoproj/argo-cd/pull/123", False),
        ("https://github.com/argoproj/argo-cd/issues/0", False),
        ("https://github.com/argoproj/argo-cd/issues/123#issuecomment-1", False),
        ("https://github.com/argoproj/argo-cd/issues/123?x=1", False),
        ("https://github.com/argoproj/argo-cd/issues/123\n", False),
        ("https://github.com/argoproj", False),
        ("http://github.com/argoproj/argo-cd", False),
        ("https://github.com.evil.io/argoproj/argo-cd", False),
        ("https://github.com/argoproj/argo-cd/tree/main", False),
        ("https://github.com/a_b/c", False),
    ],
)
def test_discovery_url_shapes(url: str, ok: bool) -> None:
    if ok:
        assert DiscoveryRequest.model_validate({"url": url}).url == url
    else:
        with pytest.raises(ValidationError):
            DiscoveryRequest.model_validate({"url": url})


def test_discovery_url_groups() -> None:
    m = re.match(DISCOVERY_URL_RE, "https://github.com/fluxcd/flux2/issues/42")
    assert m is not None and (m["owner"], m["repo"], m["n"]) == ("fluxcd", "flux2", "42")
    m = re.match(DISCOVERY_URL_RE, "https://github.com/fluxcd/flux2")
    assert m is not None and m["n"] is None


def test_resolved_target_view_round_trips() -> None:
    targets = parse_yaml_model(TargetsConfig, "targets.yaml", (REPO_ROOT / "config" / "targets.yaml").read_bytes())
    for t in (
        resolve_target(targets, "langfuse/langfuse"),
        discovered_target(targets, "argoproj/argo-cd", kept=True, egress_strict=True),
    ):
        assert t is not None
        view = ResolvedTargetView.from_target(t)
        assert view.to_target() == t
        assert ResolvedTargetView.model_validate_json(view.model_dump_json()).to_target() == t
    with pytest.raises(ValidationError):
        ResolvedTargetView.model_validate({**view.model_dump(), "extra": 1})
