"""Approval binding, two-tap lifecycle, tokens and every invalidation (§12.4, §22.1; non-git cases)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.hashing import sha256_text
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import (
    ApprovalError,
    ArtifactHashes,
    Superseded,
    binding_hash,
    build_binding,
    compute_binding,
    normalize_text,
)
from ossbot.gatekeeper.context import AppContext
from tests.conftest import FakeArtifacts, insert_action, insert_contribution, packaged_contribution, write_flag

CHAT = 4242
MSG = 555


def card(ctx: AppContext, action: str, parts: int = 1, message_id: int = MSG) -> tuple[str, dict[str, str]]:
    approval = ctx.approvals.create(action, tg_chat_id=CHAT, body_parts_total=parts)
    ctx.approvals.set_card_message(approval, message_id)
    for _ in range(parts):
        ctx.approvals.mark_part_delivered(approval)
    return approval, ctx.approvals.issue_card_tokens(approval)


def tap(ctx: AppContext, tokens: dict[str, str], message_id: int = MSG) -> str:
    return ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=message_id).confirm_token


def confirm(ctx: AppContext, token: str, message_id: int = MSG) -> dict[str, object]:
    return ctx.approvals.confirm(token, tg_user_id=CHAT, chat_id=CHAT, message_id=message_id)


def status(ctx: AppContext, approval: str) -> str:
    row = db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))
    assert row is not None
    return str(row["status"])


# ----------------------------------------------------------------------------- binding
def test_build_binding_requires_exact_kind_fields() -> None:
    common = {"cid": "c_1", "upstream": "a/b", "dry_run": True, "policy_hash": "h"}
    ok = build_binding("comment", fields={"target_number": 1, "body_sha256": "x"}, **common)  # type: ignore[arg-type]
    assert ok["v"] == 2 and ok["kind"] == "comment"
    with pytest.raises(ApprovalError):
        build_binding("comment", fields={"target_number": 1}, **common)  # type: ignore[arg-type]
    with pytest.raises(ApprovalError):
        build_binding("comment", fields={"target_number": 1, "body_sha256": "x", "extra": 1}, **common)  # type: ignore[arg-type]


def test_binding_hash_stable(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    a = compute_binding(ctx.conn, action, ctx.approvals.artifacts)
    b = compute_binding(ctx.conn, action, ctx.approvals.artifacts)
    assert binding_hash(a) == binding_hash(b)
    assert binding_hash(dict(reversed(list(a.items())))) == binding_hash(a)


def _set(column: str, value: object) -> Callable[[AppContext, str, str, FakeArtifacts], None]:
    def mutate(ctx: AppContext, cid: str, action: str, artifacts: FakeArtifacts) -> None:
        with db.transaction(ctx.conn):
            ctx.conn.execute(f"UPDATE contributions SET {column} = ? WHERE id = ?", (value, cid))

    return mutate


def _payload(key: str, value: object) -> Callable[[AppContext, str, str, FakeArtifacts], None]:
    def mutate(ctx: AppContext, cid: str, action: str, artifacts: FakeArtifacts) -> None:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE outbound_actions SET payload_json = json_set(payload_json, ?, ?) WHERE id = ?",
                (f"$.{key}", value, action),
            )

    return mutate


def _artifact(field: str) -> Callable[[AppContext, str, str, FakeArtifacts], None]:
    def mutate(ctx: AppContext, cid: str, action: str, artifacts: FakeArtifacts) -> None:
        base = {"bundle_sha256": "b" * 64, "patch_sha256": "p" * 64, "commit_message_sha256": "m" * 64}
        base[field] = "0" + base[field][1:]
        artifacts.hashes_by_rev[(cid, 1)] = ArtifactHashes(**base)

    return mutate


def _policy_hash(ctx: AppContext, cid: str, action: str, artifacts: FakeArtifacts) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE control_state SET config_hash = ? WHERE id = 1", ("f" * 64,))


def _dry_run(ctx: AppContext, cid: str, action: str, artifacts: FakeArtifacts) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE control_state SET dry_run = 0 WHERE id = 1")


SINGLE_FIELD_CHANGES = {
    "title_byte": _set("pr_title_override", "Fix the thinG"),
    "body_byte": _set("pr_body_override", "Body\n\n<!-- ossbot:cid=x --> AI assistance disclosure!"),
    "commit_message_byte": _artifact("commit_message_sha256"),
    "head_sha": _payload("head_sha", "5" * 40),
    "patch_hash": _artifact("patch_sha256"),
    "bundle_hash": _artifact("bundle_sha256"),
    "base_ref": _set("base_ref", "develop"),
    "base_sha": _set("base_sha", "6" * 40),
    "upstream_base_tip": _set("upstream_base_tip", "7" * 40),
    "fork_owner": _payload("fork_owner", "someone-else"),
    "branch": _payload("branch", "ossbot/101-other"),
    "policy_hash": _policy_hash,
    "dry_run": _dry_run,
}


@pytest.mark.parametrize("change", sorted(SINGLE_FIELD_CHANGES))
def test_any_single_field_change_changes_the_hash(ctx: AppContext, artifacts: FakeArtifacts, change: str) -> None:
    cid, action = packaged_contribution(ctx)
    before = binding_hash(compute_binding(ctx.conn, action, artifacts))
    SINGLE_FIELD_CHANGES[change](ctx, cid, action, artifacts)
    assert binding_hash(compute_binding(ctx.conn, action, artifacts)) != before


@pytest.mark.parametrize("change", sorted(SINGLE_FIELD_CHANGES))
def test_any_single_field_change_supersedes_at_confirm(ctx: AppContext, artifacts: FakeArtifacts, change: str) -> None:
    cid, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    confirm_token = tap(ctx, tokens)
    SINGLE_FIELD_CHANGES[change](ctx, cid, action, artifacts)
    with pytest.raises(Superseded):
        confirm(ctx, confirm_token)
    assert status(ctx, approval) == "invalidated"


def test_push_update_binding_fields(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx, pr_number=7)
    binding = compute_binding(ctx.conn, action, ctx.approvals.artifacts)
    assert binding["kind"] == "push_update" and binding["pr_number"] == 7 and binding["old_head_sha"] == "9" * 40
    assert "title_sha256" not in binding


def test_comment_binding_hashes_stored_body(ctx: AppContext) -> None:
    cid = insert_contribution(ctx)
    action = insert_action(ctx, cid, "comment", {"target_number": 5, "body": "hello"})
    binding = compute_binding(ctx.conn, action, ctx.approvals.artifacts)
    assert binding["body_sha256"] == sha256_text("hello") and binding["target_number"] == 5


def test_pr_kinds_fail_closed_without_artifacts(ctx: AppContext) -> None:
    from ossbot.gatekeeper.approvals import NoArtifacts

    ctx.approvals.artifacts = NoArtifacts()
    _, action = packaged_contribution(ctx)
    with pytest.raises(ApprovalError, match="artifacts_unavailable"):
        ctx.approvals.create(action, tg_chat_id=CHAT, body_parts_total=1)


# --------------------------------------------------------------------------- normalization (N3)
def test_normalize_text() -> None:
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"
    assert normalize_text("e\u0301") == "\u00e9"  # NFC
    assert normalize_text("Title  \t", title=True) == "Title"
    assert normalize_text("Body  ") == "Body  "


def test_stored_bytes_equal_hashed_bytes(ctx: AppContext) -> None:
    """N3: a CRLF body edit is stored as LF and the binding hashes exactly the stored bytes."""
    cid, action = packaged_contribution(ctx)
    body = "Line 1\r\nLine 2\r\nAI assistance disclosure\r\n<!-- ossbot:cid=x -->"
    ctx.approvals.set_text_override(cid, body=body, actor="tg:4242")
    row = db.one(ctx.conn, "SELECT pr_body_override FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row[0] == "Line 1\nLine 2\nAI assistance disclosure\n<!-- ossbot:cid=x -->"
    binding = compute_binding(ctx.conn, action, ctx.approvals.artifacts)
    assert binding["body_sha256"] == sha256_text(row[0])


# ------------------------------------------------------------------------------ lifecycle
def test_full_lifecycle_and_single_use(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    assert status(ctx, approval) == "pending"
    confirm_token = tap(ctx, tokens)
    assert status(ctx, approval) == "armed"
    with pytest.raises(ApprovalError, match="token_used"):
        tap(ctx, tokens)
    binding = confirm(ctx, confirm_token)
    assert status(ctx, approval) == "confirmed" and binding["dry_run"] is True
    with pytest.raises(ApprovalError, match="token_used"):
        confirm(ctx, confirm_token)
    ctx.approvals.consume(approval)
    assert status(ctx, approval) == "consumed"
    with pytest.raises(ApprovalError, match="wrong_status"):
        ctx.approvals.consume(approval)  # single use


def test_confirm_text_states_current_mode(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    _, tokens = card(ctx, action)
    result = ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)
    assert result.confirm_text.startswith("Confirm: DRY RUN") and not result.live
    ctx.control.set_dry_run(False, "tg:4242")
    _, action2 = packaged_contribution(ctx)
    _, tokens2 = card(ctx, action2, message_id=556)
    live = ctx.approvals.approve(tokens2["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=556)
    assert live.live
    assert live.confirm_text.startswith(
        "Confirm: LIVE — open PR on langfuse/langfuse as test-bot-login (author Test Person)"
    )


def test_token_bound_to_message_and_chat(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    _, tokens = card(ctx, action)
    with pytest.raises(ApprovalError, match="token_wrong_message"):
        ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=MSG + 1)
    with pytest.raises(ApprovalError, match="token_wrong_message"):
        ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=999, message_id=MSG)
    with pytest.raises(ApprovalError, match="token_purpose"):
        ctx.approvals.confirm(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)
    with pytest.raises(ApprovalError, match="token_invalid"):
        ctx.approvals.approve("A" * 22, tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)
    with pytest.raises(ApprovalError, match="token_invalid"):
        ctx.approvals.approve("bad token", tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)


def test_approve_tokens_not_minted_before_all_parts_delivered(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=CHAT, body_parts_total=3)
    ctx.approvals.set_card_message(approval, MSG)
    ctx.approvals.mark_part_delivered(approval)
    with pytest.raises(ApprovalError, match="body_not_delivered"):
        ctx.approvals.issue_card_tokens(approval)
    ctx.approvals.mark_part_delivered(approval)
    ctx.approvals.mark_part_delivered(approval)
    assert "approve" in ctx.approvals.issue_card_tokens(approval)


def test_approval_expires_after_ttl(ctx: AppContext, clock: FakeClock) -> None:
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    clock.advance(hours=72, seconds=1)
    with pytest.raises(ApprovalError, match="token_expired"):
        tap(ctx, tokens)
    assert ctx.approvals.expire_due() == [approval]
    assert status(ctx, approval) == "expired"


def test_confirm_ttl_five_minutes(ctx: AppContext, clock: FakeClock) -> None:
    _, action = packaged_contribution(ctx)
    _, tokens = card(ctx, action)
    confirm_token = tap(ctx, tokens)
    clock.advance(timedelta(seconds=301))
    with pytest.raises(ApprovalError, match="token_expired"):
        confirm(ctx, confirm_token)


def test_confirm_refused_while_paused(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    confirm_token = tap(ctx, tokens)
    write_flag(ctx.paths.paused_flag, {"reason": "test"})
    with pytest.raises(ApprovalError, match="confirm_refused"):
        confirm(ctx, confirm_token)
    assert status(ctx, approval) == "armed"  # approvals stay; confirms are refused


def test_cancel_disarms(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    result = ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)
    ctx.approvals.cancel(result.cancel_token, tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)
    assert status(ctx, approval) == "pending"
    with pytest.raises(ApprovalError, match="token_used"):
        confirm(ctx, result.confirm_token)


def test_reject(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    assert ctx.approvals.reject(tokens["reject"], tg_user_id=CHAT, chat_id=CHAT, message_id=MSG) == action
    assert status(ctx, approval) == "rejected"
    with pytest.raises(ApprovalError, match="token_used"):
        tap(ctx, tokens)


def test_tampering_with_artifacts_after_confirm_detected_at_execution(
    ctx: AppContext, artifacts: FakeArtifacts
) -> None:
    cid, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    confirm(ctx, tap(ctx, tokens))
    artifacts.hashes_by_rev[(cid, 1)] = ArtifactHashes("b" * 64, "e" * 64, "m" * 64)  # patch rewritten on disk
    with pytest.raises(Superseded):
        ctx.approvals.consume(approval)
    assert status(ctx, approval) == "invalidated"


# ---------------------------------------------------------------------------- invalidation
@pytest.mark.parametrize("from_dry_run", [True, False], ids=["dry_run_to_live", "live_to_dry_run"])
def test_dry_run_toggle_invalidates(ctx: AppContext, from_dry_run: bool) -> None:
    """AC 6: approve under one mode, toggle, the old confirm is superseded (both directions)."""
    if not from_dry_run:
        ctx.control.set_dry_run(False, "tg:4242")
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    confirm_token = tap(ctx, tokens)
    invalidated = ctx.control.set_dry_run(not from_dry_run, "tg:4242")
    assert [i.approval_id for i in invalidated] == [approval]
    assert status(ctx, approval) == "invalidated"
    with pytest.raises(ApprovalError, match="token_used"):
        confirm(ctx, confirm_token)


def test_dry_run_toggle_after_confirm_supersedes_before_write(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval, tokens = card(ctx, action)
    confirm(ctx, tap(ctx, tokens))
    ctx.control.set_dry_run(False, "tg:4242")
    with pytest.raises(ApprovalError):
        ctx.approvals.consume(approval)


def test_title_body_edit_invalidates(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    approval, _ = card(ctx, action)
    ctx.approvals.set_text_override(cid, title="New title  ", actor="tg:4242")
    assert status(ctx, approval) == "invalidated"
    row = db.one(ctx.conn, "SELECT pr_title_override FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row[0] == "New title"


def test_new_package_rev_invalidates_only_that_contribution(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    _, other_action = packaged_contribution(ctx)
    approval, _ = card(ctx, action)
    other, _ = card(ctx, other_action, message_id=777)
    ctx.approvals.invalidate_contribution(cid, "new_package_rev")
    assert status(ctx, approval) == "invalidated" and status(ctx, other) == "pending"


def test_new_config_snapshot_invalidates_all(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    _, action2 = packaged_contribution(ctx)
    a1, _ = card(ctx, action)
    a2, _ = card(ctx, action2, message_id=777)
    policy = ctx.paths.config_dir / "policy.yaml"
    policy.write_text(policy.read_text().replace("prs_per_24h: 2", "prs_per_24h: 1"))
    preview = ctx.snapshots.prepare_reload()
    _, invalidated = ctx.snapshots.apply_reload(preview.hash, "uid:1000/operator")
    assert {i.approval_id for i in invalidated} == {a1, a2}
    assert status(ctx, a1) == status(ctx, a2) == "invalidated"


def test_kill_invalidates_armed_and_confirmed_only(ctx: AppContext) -> None:
    _, a_pending = packaged_contribution(ctx)
    _, a_armed = packaged_contribution(ctx)
    _, a_confirmed = packaged_contribution(ctx)
    pending, _ = card(ctx, a_pending, message_id=1)
    armed, t2 = card(ctx, a_armed, message_id=2)
    tap(ctx, t2, message_id=2)
    confirmed, t3 = card(ctx, a_confirmed, message_id=3)
    confirm(ctx, tap(ctx, t3, message_id=3), message_id=3)
    ctx.control.kill("test", "tg:4242")
    assert status(ctx, pending) == "pending"
    assert status(ctx, armed) == status(ctx, confirmed) == "invalidated"


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        ({"title": "Line one\nline two"}, "title:bad_title"),
        ({"title": "x" * 257}, "title:too_long"),
        ({"title": "   "}, "title:bad_title"),
        ({"body": "no disclosure here"}, "body:missing_disclosure"),
        ({"body": "x" * 60001 + "\nAI assistance disclosure\n<!-- ossbot:cid=x -->"}, "body:too_long"),
        ({"title": "Fix for internal-codename-zeta"}, "title:confidential"),
        ({"title": "token ghp_" + "Q8xk2LmZ9vR4tYp7Wn3bJc6Hd1Fs5Ga0Ke8U"}, "title:secret_pattern"),
        ({"title": "pepper SECRETVALUE-0123456789"}, "title:gatekeeper_secret"),
    ],
)
def test_text_override_refused_before_storing(ctx: AppContext, edit: dict[str, str], code: str) -> None:
    """P1-4: an edited title/body that violates policy.check_text is refused and nothing is stored."""
    ctx.env = {"OSSBOT_TOKEN_PEPPER": "SECRETVALUE-0123456789"}
    ctx.reset_secret_scanner()
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO policy_floor (kind, value, added_at) VALUES ('confidentiality',"
                         " 'internal-codename-zeta', 'x')")  # fmt: skip
    cid, action = packaged_contribution(ctx)
    approval, _ = card(ctx, action, parts=2)
    before = ctx.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with pytest.raises(ApprovalError) as info:
        ctx.approvals.set_text_override(cid, actor="tg:4242", **edit)
    assert info.value.code == "text_refused" and code in str(info.value)
    assert "SECRETVALUE" not in str(info.value)
    row = db.one(ctx.conn, "SELECT pr_title_override, pr_body_override FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == (None, None)
    assert status(ctx, approval) == "pending"  # nothing invalidated either
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before


def test_approve_resend_rotates_tokens(ctx: AppContext) -> None:
    """N4: after /approve <cid> the old card's tokens are dead; new ones bind to the new message."""
    cid, action = packaged_contribution(ctx)
    old, old_tokens = card(ctx, action)
    [new] = ctx.approvals.reissue(cid, tg_chat_id=CHAT, actor="tg:4242", part_count=lambda a: 1)
    assert status(ctx, old) == "invalidated"
    with pytest.raises(ApprovalError, match="token_used"):
        tap(ctx, old_tokens)
    ctx.approvals.set_card_message(new, 900)
    ctx.approvals.mark_part_delivered(new)
    new_tokens = ctx.approvals.issue_card_tokens(new)
    with pytest.raises(ApprovalError, match="token_wrong_message"):
        ctx.approvals.approve(new_tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=MSG)
    assert tap(ctx, new_tokens, message_id=900)
