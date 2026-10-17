"""Wiring of the gatekeeper services (one SQLite connection, used from the event-loop thread)."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ossbot.common.clock import Clock, SystemClock
from ossbot.common.config import ConfigBundle, Limits, ProfileConfig
from ossbot.common.paths import Paths
from ossbot.gatekeeper import db
from ossbot.gatekeeper.anchors import AnchorClient
from ossbot.gatekeeper.anomaly import AnomalyHandler
from ossbot.gatekeeper.approvals import ApprovalService, ArtifactSource
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.authz import Role, interactive_ancestry
from ossbot.gatekeeper.config_snapshot import SnapshotManager
from ossbot.gatekeeper.contributions import ContributionService
from ossbot.gatekeeper.control import ControlService
from ossbot.gatekeeper.models_admin import ModelsAdmin
from ossbot.gatekeeper.outbox import Outbox
from ossbot.gatekeeper.state_machine import StateMachine

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    paths: Paths
    clock: Clock
    conn: sqlite3.Connection
    roles: Mapping[int, Role]
    audit: AuditLog
    outbox: Outbox
    approvals: ApprovalService
    control: ControlService
    snapshots: SnapshotManager
    state_machine: StateMachine
    contributions: ContributionService
    anomaly: AnomalyHandler
    models: ModelsAdmin
    anchors: AnchorClient
    kiro_key_sha256: str | None = None
    on_approval_created: Callable[[str], None] = field(default=lambda approval_id: None)

    @property
    def config(self) -> ConfigBundle:
        return self.snapshots.current.bundle

    @property
    def telegram_chat_id(self) -> int:
        return self.config.profile.telegram.user_id

    def notify(self, text: str) -> None:
        """FYI/alert to the owner's private chat via the outbox (dropped while Telegram is unset)."""
        try:
            chat_id = self.telegram_chat_id
        except RuntimeError:
            chat_id = 0
        if chat_id == 0:
            log.warning(
                "telegram not configured; notification not queued: %s",
                text,
                extra={"event": "notify_dropped"},
            )
            return
        self.outbox.enqueue("sendMessage", {"chat_id": chat_id, "text": text})

    def operator_fyi(self, action: str, uid: int, pid: int) -> None:
        """Every operator 'go' action is announced on Telegram (§12.2, T7)."""
        when = self.clock.now().strftime("%H:%M")
        interactive = interactive_ancestry(pid)
        suffix = "" if interactive else " (interactive: false)"
        self.notify(f"⚠️ Local operator action: {action} by uid {uid} at {when}{suffix}")


def build_context(
    paths: Paths,
    roles: Mapping[int, Role],
    *,
    clock: Clock | None = None,
    conn: sqlite3.Connection | None = None,
    artifacts: ArtifactSource | None = None,
) -> AppContext:
    """Create every service. The config snapshot is loaded separately (``snapshots.load_at_start``)."""
    clock = clock or SystemClock()
    if conn is None:
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(paths.db_path)
    db.migrate(conn)
    audit = AuditLog(conn, clock)
    outbox = Outbox(conn, clock)
    holder: dict[str, SnapshotManager] = {}

    def limits() -> Limits:
        return holder["snap"].current.bundle.policy.limits

    def profile() -> ProfileConfig:
        return holder["snap"].current.bundle.profile

    ctx_ref: dict[str, AppContext] = {}

    def notify(text: str) -> None:
        if "ctx" in ctx_ref:
            ctx_ref["ctx"].notify(text)

    approvals = ApprovalService(conn, clock, audit, paths.control_dir, limits, profile, artifacts)
    control = ControlService(conn, paths, clock, audit, approvals, notify)
    snapshots = SnapshotManager(conn, paths, clock, audit, control, approvals, notify)
    holder["snap"] = snapshots
    ctx = AppContext(
        paths=paths,
        clock=clock,
        conn=conn,
        roles=roles,
        audit=audit,
        outbox=outbox,
        approvals=approvals,
        control=control,
        snapshots=snapshots,
        state_machine=StateMachine(conn, clock, audit, approvals, control, limits, notify),
        contributions=ContributionService(conn, clock, audit, lambda: snapshots.current.bundle),
        anomaly=AnomalyHandler(conn, clock, audit, control, notify),
        models=ModelsAdmin(paths.config_dir / "models.yaml", conn, clock, audit),
        anchors=AnchorClient(paths.anchor_sock),
        kiro_key_sha256=os.environ.get("KIRO_KEY_SHA256") or None,
    )
    ctx_ref["ctx"] = ctx
    return ctx
