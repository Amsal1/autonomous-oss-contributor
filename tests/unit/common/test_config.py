"""Config schema accept/reject (extra=forbid, strict, v1-only flags) and the shipped files."""

from __future__ import annotations

import pytest

from ossbot.common.config import (
    ConfigError,
    EgressConfig,
    ModelsConfig,
    PolicyConfig,
    ProfileConfig,
    RetentionPolicy,
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


def test_shipped_policy_size_blocks() -> None:
    p = parse_bundle(bundle_files()).policy
    assert (p.diff.max_files, p.diff.max_changed_lines, p.diff.max_test_lines) == (80, 3000, 3000)
    assert p.size.autonomous_max_class == "s" and p.size.preferred_classes == ["xs", "s"]
    assert p.size.overrun_tolerance == 0.5 and p.size.on_overrun == "flag"
    assert "**/package-lock.json" in p.size.exclude_globs and "**/tests/**" in p.size.test_globs
    assert p.discovery_filters.validation_tier_max == "T1"
    assert p.proposal.ttl_hours == 72 and p.proposal.claim_comment == "off"
    assert p.discovery.proposals_per_cycle == 3
    assert p.hermes.capability_tier_default == "self_improve"  # §15.5, PLAN item 23a
    # the shipped file matches the model defaults (the §9.14 values)
    assert p.size == PolicyConfig(version=1).size
    assert p.discovery_filters == PolicyConfig(version=1).discovery_filters
    assert p.proposal == PolicyConfig(version=1).proposal


def test_retention_defaults() -> None:
    """§18.10: the shipped retention block equals the model defaults (time-based only, no size caps)."""
    shipped = parse_bundle(bundle_files()).policy.retention
    assert shipped == PolicyConfig(version=1).retention == RetentionPolicy()
    assert (shipped.contributions_days, shipped.telegram_rows_days, shipped.run_logs_days) == (30, 90, 30)
    assert (shipped.hermes_sessions_days, shipped.tmp_days) == (90, 7)
    assert (shipped.docker_build_cache_hours, shipped.disk_warn_gib) == (168, 40)


def test_retention_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="journal_max_mb"):
        parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\nretention: {journal_max_mb: 500}\n")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("contributions_days", 6), ("contributions_days", 3651), ("telegram_rows_days", 29),
        ("run_logs_days", 6), ("hermes_sessions_days", 6), ("tmp_days", 0), ("tmp_days", 366),
        ("docker_build_cache_hours", 23), ("docker_build_cache_hours", 8761), ("disk_warn_gib", 4),
        ("disk_warn_gib", 1025), ("contributions_days", "30"),
    ],
)  # fmt: skip
def test_retention_bounds(key: str, bad: object) -> None:
    text = f"version: 1\nretention: {{{key}: {bad!r}}}\n".replace("'", '"')
    with pytest.raises(ConfigError, match=key):
        parse_yaml_model(PolicyConfig, "policy.yaml", text.encode())


def test_xl_fits_the_default_ceiling() -> None:
    policy = parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\nsize: {autonomous_max_class: xl}\n")
    assert policy.size.autonomous_max_class == "xl"


def test_repo_size_class_resolution() -> None:
    text = (
        "version: 1\ndefaults: {size_class: m}\ncategories: [{id: a, priority: 1, why: x, repos: ["
        "{repo: a/one}, {repo: a/two, size_class: xs}, {repo: a/three, size_class: inherit}]}]\n"
    )
    targets = parse_yaml_model(TargetsConfig, "targets.yaml", text.encode())
    sizes = {r: getattr(resolve_target(targets, r), "size_class", "?") for r in ("a/one", "a/two", "a/three")}
    assert sizes == {"a/one": "m", "a/two": "xs", "a/three": None}


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
        # CR4-2o: the per-target line cap is gone (extra=forbid), size_class is a preference
        (TargetsConfig, "version: 1\ndefaults: {max_changed_lines: 400}\ncategories: []\n", "max_changed_lines"),
        (
            TargetsConfig,
            "version: 1\ncategories: [{id: a, priority: 1, why: x, repos: [{repo: a/b, max_changed_lines: 9}]}]\n",
            "max_changed_lines",
        ),
        (
            TargetsConfig,
            "version: 1\ncategories: [{id: a, priority: 1, why: x, repos: [{repo: a/b, size_class: xxl}]}]\n",
            "size_class",
        ),
        (PolicyConfig, "version: 1\nsize: {autonomous_max_class: xxl}\n", "autonomous_max_class"),
        (PolicyConfig, "version: 1\nsize: {on_overrun: block}\n", "on_overrun"),
        (PolicyConfig, "version: 1\nsize: {bogus: 1}\n", "bogus"),
        (PolicyConfig, "version: 1\ndiscovery_filters: {validation_tier_max: T3}\n", "validation_tier_max"),
        (PolicyConfig, "version: 1\nproposal: {claim_comment: maybe}\n", "claim_comment"),
        (PolicyConfig, "version: 1\nhermes: {capability_tier_default: god}\n", "capability_tier_default"),
        # the validator: a class whose bounds exceed the ceiling
        (PolicyConfig, "version: 1\ndiff: {max_changed_lines: 200}\nsize: {autonomous_max_class: m}\n", "ceiling"),
        (PolicyConfig, "version: 1\ndiff: {max_files: 10}\nsize: {autonomous_max_class: l}\n", "ceiling"),
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
