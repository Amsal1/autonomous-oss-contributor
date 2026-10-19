"""Hermes tool surface per capability tier (DESIGN §15.1, §15.5, ADR-0018).

The rendered Hermes config, not a skill, defines the toolsets, so a skill can never grant tools above
the tier. ``ALWAYS_DISABLED`` stays in ``agent.disabled_toolsets`` in every tier. ``check_toolsets``
compares Hermes' reported surface (``GET /v1/toolsets``) with the tier's allowlist; drift or an
unverifiable answer is a soft kill ``hermes_tool_surface`` (the relay gate in ``chat/backend.py``,
doctor and the root tier switch run it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

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

# The concrete tools each allowlisted toolset expands to at v0.22.2 (toolsets.py); ``ossbot`` is the
# plugin's ``ossbot_*`` tools. The Tool Search bridge is forbidden in every tier (M8).
TOOLSET_TOOLS: dict[str, frozenset[str]] = {
    "memory": frozenset({"memory"}),
    "todo": frozenset({"todo_list"}),
    "session_search": frozenset({"session_search"}),
    "skills": frozenset({"skills_list", "skill_view", "skill_manage"}),
    "web": frozenset({"web_search", "web_extract"}),
}
PLUGIN_TOOLSET = "ossbot"
PLUGIN_PREFIX = "ossbot_"
BRIDGE_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})
# §15.1: every built-in toolset outside the allowlist, listed in agent.disabled_toolsets; a tier
# removes from it only what it adds (web for research, skills for self_improve).
BUILTIN_DISABLED: tuple[str, ...] = (
    "terminal", "file", "code_execution", "delegation", "cronjob", "browser", "web", "search", "safe", "skills",
    "catalog", "kanban", "connections", "computer_use", "image_gen", "video_gen", "vision", "video", "tts",
    "clarify", "x_search", "desktop_ui", "project", "context_engine", "debugging", "coding", "discord",
    "discord_admin", "feishu_doc", "feishu_drive", "yuanbao",
)  # fmt: skip


def disabled_toolsets(tier: HermesTier) -> tuple[str, ...]:
    allowed = set(allowed_toolsets(tier))
    return tuple(t for t in BUILTIN_DISABLED if t not in allowed)


def allowed_tools(tier: HermesTier) -> frozenset[str]:
    """Built-in tool names allowed in ``tier`` (``ossbot_*`` plugin tools come on top)."""
    out: set[str] = set()
    for ts in allowed_toolsets(tier):
        out |= TOOLSET_TOOLS.get(ts, frozenset())
    return frozenset(out)


# ---------------------------------------------------------------------- runtime check (§15.1)
class ToolsetEntry(BaseModel):
    """One ``/v1/toolsets`` entry; documented additive fields are ignored (N7)."""

    model_config = ConfigDict(extra="ignore")
    name: StrictStr
    enabled: StrictBool
    tools: list[StrictStr]


class ToolsetsReply(BaseModel):
    """v0.22.2 wraps the entries: ``{"object": "list", "platform": "api_server", "data": [...]}``."""

    model_config = ConfigDict(extra="ignore")
    data: list[ToolsetEntry]


Outcome = Literal["ok", "drift", "unverifiable", "unavailable"]


@dataclass(frozen=True)
class SurfaceResult:
    outcome: Outcome
    detail: str = ""
    unexpected: tuple[str, ...] = ()

    @property
    def kill(self) -> bool:
        """Drift and an unverifiable answer disable the relay and soft-kill ``hermes_tool_surface``."""
        return self.outcome in ("drift", "unverifiable")


def evaluate_toolsets(payload: Any, tier: HermesTier) -> SurfaceResult:
    """Fail closed: the effective tools of every enabled toolset must be within the tier's allowlist."""
    try:
        entries = (
            ToolsetsReply.model_validate({"data": payload}) if isinstance(payload, list)
            else ToolsetsReply.model_validate(payload)
        ).data  # fmt: skip
    except ValidationError as exc:
        return SurfaceResult("unverifiable", f"schema mismatch: {exc.error_count()} error(s)")
    allowed_sets = set(allowed_toolsets(tier))
    tools_ok = allowed_tools(tier)
    unexpected: list[str] = []
    for entry in entries:
        if not entry.enabled:
            continue
        if entry.name not in allowed_sets:
            unexpected.append(f"toolset {entry.name}")
        for tool in entry.tools:
            plugin_tool = entry.name == PLUGIN_TOOLSET and tool.startswith(PLUGIN_PREFIX)
            if tool in BRIDGE_TOOLS or not (plugin_tool or tool in tools_ok):
                unexpected.append(tool)
    if unexpected:
        return SurfaceResult("drift", f"tier {tier}", tuple(dict.fromkeys(unexpected)))
    return SurfaceResult("ok", f"tier {tier}")


def check_toolsets(base_url: str, api_key: str, tier: HermesTier, *, timeout: float = 10.0) -> SurfaceResult:
    """``GET /v1/toolsets`` (bearer ``API_SERVER_KEY``): refused or timed out = only unavailable; a 404,
    any other status or non-JSON = unverifiable (the pinned version documents the endpoint)."""
    try:
        with httpx.Client(base_url=base_url, timeout=timeout, trust_env=False) as client:
            resp = client.get("/v1/toolsets", headers={"Authorization": f"Bearer {api_key}"})
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return SurfaceResult("unavailable", type(exc).__name__)
    except httpx.HTTPError as exc:
        return SurfaceResult("unavailable", type(exc).__name__)
    if resp.status_code != 200:
        return SurfaceResult("unverifiable", f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return SurfaceResult("unverifiable", "not JSON")
    return evaluate_toolsets(payload, tier)
