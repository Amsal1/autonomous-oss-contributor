"""Anomaly signal -> automatic stop action, gatekeeper-side rows of DESIGN §13.4.

Only stop actions exist here (I9): an anomaly can pause or kill, never resume or approve.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Literal

from ossbot.common.clock import Clock, to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.control import ControlService

Action = Literal["kill", "pause", "block", "alert"]


class Signal(StrEnum):
    TOKEN_LOGIN_MISMATCH = "token_login_mismatch"
    PATCH_MISMATCH = "patch_mismatch"
    PATCH_UNREADABLE = "patch_unreadable"
    GITHUB_WRITE_AUTH = "github_write_auth"
    SECRET_IN_TEXT = "secret_in_text"
    HERMES_TOOL_SURFACE = "hermes_tool_surface"
    HERMES_KIRO_SURFACE = "hermes_kiro_surface"
    UNKNOWN_TELEGRAM_USERS = "unknown_telegram_users"
    SANDBOX_REPORTED = "sandbox_reported"  # sandboxd pause requests (secret in output, snapshot unsafe, probe)


@dataclass(frozen=True)
class Rule:
    action: Action
    kill_level: Literal["soft", "full"] = "soft"


RULES: dict[Signal, Rule] = {
    Signal.TOKEN_LOGIN_MISMATCH: Rule("kill"),
    Signal.PATCH_MISMATCH: Rule("pause"),
    Signal.PATCH_UNREADABLE: Rule("pause"),
    Signal.GITHUB_WRITE_AUTH: Rule("pause"),
    Signal.SECRET_IN_TEXT: Rule("block"),  # escalates to pause on the 2nd hit in 24 h
    Signal.HERMES_TOOL_SURFACE: Rule("kill"),
    Signal.HERMES_KIRO_SURFACE: Rule("kill"),
    Signal.UNKNOWN_TELEGRAM_USERS: Rule("alert"),
    Signal.SANDBOX_REPORTED: Rule("pause"),
}
SECRET_ESCALATION_WINDOW = timedelta(hours=24)


class AnomalyHandler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        control: ControlService,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.control = control
        self.notify = notify or (lambda text: None)

    def report(self, signal: Signal, detail: str, cid: str | None = None) -> Action:
        rule = RULES[signal]
        action: Action = rule.action
        if signal is Signal.SECRET_IN_TEXT:
            since = to_iso(self.clock.now() - SECRET_ESCALATION_WINDOW)
            previous = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE event = 'anomaly' AND ts >= ?"
                    " AND json_extract(data_json, '$.signal') = ?",
                    (since, signal.value),
                ).fetchone()[0]
            )
            if previous >= 1:
                action = "pause"
        with db.transaction(self.conn):
            self.audit.append("system", "anomaly", {"signal": signal.value, "detail": detail, "action": action}, cid)
        reason = f"{signal.value}: {detail}"[:500]
        if action == "kill":
            self.control.kill(reason, "anomaly", rule.kill_level)
        elif action == "pause":
            self.control.pause(reason, "anomaly")
        else:
            self.notify(f"🚨 {signal.value}: {detail}")
        return action
