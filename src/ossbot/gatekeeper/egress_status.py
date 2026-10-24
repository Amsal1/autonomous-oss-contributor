"""What ``/status`` and ``/egress`` show about the floors (build-env design §9.2, ADR-0025): the floor detector
and the public-address floor. Read-only; nothing here gates anything.

* Floor detector: the gatekeeper never reaches sandboxd (only root and ``ossrunner`` can), so sandboxd's meter
  posts its ``floor_detector`` every heartbeat (``POST /v1/egress/health``, role sandbox). The last report is
  kept in memory; none, or one older than ``HEALTH_MAX_AGE_S``, reads as ``down`` (fail closed wording).
* Public floor: read from ``/etc/ossbot/proxy/floor/state.json`` (``root:root 0644``) with the same reader
  sandboxd's admission uses (``common.public_floor.read_state``: ``known`` recomputed, a count, never a value).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ossbot.common.control_flags import CODE_WEB_MODES
from ossbot.common.public_floor import FloorStatus, read_state
from ossbot.common.schemas import SandboxHealthReport

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

HEALTH_MAX_AGE_S = 120.0  # four missed meter heartbeats (30 s)
Detector = Literal["ok", "down"]


def record(ctx: AppContext, report: SandboxHealthReport) -> None:
    ctx.sandbox_health = (report, ctx.clock.now())


def floor_detector(ctx: AppContext) -> Detector:
    got = ctx.sandbox_health
    if got is None:
        return "down"
    report, at = got
    if (ctx.clock.now() - at).total_seconds() > HEALTH_MAX_AGE_S:
        return "down"
    return report.floor_detector


def public_floor(ctx: AppContext) -> FloorStatus:
    return read_state(ctx.paths.floor_dir, ctx.clock.now().timestamp())


def implement_network_state(value: str, mode: str, floor_known: bool = True) -> str:
    """``on_request (active)``, ``auto (inactive)``, ``off`` (ADR-0024/0025: effective only in sandbox_open and
    sandbox_full), ``auto (inactive, public floor unknown)`` while the public-address floor is unknown (build-env
    design §4.7: repo code's web waits then)."""
    if value == "off":
        return "off"
    if mode not in CODE_WEB_MODES:
        return f"{value} (inactive)"
    return f"{value} (active)" if floor_known else f"{value} (inactive, public floor unknown)"


def public_floor_text(ctx: AppContext) -> str:
    """``ok`` or ``unknown since HH:MM UTC, repo-code web paused`` (build-env design §9.2; never a value)."""
    status = public_floor(ctx)
    if status.known:
        return "ok"
    since = since_text(status)
    return f"unknown since {since} UTC, repo-code web paused" if since else "unknown, repo-code web paused"


def status_line(ctx: AppContext) -> str:
    """The /status egress line (build-env design §9.2)."""
    mode = ctx.control.egress_mode(ctx.config.egress.mode_default)
    impl = ctx.control.implement_network()
    state = implement_network_state(impl, mode, public_floor(ctx).known)
    return (f"🌐 Egress {mode} · implement network {state} · floor detector {floor_detector(ctx)} · public floor"
            f" {public_floor_text(ctx)}")  # fmt: skip


def since_text(status: FloorStatus) -> str | None:
    """``HH:MM`` (UTC, the fallback text of a Telegram time entity) of when the floor became unknown, or None
    when it never was known."""
    if status.since is None:
        return None
    return datetime.fromtimestamp(status.since, UTC).strftime("%H:%M")
