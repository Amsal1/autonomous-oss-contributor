"""Shared fixtures. Tests never read config/profile.yaml; they use tests/fixtures/profile.test.yaml."""

from __future__ import annotations

import itertools
import json
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.ids import new_action_id, new_cid
from ossbot.common.paths import Paths
from ossbot.common.schemas import OutboundKind, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ArtifactHashes
from ossbot.gatekeeper.authz import Role
from ossbot.gatekeeper.context import AppContext, build_context
from ossbot.gatekeeper.gitops import Git, GitOps
from tests.githarness import GitWorld, LocalRemote

_ISSUES = itertools.count(1000)
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Fake uids for role mapping (tests never depend on real accounts).
UID_OPERATOR = 1000
UID_ROOT = 0
UID_WORKER = 2001
UID_SANDBOX = 2002
UID_REQUESTER = 2003
UID_NOBODY = 1001
TEST_ROLES: dict[int, Role] = {
    UID_ROOT: Role.OPERATOR,
    UID_OPERATOR: Role.OPERATOR,
    UID_WORKER: Role.WORKER,
    UID_SANDBOX: Role.SANDBOX,
    UID_REQUESTER: Role.REQUESTER,
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        node = item.nodeid.replace("\\", "/")
        if "/tests/unit/" in node or node.startswith("tests/unit/"):
            item.add_marker(pytest.mark.unit)
        elif "/tests/telegram/" in node or node.startswith("tests/telegram/"):
            item.add_marker(pytest.mark.telegram)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "config").mkdir(parents=True)
    for name in ("policy.yaml", "egress.yaml", "targets.yaml", "models.yaml"):
        shutil.copy(REPO_ROOT / "config" / name, repo / "config" / name)
    shutil.copy(FIXTURES / "profile.test.yaml", repo / "config" / "profile.yaml")
    return repo


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths.under(tmp_path, repo=make_repo(tmp_path))
    for d in (p.control_dir, p.logs_dir, p.snapshot_root, p.gatekeeper_dir, p.run):
        d.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@dataclass
class FakeArtifacts:
    """Stand-in for the FEAT-002 mirror: hashes per (cid, rev), editable to simulate tampering."""

    hashes_by_rev: dict[tuple[str, int], ArtifactHashes] = field(default_factory=dict)

    def hashes(self, cid: str, rev: int, kind: OutboundKind) -> ArtifactHashes:
        return self.hashes_by_rev.get((cid, rev), ArtifactHashes("b" * 64, "p" * 64, "m" * 64))


@pytest.fixture
def artifacts() -> FakeArtifacts:
    return FakeArtifacts()


@pytest.fixture
def ctx(paths: Paths, clock: FakeClock, artifacts: FakeArtifacts) -> Iterator[AppContext]:
    context = build_context(paths, TEST_ROLES, clock=clock, artifacts=artifacts)
    context.snapshots.load_at_start()
    yield context
    context.conn.close()


@pytest.fixture
def conn(ctx: AppContext) -> sqlite3.Connection:
    return ctx.conn


def fake_scanner(data: bytes) -> list[str]:
    """Deterministic stand-in for gitleaks in unit tests (the real binary has its own test)."""
    return ["gitleaks"] if b"FAKE_LEAK_" in data else []


@pytest.fixture
def remote() -> LocalRemote:
    return LocalRemote()


@pytest.fixture
def world(tmp_path: Path, remote: LocalRemote) -> GitWorld:
    return GitWorld(tmp_path / "world", remote)


@pytest.fixture
def gctx(paths: Paths, clock: FakeClock, remote: LocalRemote) -> Iterator[AppContext]:
    """Context with the real git TCB (production GIT_ENV/GIT_ARGS), recording every git argv."""
    ops = GitOps(paths, Git(home=paths.gatekeeper_dir / "githome", record=True), remote)
    context = build_context(paths, TEST_ROLES, clock=clock, gitops=ops)
    context.snapshots.load_at_start()
    context.gitleaks_scanner = fake_scanner
    yield context
    context.conn.close()


def insert_contribution(
    ctx: AppContext,
    state: State = State.DISCOVERED,
    *,
    upstream: str = "langfuse/langfuse",
    issue_number: int | str | None = "auto",
    source: str = "discover",
    **columns: Any,
) -> str:
    cid = columns.pop("id", None) or new_cid()
    if issue_number == "auto":
        issue_number = next(_ISSUES)
    now = to_iso(ctx.clock.now())
    values: dict[str, Any] = {
        "id": cid,
        "upstream": upstream,
        "issue_number": issue_number,
        "source": source,
        "state": state.value,
        "created_at": columns.pop("created_at", now),
        "updated_at": columns.pop("updated_at", now),
        **columns,
    }
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with db.transaction(ctx.conn):
        ctx.conn.execute(f"INSERT INTO contributions ({cols}) VALUES ({marks})", tuple(values.values()))
    return cid


def insert_action(
    ctx: AppContext,
    cid: str,
    kind: OutboundKind,
    payload: dict[str, Any],
    status: str = "pending_approval",
    **cols: Any,
) -> str:
    action_id = new_action_id()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at, executed_at, error_class)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                action_id,
                cid,
                kind,
                json.dumps(payload),
                status,
                to_iso(ctx.clock.now()),
                cols.get("executed_at"),
                cols.get("error_class"),
            ),
        )
    return action_id


def packaged_contribution(ctx: AppContext, *, pr_number: int | None = None) -> tuple[str, str]:
    """A contribution in AWAITING_APPROVAL with an open_pr action (or push_update with a PR)."""
    cid = insert_contribution(
        ctx,
        State.AWAITING_APPROVAL,
        base_ref="main",
        base_sha="1" * 40,
        upstream_base_tip="2" * 40,
        pr_title="Fix the thing",
        pr_body="Body\n\n<!-- ossbot:cid=x --> AI assistance disclosure",
        pr_number=pr_number,
        head_sha="9" * 40 if pr_number else None,
    )
    if pr_number is None:
        payload = {"rev": 1, "head_sha": "3" * 40, "branch": "ossbot/101-fix-abc123", "fork_owner": "test-bot-login"}
        action = insert_action(ctx, cid, "open_pr", payload)
    else:
        payload = {
            "rev": 2,
            "head_sha": "4" * 40,
            "branch": "ossbot/101-fix-abc123",
            "fork_owner": "test-bot-login",
            "pr_number": pr_number,
            "old_head_sha": "9" * 40,
        }
        action = insert_action(ctx, cid, "push_update", payload)
    return cid, action


def write_flag(path: Path, payload: dict[str, Any] | str) -> None:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
