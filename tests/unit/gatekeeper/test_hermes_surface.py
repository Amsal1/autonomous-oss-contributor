"""Hermes toolsets per capability tier (DESIGN §15.5, ADR-0018; PLAN item 23a)."""

from __future__ import annotations

import pytest

from ossbot.common.control_flags import HERMES_TIERS, HermesTier
from ossbot.gatekeeper.chat import hermes_surface as hs


@pytest.mark.parametrize("tier", HERMES_TIERS)
def test_always_disabled_never_allowed(tier: HermesTier) -> None:
    assert not set(hs.allowed_toolsets(tier)) & set(hs.ALWAYS_DISABLED)
    assert set(hs.ALWAYS_DISABLED) == {"terminal", "code_execution", "file", "browser", "delegation", "cronjob"}


def test_skills_only_in_self_improve() -> None:
    assert "skills" in hs.allowed_toolsets("self_improve")
    assert "skills" not in hs.allowed_toolsets("research") and "skills" not in hs.allowed_toolsets("locked")


def test_research_equals_locked_while_web_routing_is_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hs, "HERMES_WEB_VIA_PROXY", False)
    assert hs.allowed_toolsets("locked") == ("memory", "todo", "session_search", "ossbot")
    assert hs.allowed_toolsets("research") == hs.allowed_toolsets("locked")
    assert hs.allowed_toolsets("self_improve") == (*hs.LOCKED, "skills")


def test_research_adds_web_now_that_routing_is_confirmed() -> None:
    assert hs.HERMES_WEB_VIA_PROXY is True  # PLAN item 33 (ADR-0002 addendum FEAT-007)
    assert hs.allowed_toolsets("research") == (*hs.LOCKED, "web")
    assert hs.TIER_TOOLSETS["self_improve"] == (*hs.LOCKED, "web", "skills")
    assert "web" not in hs.allowed_toolsets("locked")


def _entry(name: str, tools: list[str], enabled: bool = True, **extra: object) -> dict[str, object]:
    return {"name": name, "enabled": enabled, "tools": tools, **extra}


ALLOWED = [
    _entry("memory", ["memory"]),
    _entry("todo", ["todo_list"]),
    _entry("session_search", ["session_search"]),
    _entry("ossbot", ["ossbot_status", "ossbot_kill"]),
    _entry("terminal", ["terminal"], enabled=False),
]


def _wrap(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"object": "list", "platform": "api_server", "data": entries}  # the v0.22.2 envelope


@pytest.mark.parametrize(
    "extra",
    [
        [_entry("terminal", ["terminal"])],
        [_entry("file", ["read_file"])],
        [_entry("memory", ["memory", "tool_search", "tool_describe", "tool_call"])],
        [_entry("ossbot", ["read_file"])],
        [_entry("browser", [])],
    ],
)
def test_drift_kills(extra: list[dict[str, object]]) -> None:
    res = hs.evaluate_toolsets(_wrap([*ALLOWED, *extra]), "self_improve")
    assert res.outcome == "drift" and res.kill


def test_allowlisted_set_passes_with_additive_fields() -> None:
    assert hs.evaluate_toolsets(_wrap(ALLOWED), "locked").outcome == "ok"
    labelled = [{**e, "label": "x", "description": "y", "configured": True} for e in ALLOWED]
    assert hs.evaluate_toolsets(_wrap(labelled), "locked").outcome == "ok"  # N7
    assert hs.evaluate_toolsets(ALLOWED, "locked").outcome == "ok"  # the documented bare list too


def test_tier_decides_skills_and_web() -> None:
    skills = _entry("skills", ["skills_list", "skill_view", "skill_manage"])
    web = _entry("web", ["web_search", "web_extract"])
    assert hs.evaluate_toolsets(_wrap([*ALLOWED, skills]), "research").outcome == "drift"
    assert hs.evaluate_toolsets(_wrap([*ALLOWED, skills, web]), "self_improve").outcome == "ok"
    assert hs.evaluate_toolsets(_wrap([*ALLOWED, web]), "locked").outcome == "drift"


@pytest.mark.parametrize(
    "payload", [{"data": [{"name": "memory"}]}, {"data": [{"name": 1, "enabled": "yes", "tools": []}]}, "nope", {}]
)
def test_schema_mismatch_is_unverifiable(payload: object) -> None:
    res = hs.evaluate_toolsets(payload, "locked")
    assert res.outcome == "unverifiable" and res.kill


def test_http_outcomes() -> None:
    import httpx
    import respx

    url = "http://127.0.0.1:8642/v1/toolsets"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).respond(404)
        assert hs.check_toolsets("http://127.0.0.1:8642", "k", "locked").outcome == "unverifiable"
        mock.get(url).respond(200, text="<html>")
        assert hs.check_toolsets("http://127.0.0.1:8642", "k", "locked").outcome == "unverifiable"
        mock.get(url).mock(side_effect=httpx.ConnectError("refused"))
        res = hs.check_toolsets("http://127.0.0.1:8642", "k", "locked")
        assert res.outcome == "unavailable" and not res.kill
        route = mock.get(url).respond(200, json=_wrap(ALLOWED))
        assert hs.check_toolsets("http://127.0.0.1:8642", "secret-k", "locked").outcome == "ok"
        assert route.calls.last.request.headers["authorization"] == "Bearer secret-k"


def test_disabled_list_drops_only_what_the_tier_adds() -> None:
    assert "web" in hs.disabled_toolsets("locked") and "web" not in hs.disabled_toolsets("research")
    assert "skills" in hs.disabled_toolsets("research") and "skills" not in hs.disabled_toolsets("self_improve")
    for tier in HERMES_TIERS:
        assert set(hs.ALWAYS_DISABLED) <= set(hs.disabled_toolsets(tier))


def test_memory_stays_on_in_every_tier() -> None:
    for tier in HERMES_TIERS:
        assert "memory" in hs.allowed_toolsets(tier)
