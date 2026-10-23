"""Wiring of the gatekeeper services (one SQLite connection, used from the event-loop thread)."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ossbot.common import probe_result
from ossbot.common.clock import Clock, SystemClock
from ossbot.common.config import ConfigBundle, Limits, ProfileConfig
from ossbot.common.control_flags import HermesTier, read_hermes_tier
from ossbot.common.paths import Paths
from ossbot.common.schemas import HoldView, State
from ossbot.gatekeeper import auto_reissue, db, implement_network, policy, proposals, secret_scan, signing
from ossbot.gatekeeper.anchors import AnchorClient
from ossbot.gatekeeper.anomaly import AnomalyHandler, Signal
from ossbot.gatekeeper.approvals import ApprovalError, ApprovalService, ArtifactSource, Invalidated
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.authz import Role, interactive_ancestry
from ossbot.gatekeeper.chat import hermes_api
from ossbot.gatekeeper.chat.backend import ChatRuntime, ChatService, HermesApiBackend, RelayGate
from ossbot.gatekeeper.chat.code_jobs import CodeJobs
from ossbot.gatekeeper.chat.hermes_api import HermesClient
from ossbot.gatekeeper.chat.hermes_surface import SurfaceResult, check_toolsets
from ossbot.gatekeeper.chat.media import MediaStore
from ossbot.gatekeeper.chat.pdf_jobs import PdfJobs
from ossbot.gatekeeper.chat.turns import TurnStore
from ossbot.gatekeeper.config_snapshot import SnapshotManager
from ossbot.gatekeeper.contributions import ContributionService
from ossbot.gatekeeper.control import ControlService
from ossbot.gatekeeper.decisions import DecisionServices
from ossbot.gatekeeper.derive import GitArtifacts
from ossbot.gatekeeper.discovered_targets import DiscoveredTargets
from ossbot.gatekeeper.discovery_prefs import DiscoveryPrefs
from ossbot.gatekeeper.gitops import GitOps
from ossbot.gatekeeper.holds import ContributionHolds
from ossbot.gatekeeper.models_admin import ModelsAdmin
from ossbot.gatekeeper.outbox import Outbox
from ossbot.gatekeeper.pollstats import PollStats
from ossbot.gatekeeper.proposal_approvals import ProposalApprovals
from ossbot.gatekeeper.secret_scan import SecretScanner
from ossbot.gatekeeper.skill_approvals import SkillApprovals
from ossbot.gatekeeper.skill_reviews import DbSkillSource, SkillReviews, render_skill_card
from ossbot.gatekeeper.stage_runs import StageRuns
from ossbot.gatekeeper.state_machine import StateMachine
from ossbot.gatekeeper.telegram import cards, proposal_cards
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
    discovery_prefs: DiscoveryPrefs = field(default=None)  # type: ignore[assignment]  # set by build_context
    # post-v1 open discovery (design §8): repos found outside targets.yaml, set by build_context
    discovered: DiscoveredTargets = field(default=None)  # type: ignore[assignment]
    stage_runs: StageRuns = field(default=None)  # type: ignore[assignment]  # set by build_context
    # open-discovery design §13.3 (ADR-0023): per-contribution egress-anomaly holds, set by build_context
    holds: ContributionHolds = field(default=None)  # type: ignore[assignment]
    skill_reviews: SkillReviews = field(default=None)  # type: ignore[assignment]  # set by build_context
    # CardPublisher.recover (delta 8); TelegramFrontend.start sets it, housekeeping calls it each tick
    card_recover: Callable[[], None] = field(default=lambda: None)
    # reissue_due: the invalidated approval ids whose auto re-send was refused (per process, D6)
    auto_reissue_failed: set[str] = field(default_factory=set)
    chat: ChatRuntime | None = None  # the assistant backend (§15.6), set by build_context
    review_mirror: Any = None  # the opt-in review mirror service (ADR-0019 C), set by main
    # the gatekeeper's read-only GitHub client (GETs only), set by main: /v1/work repo checks
    github_reader: Any = None
    telegraph: Any = None  # the opt-in Telegraph service (§14.12.8), set by main

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
    def signer(self) -> signing.CommitSigner | None:
        """The optional SSH commit signer of ``gatekeeper.env`` (None = no key); ``SigningError`` when the
        key is set but unusable, so ``/package`` fails closed instead of silently not signing."""
        body = (self.env.get(signing.SIGNING_ENV) or "").strip()
        cached = self.__dict__.get("_signer")
        if cached is None or cached[0] != body:
            cached = (body, signing.load(self.env))
            self.__dict__["_signer"] = cached
        signer: signing.CommitSigner | None = cached[1]
        return signer

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

    def reissue_due(self, actor: str = "system") -> list[str]:
        """Re-send every owed card (``auto_reissue``, §12.4, ADR-0008 addendum): after a start-time
        snapshot change, a kill, a deferred reload or dry-run toggle. Runs first in
        ``CardPublisher.recover`` (start and every housekeeping tick); nothing while paused or killed (the
        cards follow on the first tick after resume). Each re-send goes through the wrappers above with
        the expiry kept, so the binding is recomputed by ``create`` (I1, I3). A failing re-send is
        audited once and not retried by this process (a restart retries once)."""
        if not self.approvals.system_running():
            return []
        now = self.clock.now()
        floor = now + timedelta(seconds=self.config.policy.limits.confirm_ttl_seconds)
        wrappers: dict[str, Callable[..., list[str]]] = {"a": self.reissue, "p": self.reissue_proposal,
                                                         "s": self.reissue_skill}  # fmt: skip
        created: list[str] = []
        for item in auto_reissue.owed(self.conn, now, floor):
            if item.newest_id in self.auto_reissue_failed:
                continue
            cid = None if item.family == "s" else item.subject
            try:
                ids = wrappers[item.family](item.subject, actor, keep_expiry=True)
            except (ApprovalError, KeyError, ValueError, OSError) as exc:
                code = exc.code if isinstance(exc, ApprovalError) else type(exc).__name__
                self.auto_reissue_failed.add(item.newest_id)
                with db.transaction(self.conn):
                    self.audit.append(actor, "auto_reissue_failed", {"family": item.family, "subject": item.subject,
                                                                     "previous": item.newest_id, "code": code},
                                      cid)  # fmt: skip
                log.warning("auto re-send of %s refused: %s", item.subject, code,
                            extra={"event": "auto_reissue_failed"})  # fmt: skip
                continue
            with db.transaction(self.conn):
                data = {"family": item.family, "subject": item.subject, "previous": item.newest_id, "approval_ids": ids}
                self.audit.append(actor, "auto_reissue", data, cid)
            log.info("re-sent %d card(s) for %s", len(ids), item.subject, extra={"event": "auto_reissue"})
            created += ids
        return created

    def reissue_cards(self, invalidated: list[Invalidated], actor: str) -> list[str]:
        """Fresh cards for approvals a dry-run toggle or a reload invalidated (§12.4). Outbound-action
        cards get a fresh TTL (the bound mode or policy changed); proposal and skill cards keep their
        expiry and are re-created only while still open (``reissue_invalidated``, BA-5, BA-17).
        While paused or killed nothing is created: the reasons are auto re-send reasons, so
        ``reissue_due`` sends the cards on the first tick after resume (ADR-0008 addendum)."""
        if not self.approvals.system_running():
            return []
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

    def notify(self, text: str, *, priority: int | None = None, category: str | None = None,
               coalesce_key: str | None = None, thread: str | None = None) -> None:  # fmt: skip
        """FYI/alert to the owner's private chat via the outbox (dropped while Telegram is unset).

        ``text`` is plain text: it is sent as escaped HTML with its ids in ``<code>`` (one tap to copy).
        The priority comes from the text's fixed leading icon unless given (``telegram.notify.classify``:
        🛑/🚨 are P0 in 🚦 Control and always ring; FYIs are silent P2 in 📈 OSS feed and may be held
        in quiet hours or a batch window, §14.13)."""
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
        from ossbot.gatekeeper.telegram import notify as tg_notify  # noqa: PLC0415 - telegram imports the context

        prio, holdable = tg_notify.classify(text)
        if priority is not None:
            prio, holdable = priority, holdable and priority >= 2
        from ossbot.gatekeeper.telegram.ui import ids  # noqa: PLC0415 - telegram imports the context

        # The text stays data (§14.13): escaped, ids as one-tap code (``ui/ids.py``), never markup.
        payload = {"chat_id": chat_id, "text": ids.autocode(text), "parse_mode": "HTML"}
        self.outbox.enqueue("sendMessage", payload, priority=prio,
                            thread_key=thread or tg_notify.THREAD_OF[prio], category=category, holdable=holdable,
                            coalesce_key=coalesce_key)  # fmt: skip

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

    def has_interdiff(cid: str, rev: int) -> bool:
        info = cards.interdiff_info(cid, rev)  # derived/<cid>/<rev>/interdiff.json (PLAN item 45)
        return info is not None and info.get("mode") != "unavailable"

    cards.set_derived_root(paths.gatekeeper_dir / "derived")
    approvals = ApprovalService(conn, clock, audit, paths.control_dir, limits, profile, artifacts,
                                interdiff=has_interdiff)  # fmt: skip
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

    def hermes_tier() -> HermesTier:
        return read_hermes_tier(paths.control_dir, holder["snap"].current.bundle.policy.hermes.capability_tier_default)

    decisions = DecisionServices(
        approvals=approvals,
        proposals=ProposalApprovals(
            conn, clock, audit, approvals, lambda: holder["snap"].current.bundle.policy.proposal.ttl_hours,
            confirm_ttl, gate1_refusal, renderer=lambda row: proposal_cards.render(ctx_ref["ctx"], row),
            chat_id=owner_chat,
            source=proposals.StoredProposals(lambda: holder["snap"].current.hash),
        ),
        skills=SkillApprovals(
            conn, clock, audit, approvals, lambda: limits().approval_ttl_hours, confirm_ttl, skill_refusal,
            chat_id=owner_chat, renderer=lambda row: render_skill_card(conn, row), source=DbSkillSource(hermes_tier),
        ),
    )  # fmt: skip
    control = ControlService(conn, paths, clock, audit, approvals, notify, decisions=decisions)
    control_ref["control"] = control
    snapshots = SnapshotManager(conn, paths, clock, audit, control, approvals, notify, decisions=decisions)
    holder["snap"] = snapshots
    prefs = DiscoveryPrefs(conn, clock, audit, lambda: snapshots.current.bundle, notify)
    discovered = DiscoveredTargets(conn, clock, audit, lambda: snapshots.current.bundle, prefs, notify)
    holds = ContributionHolds(conn, clock, audit, approvals)
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
        state_machine=StateMachine(
            conn,
            clock,
            audit,
            approvals,
            control,
            limits,
            notify,
            claim_comment=lambda: holder["snap"].current.bundle.policy.proposal.claim_comment == "on",
        ),
        contributions=ContributionService(conn, clock, audit, lambda: snapshots.current.bundle, discovered),
        anomaly=AnomalyHandler(conn, clock, audit, control, notify),
        models=ModelsAdmin(paths.config_dir / "models.yaml", conn, clock, audit, notify),
        anchors=AnchorClient(paths.anchor_sock),
        gitops=gitops,
        kiro_key_sha256=os.environ.get("KIRO_KEY_SHA256") or None,
        poll_stats=PollStats(clock),
        decisions=decisions,
        discovery_prefs=prefs,
        discovered=discovered,
        stage_runs=StageRuns(conn, clock, audit, control, held=holds.is_held),
        holds=holds,
    )
    ctx_ref["ctx"] = ctx

    def publish_hold(view: HoldView) -> None:
        """The P1 hold card (open-discovery §13.3): injected, so holds.py never imports Telegram."""
        from ossbot.gatekeeper.telegram import hold_cards  # noqa: PLC0415 - telegram imports the context

        hold_cards.queue_hold_card(ctx, view)

    holds.publish_hold = publish_hold

    def on_terminal(row: sqlite3.Row, from_: State, to: State, reason: str | None, evidence: dict[str, Any]) -> None:
        """The P2 terminal FYI after gate 1 (§14.13.1): injected, so state_machine.py never imports Telegram."""
        from ossbot.gatekeeper.telegram import terminal_fyi  # noqa: PLC0415 - telegram imports the context

        terminal_fyi.queue_terminal_fyi(ctx, row, from_, to, reason, evidence)

    ctx.state_machine.on_terminal = on_terminal
    # ADR-0024: the H-only FAILED -> PROPOSAL_APPROVED edge and the Telegram card share this one guard
    ctx.state_machine.rerun_guard = lambda row: implement_network.rerun_guard(ctx, row)
    decisions.proposals.rows_hook = lambda approval_id, tokens: proposal_cards.card_rows(ctx, approval_id, tokens)
    ctx.skill_reviews = SkillReviews(conn, paths, clock, audit, decisions.skills, hermes_tier, owner_chat,
                                     on_card=lambda approval_id: ctx.on_approval_created(approval_id))  # noqa: PLW0108 - late-bound: the front-end replaces it  # fmt: skip
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
    ctx.chat = chat_runtime(ctx, hermes_tier)
    ctx.chat.service.turns.reset_streaming()  # turns a crash or restart left open (§14.18.5)
    return ctx


def chat_runtime(ctx: AppContext, tier: Callable[[], HermesTier]) -> ChatRuntime:
    """The assistant backend (§15.6): relay gate, Hermes API client, turns, attachments, PDF jobs."""
    key = ctx.env.get("HERMES_API_KEY") or ""
    probe_path = ctx.paths.var_lib / "probes" / "hermes-kiro.json"

    def surface(t: HermesTier) -> SurfaceResult:
        if not key:
            return SurfaceResult("unavailable", "HERMES_API_KEY missing")
        return check_toolsets(hermes_api.DEFAULT_URL, key, t)

    def lifecycle() -> str | None:
        info = ctx.control.lifecycle()
        return None if info is None else info.state

    def surface_kill(result: SurfaceResult) -> None:
        detail = f"{result.outcome}: {result.detail}; unexpected {', '.join(result.unexpected) or '-'}"
        ctx.anomaly.report(Signal.HERMES_TOOL_SURFACE, detail[:300])

    def probe_doc() -> Mapping[str, Any] | None:
        return probe_result.read_result(probe_path)

    gate = RelayGate(flags=ctx.control.flags, lifecycle=lifecycle, probe_doc=probe_doc, surface=surface, tier=tier,
                     on_surface_kill=surface_kill)  # fmt: skip
    service = ChatService(HermesApiBackend(HermesClient(key), gate), TurnStore(ctx.conn, ctx.clock))
    media_store = MediaStore(ctx.paths.gatekeeper_dir / "chat")

    def stopped() -> str | None:
        flags = ctx.control.flags()
        if flags.killed is not None:
            return "killed"
        if flags.paused is not None:
            return "paused"
        return None if lifecycle() is None else "workers_down"

    code = CodeJobs(ctx.paths.artifacts_dir, media_store, redact=lambda t: ui_redact.redact(t, surface="code_job")[0],
                    stopped=stopped)  # fmt: skip
    return ChatRuntime(service, gate, media_store, PdfJobs(ctx.paths.artifacts_dir), probe_path, code, tier)
