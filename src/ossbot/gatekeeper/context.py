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
from ossbot.gatekeeper import db, policy, secret_scan
from ossbot.gatekeeper.anchors import AnchorClient
from ossbot.gatekeeper.anomaly import AnomalyHandler
from ossbot.gatekeeper.approvals import ApprovalService, ArtifactSource, Invalidated
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.authz import Role, interactive_ancestry
from ossbot.gatekeeper.config_snapshot import SnapshotManager
from ossbot.gatekeeper.contributions import ContributionService
from ossbot.gatekeeper.control import ControlService
from ossbot.gatekeeper.decisions import DecisionServices
from ossbot.gatekeeper.derive import GitArtifacts
from ossbot.gatekeeper.gitops import GitOps
from ossbot.gatekeeper.models_admin import ModelsAdmin
from ossbot.gatekeeper.outbox import Outbox
from ossbot.gatekeeper.pollstats import PollStats
from ossbot.gatekeeper.proposal_approvals import ProposalApprovals
from ossbot.gatekeeper.secret_scan import SecretScanner
from ossbot.gatekeeper.skill_approvals import SkillApprovals
from ossbot.gatekeeper.state_machine import StateMachine
from ossbot.gatekeeper.telegram import cards
from ossbot.gatekeeper.telegram.ui import redact as ui_redact

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
    gitops: GitOps
    kiro_key_sha256: str | None = None
    gitleaks_scanner: Callable[[bytes], list[str]] | None = None  # None = gitleaks (packaging.gitleaks_scan)
    # gatekeeper.env (the service's environment): the source of the U14 secret values
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    on_approval_created: Callable[[str], None] = field(default=lambda approval_id: None)
    poll_stats: PollStats = field(default_factory=PollStats)  # Telegram long-poll health (CR-001)
    decisions: DecisionServices = field(default=None)  # type: ignore[assignment]  # set by build_context
    # CardPublisher.recover (delta 8); TelegramFrontend.start sets it, housekeeping calls it each tick
    card_recover: Callable[[], None] = field(default=lambda: None)

    @property
    def config(self) -> ConfigBundle:
        return self.snapshots.current.bundle

    @property
    def secret_scanner(self) -> SecretScanner:
        """The U14 scanner of the current snapshot (rebuilt when a confirmed /reload changes it)."""
        current = self.snapshots.current.hash
        cached = self.__dict__.get("_scanner")
        if cached is None or cached[0] != current:
            cached = (current, secret_scan.build(self.config, self.env))
            self.__dict__["_scanner"] = cached
        scanner: SecretScanner = cached[1]
        return scanner

    def reset_secret_scanner(self) -> None:
        self.__dict__.pop("_scanner", None)

    @property
    def telegram_chat_id(self) -> int:
        return self.config.profile.telegram.user_id

    def create_approval(self, action_id: str, actor: str) -> str:
        """New pending approval; ``body_parts_total`` from the card renderer. Caller fires the card
        hook (``on_approval_created``) after its transaction commits."""
        return self.approvals.create(
            action_id,
            tg_chat_id=self.telegram_chat_id,
            body_parts_total=cards.part_count(self.conn, action_id),
            actor=actor,
        )

    # ------------------------------------------------- re-sends (the only callers, BA-17)
    def reissue(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]:
        """Show card, Show current, /queue -> Open, typed /approve and Retry (§14.18.4.3)."""
        ids = self.approvals.reissue(
            cid, tg_chat_id=self.telegram_chat_id, actor=actor, keep_expiry=keep_expiry,
            part_count=lambda action_id: cards.part_count(self.conn, action_id),
        )  # fmt: skip
        for aid in ids:  # after reissue()'s transaction has committed
            self.on_approval_created(aid)
        return ids

    def reissue_proposal(self, cid: str, actor: str, *, keep_expiry: bool) -> list[str]:
        ids = self.decisions.proposals.reissue(cid, actor=actor, keep_expiry=keep_expiry)
        for pid in ids:  # proposal cards have no parts: armable once sent
            self.on_approval_created(pid)
        return ids

    def reissue_skill(self, skill: str, actor: str, *, keep_expiry: bool) -> list[str]:
        ids = self.decisions.skills.reissue(skill, actor=actor, keep_expiry=keep_expiry)
        for sid in ids:  # parts follow; Approve after all of them
            self.on_approval_created(sid)
        return ids

    def reissue_cards(self, invalidated: list[Invalidated], actor: str) -> list[str]:
        """Fresh cards for approvals a dry-run toggle or a reload invalidated (§12.4). Outbound-action
        cards get a fresh TTL (the bound mode or policy changed); proposal and skill cards keep their
        expiry and are re-created only while still open (``reissue_invalidated``, BA-5, BA-17)."""
        created: list[str] = []
        proposals = [i.cid for i in invalidated if i.approval_id.startswith("p_") and i.cid is not None]
        skill_ids = [i.approval_id for i in invalidated if i.approval_id.startswith("s_")]
        with db.transaction(self.conn):
            for action_id in dict.fromkeys(i.action_id for i in invalidated if i.action_id is not None):
                row = db.one(
                    self.conn,
                    "SELECT o.status, c.state FROM outbound_actions o JOIN contributions c ON c.id = o.cid"
                    " WHERE o.id = ?",
                    (action_id,),
                )
                if row is None or row["status"] != "pending_approval":
                    continue
                created.append(self.create_approval(action_id, actor))
            created += self.decisions.proposals.reissue_invalidated(proposals, actor=actor)
            skills = [
                str(r["skill"])
                for sid in skill_ids
                if (r := db.one(self.conn, "SELECT skill FROM skill_approvals WHERE id = ?", (sid,))) is not None
            ]
            created += self.decisions.skills.reissue_invalidated(skills, actor=actor)
        for approval_id in created:
            self.on_approval_created(approval_id)
        return created

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
    gitops: GitOps | None = None,
) -> AppContext:
    """Create every service. The config snapshot is loaded separately (``snapshots.load_at_start``).

    Without ``artifacts`` the approval binding re-derives from the gatekeeper's mirrors
    (``derive.GitArtifacts``); tests that do not exercise git pass a stand-in."""
    clock = clock or SystemClock()
    if conn is None:
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(paths.db_path)
    db.migrate(conn)
    gitops = gitops or GitOps(paths)
    artifacts = artifacts or GitArtifacts(gitops, conn)
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
    control_ref: dict[str, ControlService] = {}

    def gate1_refusal() -> str | None:
        """Proposal Start work: refused while paused, killed, draining or down (§14.18.4.2)."""
        if not approvals.system_running():
            return "paused or killed"
        info = control_ref["control"].lifecycle()
        return None if info is None else info.state

    def skill_refusal() -> str | None:
        return None if approvals.system_running() else "paused or killed"

    def confirm_ttl() -> int:
        return limits().confirm_ttl_seconds

    def owner_chat() -> int:
        return profile().telegram.user_id

    decisions = DecisionServices(
        approvals=approvals,
        proposals=ProposalApprovals(
            conn, clock, audit, approvals, lambda: holder["snap"].current.bundle.policy.proposal.ttl_hours,
            confirm_ttl, gate1_refusal, chat_id=owner_chat,
        ),
        skills=SkillApprovals(
            conn, clock, audit, approvals, lambda: limits().approval_ttl_hours, confirm_ttl, skill_refusal,
            chat_id=owner_chat,
        ),
    )  # fmt: skip
    control = ControlService(conn, paths, clock, audit, approvals, notify, decisions=decisions)
    control_ref["control"] = control
    snapshots = SnapshotManager(conn, paths, clock, audit, control, approvals, notify, decisions=decisions)
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
        gitops=gitops,
        kiro_key_sha256=os.environ.get("KIRO_KEY_SHA256") or None,
        poll_stats=PollStats(clock),
        decisions=decisions,
    )
    ctx_ref["ctx"] = ctx
    # BA-16: a snapshot whose secret scanner can't be built refuses the start or the /reload.
    snapshots.validators.append(lambda bundle: secret_scan.build(bundle, ctx.env))

    def text_check(kind: str, text: str) -> list[policy.Violation]:
        """P1-4: an edited title/body passes the same text policy as packaging."""
        return policy.check_text(
            kind, text, ctx.config, policy.effective_confidentiality(conn, ctx.config),  # type: ignore[arg-type]
            ctx.kiro_key_sha256, secret_values=ctx.secret_scanner.values,
        )  # fmt: skip

    approvals.text_check = text_check
    ui_redact.bind(lambda: ctx.secret_scanner)  # U14: the one redactor follows the current snapshot
    return ctx
