"""Hermes tool surface per capability tier (DESIGN §15.1, §15.5, ADR-0018).

The rendered Hermes config, not a skill, defines the toolsets, so a skill can never grant tools above
the tier. ``ALWAYS_DISABLED`` stays in ``agent.disabled_toolsets`` in every tier. The gatekeeper's
``/v1/toolsets`` check (PLAN item 36) compares Hermes' reported surface with ``allowed_toolsets``;
a mismatch is a soft kill ``hermes_tool_surface``.
"""

from __future__ import annotations

from ossbot.common.control_flags import HermesTier

LOCKED: tuple[str, ...] = ("memory", "todo", "session_search", "ossbot")  # the §15.1 allowlist
# §15.5 [VERIFY]: Hermes-native ``web`` tools join ``research`` only once PLAN item 33 confirms that the
# pinned Hermes routes them through HTTPS_PROXY (port 3141). Until then research == locked and the Kiro
# session's web_search/web_fetch stay the web path.
HERMES_WEB_VIA_PROXY = False
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
