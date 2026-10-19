"""Hermes tool surface per capability tier (DESIGN §15.1, §15.5, ADR-0018).

The rendered Hermes config, not a skill, defines the toolsets, so a skill can never grant tools above
the tier. ``ALWAYS_DISABLED`` stays in ``agent.disabled_toolsets`` in every tier. The gatekeeper's
``/v1/toolsets`` check (PLAN item 36) compares Hermes' reported surface with ``allowed_toolsets``;
a mismatch is a soft kill ``hermes_tool_surface``.
"""

from __future__ import annotations

from ossbot.common.control_flags import HermesTier

LOCKED: tuple[str, ...] = ("memory", "todo", "session_search", "ossbot")  # the §15.1 allowlist
# §15.5 [VERIFY], checked in PLAN item 33 (ADR-0002 addendum FEAT-007): Hermes v0.22.2 web tools use
# HTTP clients that honour HTTPS_PROXY, and the hermes uid can reach nothing but 10.212.0.2:3141 and lo
# (OSSBOT-OUT), so every Hermes-native web call goes through squid port 3141 or fails. ``web`` therefore
# joins ``research`` (and ``self_improve``). The live squid-log check is PLAN item 38.
HERMES_WEB_VIA_PROXY = True
ALWAYS_DISABLED: tuple[str, ...] = ("terminal", "code_execution", "file", "browser", "delegation", "cronjob")


def _research() -> tuple[str, ...]:
    return (*LOCKED, "web") if HERMES_WEB_VIA_PROXY else LOCKED


def allowed_toolsets(tier: HermesTier) -> tuple[str, ...]:
    """The toolsets added to ``platform_toolsets.api_server`` for ``tier``."""
    if tier == "locked":
        return LOCKED
    if tier == "research":
        return _research()
    return (*_research(), "skills")  # self_improve: skills behind the deterministic skill gate


def tier_toolsets() -> dict[HermesTier, tuple[str, ...]]:
    """``TIER_TOOLSETS`` as of now (``HERMES_WEB_VIA_PROXY`` is read at call time)."""
    return {tier: allowed_toolsets(tier) for tier in ("locked", "research", "self_improve")}


TIER_TOOLSETS: dict[HermesTier, tuple[str, ...]] = tier_toolsets()
