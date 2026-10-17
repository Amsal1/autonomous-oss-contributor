"""Config schema accept/reject (extra=forbid, strict, v1-only flags) and the shipped files."""

from __future__ import annotations

import pytest

from ossbot.common.config import (
    ConfigError,
    EgressConfig,
    ModelsConfig,
    PolicyConfig,
    ProfileConfig,
    TargetsConfig,
    parse_bundle,
    parse_models,
    parse_yaml_model,
    resolve_target,
)
from tests.conftest import FIXTURES, REPO_ROOT

CONFIG = REPO_ROOT / "config"


def load(name: str) -> bytes:
    return (CONFIG / name).read_bytes()


def bundle_files() -> dict[str, bytes]:
    return {
        "policy.yaml": load("policy.yaml"),
        "targets.yaml": load("targets.yaml"),
        "egress.yaml": load("egress.yaml"),
        "profile.yaml": (FIXTURES / "profile.test.yaml").read_bytes(),
    }


def test_shipped_config_files_validate() -> None:
    bundle = parse_bundle(bundle_files())
    assert bundle.policy.dry_run_default is True
    assert bundle.policy.sandbox.coder_mode == "split"
    assert parse_models(load("models.yaml")).default.model


def test_profile_example_validates_with_placeholders_only() -> None:
    profile = parse_yaml_model(ProfileConfig, "profile.yaml", load("profile.example.yaml"))
    assert profile.github_login == "your-github-login"
    assert profile.telegram.user_id == 0
    assert profile.commit_email.endswith("@example.com")


def test_targets_seed_resolves_defaults() -> None:
    targets = parse_yaml_model(TargetsConfig, "targets.yaml", load("targets.yaml"))
    langfuse = resolve_target(targets, "langfuse/langfuse")
    assert langfuse is not None and langfuse.enabled and langfuse.priority == 1
    assert "good first issue" in langfuse.labels
    provider = resolve_target(targets, "tobenwarrior/hermes-kiro-provider")
    assert provider is not None and provider.discovery is False
    assert resolve_target(targets, "LineageOS/android") is None  # denylist
    assert resolve_target(targets, "nobody/unknown") is None


@pytest.mark.parametrize(
    ("model", "text", "needle"),
    [
        (PolicyConfig, "version: 1\nunknown_key: 1\n", "unknown_key"),
        (PolicyConfig, "version: 2\n", "version"),
        (PolicyConfig, "version: 1\nsandbox: {coder_mode: single, coder_single_mode_evidence: x}\n", "phase-2"),
        (PolicyConfig, "version: 1\ndry_run_default: false\n", "dry_run_default"),
        (PolicyConfig, "version: 1\nlimits: {prs_per_24h: '2'}\n", "prs_per_24h"),  # strict: no str->int
        (PolicyConfig, "version: 1\nlimits: {prs_per_24h: -1}\n", "prs_per_24h"),
        (PolicyConfig, "version: 1\nvalidation: {stack: {enabled: true}}\n", "enabled"),
        (PolicyConfig, "version: 1\ndiscovery: {proactive: true}\n", "proactive"),
        (PolicyConfig, "version: 1\ntelegram: {mode: dual}\n", "mode"),
        (PolicyConfig, "version: 1\nsecrets: {extra_patterns: ['(']}\n", "invalid regex"),
        (
            TargetsConfig,
            "version: 1\ncategories: [{id: a, priority: 1, why: x, repos: [{repo: 'bad repo'}]}]\n",
            "repo",
        ),
        (
            TargetsConfig,
            "version: 1\ncategories: [{id: a, priority: 1, why: x, repos: [{repo: a/b}, {repo: A/B}]}]\n",
            "duplicate",
        ),
        (
            TargetsConfig,
            "version: 1\ncategories: [{id: a, priority: 1, why: x, repos: [{repo: x/y}]}]\n"
            "denylist: [{pattern: 'x/*'}]\n",
            "denylist",
        ),
        (TargetsConfig, "version: 1\ndefaults: {stack_validation: kind}\ncategories: []\n", "stack_validation"),
        (ModelsConfig, "version: 1\ndefault: {model: m}\nroles: {}\n", "roles"),
    ],
)
def test_schema_rejects(model: type, text: str, needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_yaml_model(model, "f.yaml", text.encode())
    assert needle in str(info.value)


def test_duplicate_yaml_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="YAML"):
        parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\nversion: 1\n")


def test_profile_rejects_login_in_forbidden_list() -> None:
    text = (FIXTURES / "profile.test.yaml").read_text().replace("forbidden_logins: [test-work-login]",
                                                                "forbidden_logins: [test-bot-login]")  # fmt: skip
    with pytest.raises(ConfigError, match="forbidden_logins"):
        parse_yaml_model(ProfileConfig, "profile.yaml", text.encode())


def test_models_unknown_role_rejected() -> None:
    text = load("models.yaml").decode().replace("roles:\n", "roles:\n  extra_role: {model: default}\n", 1)
    with pytest.raises(ConfigError, match="extra_role"):
        parse_models(text.encode())


def test_egress_rejects_unknown_category() -> None:
    text = load("egress.yaml").decode().replace("agent-llm: [kiro_core]", "agent-llm: [kiro_cor]")
    with pytest.raises(ConfigError, match="kiro_cor"):
        parse_yaml_model(EgressConfig, "egress.yaml", text.encode())


def test_bundle_collects_errors_per_file() -> None:
    files = bundle_files()
    files["policy.yaml"] = b"version: 1\nbogus: 1\n"
    del files["egress.yaml"]
    with pytest.raises(ConfigError) as info:
        parse_bundle(files)
    assert set(info.value.errors) == {"policy.yaml", "egress.yaml"}


def test_profile_example_has_no_personal_values() -> None:
    """Placeholders only: every scalar is an obvious placeholder (FR-16)."""
    text = load("profile.example.yaml").decode()
    for needle in ("your-", "Your Name", "example", "age1replace"):
        assert needle in text
