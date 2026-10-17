"""Deterministic policy checks (DESIGN §12.6, §22.1)."""

from __future__ import annotations

import pytest

from ossbot.common.config import ConfigError, resolve_target
from ossbot.common.hashing import sha256_text
from ossbot.gatekeeper import policy
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.policy import DiffEntry, RepoState, Violation

DISCLOSURE = "\n---\n<details><summary>🤖 AI assistance disclosure</summary>...</details>\n<!-- ossbot:cid=c_X -->"


def codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        (".github/workflows/ci.yml", True),
        (".github/actions/x/action.yml", True),
        ("CODEOWNERS", True),
        ("docs/CODEOWNERS", True),
        ("LICENSE", True),
        ("LICENSE.md", True),
        ("sub/LICENSE", True),
        ("COPYING", True),
        ("certs/server.pem", True),
        ("a/b/c.key", True),
        (".env", True),
        ("svc/.env.local", True),
        (".kiro/settings/mcp.json", True),
        (".git/hooks/pre-commit", True),
        ("src/main.py", False),
        ("docs/license-faq.md", False),
        ("github/workflows/x.yml", False),
        ("keys.md", False),
    ],
)
def test_forbidden_paths(ctx: AppContext, path: str, forbidden: bool) -> None:
    result = policy.check_diff([DiffEntry(path, 1, 0)], ctx.config.policy)
    assert ("forbidden_path" in codes(result)) is forbidden


def test_floor_survives_config_without_workflows(ctx: AppContext) -> None:
    custom = ctx.config.policy.model_copy(
        update={"diff": ctx.config.policy.diff.model_copy(update={"forbidden_paths": []})}
    )
    assert "forbidden_path" in codes(policy.check_diff([DiffEntry(".github/workflows/x.yml", 1, 0)], custom))
    assert policy.effective_forbidden_paths(custom) == policy.FORBIDDEN_PATHS_FLOOR


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        (DiffEntry("img.png", None, None), "binary"),
        (DiffEntry("link", 1, 0, new_mode="120000"), "symlink"),
        (DiffEntry("vendor/sub", 1, 1, old_mode="160000", new_mode="160000"), "submodule"),
    ],
)
def test_binary_symlink_submodule(ctx: AppContext, entry: DiffEntry, code: str) -> None:
    assert code in codes(policy.check_diff([entry], ctx.config.policy))


def test_max_files_and_lines(ctx: AppContext) -> None:
    many = [DiffEntry(f"f{i}.py", 1, 0) for i in range(26)]
    assert "too_many_files" in codes(policy.check_diff(many, ctx.config.policy))
    big = [DiffEntry("a.py", 500, 101)]
    assert "too_many_lines" in codes(policy.check_diff(big, ctx.config.policy))
    target = resolve_target(ctx.config.targets, "langfuse/langfuse")
    assert target is not None
    medium = [DiffEntry("a.py", 401, 0)]  # within policy 600 but over the target's 400
    assert codes(policy.check_diff(medium, ctx.config.policy)) == set()
    assert "too_many_lines" in codes(policy.check_diff(medium, ctx.config.policy, target))


@pytest.mark.parametrize(
    "secret",
    [
        "ksk_" + "A" * 20,
        "ghp_" + "a" * 36,
        "github_pat_" + "a" * 60,
        "123456789:" + "A" * 35,
    ],
)
@pytest.mark.parametrize("kind", ["title", "body", "comment", "commit_message"])
def test_secret_patterns(ctx: AppContext, secret: str, kind: str) -> None:
    text = f"see {secret}" + (DISCLOSURE if kind == "body" else "")
    assert "secret_pattern" in codes(policy.check_text(kind, text, ctx.config, set()))  # type: ignore[arg-type]


def test_kiro_key_hash_match_on_token_like_string(ctx: AppContext) -> None:
    key = "kiro-dedicated-key-value-0123456789"
    found = policy.check_text("comment", f"oops {key} here", ctx.config, set(), sha256_text(key))
    assert "kiro_key" in codes(found)
    assert "kiro_key" not in codes(policy.check_text("comment", "nothing here", ctx.config, set(), sha256_text(key)))


@pytest.mark.parametrize("kind", ["title", "body", "comment", "commit_message"])
@pytest.mark.parametrize("value", ["test-work-login", "someone@corp.example", "TEST-WORK-LOGIN"])
def test_confidentiality_floor_in_every_text(ctx: AppContext, kind: str, value: str) -> None:
    conf = policy.effective_confidentiality(ctx.conn, ctx.config)
    text = f"mention {value}" + (DISCLOSURE if kind == "body" else "")
    assert "confidential" in codes(policy.check_text(kind, text, ctx.config, conf))  # type: ignore[arg-type]


def test_missing_disclosure(ctx: AppContext) -> None:
    assert "missing_disclosure" in codes(policy.check_text("body", "just a body", ctx.config, set()))
    assert "missing_disclosure" not in codes(policy.check_text("body", "body" + DISCLOSURE, ctx.config, set()))


def test_text_limits(ctx: AppContext) -> None:
    assert "too_long" in codes(policy.check_text("title", "x" * 257, ctx.config, set()))
    assert "bad_title" in codes(policy.check_text("title", "a\nb", ctx.config, set()))
    assert "subject_too_long" in codes(policy.check_text("commit_message", "s" * 101 + "\n\nbody", ctx.config, set()))
    assert "too_long" in codes(policy.check_text("commit_message", "é" * 5001, ctx.config, set()))  # bytes, not chars
    assert "too_long" in codes(policy.check_text("comment", "x" * 20001, ctx.config, set()))


def test_repo_checks(ctx: AppContext) -> None:
    target = resolve_target(ctx.config.targets, "langfuse/langfuse")
    logins = {"test-work-login"}
    assert codes(policy.check_repo("langfuse/langfuse", target, RepoState(ai_policy="allowed"), logins)) == set()
    assert "not_a_target" in codes(policy.check_repo("x/y", None, RepoState(), logins))
    assert "repo_disabled" in codes(
        policy.check_repo("langfuse/langfuse", target, RepoState("allowed", db_enabled=False), logins)
    )
    assert "ai_policy_banned" in codes(policy.check_repo("langfuse/langfuse", target, RepoState("banned"), logins))
    assert "ai_policy_unclear" in codes(policy.check_repo("langfuse/langfuse", target, RepoState("unclear"), logins))
    allowed = RepoState("unclear", ai_override_allowed=True)
    assert codes(policy.check_repo("langfuse/langfuse", target, allowed, logins)) == set()
    assert "cla_required" in codes(
        policy.check_repo("langfuse/langfuse", target, RepoState("allowed", cla_required=True), logins)
    )
    assert "forbidden_owner" in codes(policy.check_repo("test-work-login/x", target, RepoState("allowed"), logins))


def test_coder_mode_single_rejects_snapshot(ctx: AppContext) -> None:
    path = ctx.paths.config_dir / "policy.yaml"
    path.write_text(path.read_text().replace("coder_mode: split", "coder_mode: single"))
    with pytest.raises(ConfigError, match="phase-2"):
        ctx.snapshots.prepare_reload()
