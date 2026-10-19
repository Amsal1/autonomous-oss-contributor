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


def test_memory_stays_on_in_every_tier() -> None:
    for tier in HERMES_TIERS:
        assert "memory" in hs.allowed_toolsets(tier)
