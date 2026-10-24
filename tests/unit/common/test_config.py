"""Config schema accept/reject (extra=forbid, strict, v1-only flags) and the shipped files."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from ossbot.common.config import (
    DEFAULT_OPEN_TOPICS,
    DEFAULT_PREFERRED_LANGUAGES,
    KEPT_PRIORITY,
    OPEN_CATEGORY,
    OPEN_PRIORITY,
    AiPolicyRules,
    ConfigError,
    EgressConfig,
    Limits,
    ModelsConfig,
    PolicyConfig,
    ProfileConfig,
    RetentionPolicy,
    TargetsConfig,
    discovered_target,
    effective_ai_rules,
    helm_hosts,
    parse_bundle,
    parse_models,
    parse_yaml_model,
    resolve_target,
)
from ossbot.common.project import PROJECT_URL
from ossbot.common.yamlio import safe_load
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


def test_ci_on_pr_ranking_keys() -> None:
    """Post-v1 (§9.4): the CI boost and the facts TTL are config-only, bounded, and shipped at the defaults."""
    shipped = parse_bundle(bundle_files()).policy.discovery
    assert (shipped.ci_on_pr_weight, shipped.ci_facts_ttl_hours) == (6.0, 168)
    assert shipped == PolicyConfig(version=1).discovery
    for bad in ("ci_on_pr_weight: 31", "ci_on_pr_weight: -1", "ci_facts_ttl_hours: 0", "ci_facts_ttl_hours: 721"):
        with pytest.raises(ConfigError):
            parse_yaml_model(PolicyConfig, "policy.yaml", f"version: 1\ndiscovery: {{{bad}}}\n".encode())


def test_retention_defaults() -> None:
    """§18.10: the shipped retention block equals the model defaults (time-based only, no size caps)."""
    shipped = parse_bundle(bundle_files()).policy.retention
    assert shipped == PolicyConfig(version=1).retention == RetentionPolicy()
    assert (shipped.contributions_days, shipped.telegram_rows_days, shipped.run_logs_days) == (30, 90, 30)
    assert (shipped.hermes_sessions_days, shipped.tmp_days) == (90, 7)
    assert (shipped.docker_build_cache_hours, shipped.disk_warn_gib) == (168, 40)
    assert shipped.proxy_logs_days == 30


def test_retention_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="journal_max_mb"):
        parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\nretention: {journal_max_mb: 500}\n")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("contributions_days", 6), ("contributions_days", 3651), ("telegram_rows_days", 29),
        ("run_logs_days", 6), ("hermes_sessions_days", 6), ("tmp_days", 0), ("tmp_days", 366),
        ("docker_build_cache_hours", 23), ("docker_build_cache_hours", 8761), ("disk_warn_gib", 4),
        ("disk_warn_gib", 1025), ("contributions_days", "30"), ("proxy_logs_days", 29),
        ("proxy_logs_days", 3651),
    ],
)  # fmt: skip
def test_retention_bounds(key: str, bad: object) -> None:
    text = f"version: 1\nretention: {{{key}: {bad!r}}}\n".replace("'", '"')
    with pytest.raises(ConfigError, match=key):
        parse_yaml_model(PolicyConfig, "policy.yaml", text.encode())


def test_vet_ai_policy_defaults() -> None:
    """§9.5 (post-v1): skip only on an explicit ban; the shipped block equals the defaults."""
    shipped = parse_bundle(bundle_files()).policy.vet.ai_policy
    assert shipped == PolicyConfig(version=1).vet.ai_policy == AiPolicyRules()
    assert (shipped.unstated, shipped.unclear, shipped.banned) == ("allow", "allow", "skip")


def test_project_url_default_and_validation() -> None:
    """§12.3 (post-v1): the PR footer link; an https URL without quotes or markup (it lands in an href)."""
    assert PolicyConfig(version=1).text.project_url == PROJECT_URL
    assert PROJECT_URL.startswith("https://github.com/") and PROJECT_URL.endswith("/autonomous-oss-contributor")
    fork = parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\ntext: {project_url: 'https://example.org/f'}\n")
    assert fork.text.project_url == "https://example.org/f"
    for bad in ('https://x.org/"onclick', "http://x.org/a", "javascript:alert(1)", "https://x.org/<b>"):
        with pytest.raises(ConfigError):
            parse_yaml_model(PolicyConfig, "policy.yaml", f"version: 1\ntext: {{project_url: '{bad}'}}\n".encode())


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("vet: {ai_policy: {banned: allow}}", "banned"),
        ("vet: {ai_policy: {unclear: maybe}}", "unclear"),
        ("vet: {ai_policy: {silent: allow}}", "silent"),
        ("vet: {bogus: 1}", "bogus"),
    ],
)
def test_vet_ai_policy_rejects(text: str, needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        parse_yaml_model(PolicyConfig, "policy.yaml", f"version: 1\n{text}\n".encode())


def _targets(repos: str) -> TargetsConfig:
    text = f"version: 1\ncategories: [{{id: a, priority: 1, why: x, repos: [{repos}]}}]\n"
    return parse_yaml_model(TargetsConfig, "targets.yaml", text.encode())


def test_repo_ai_policy_override_rejects_banned() -> None:
    with pytest.raises(ConfigError, match="banned"):
        _targets("{repo: a/one, ai_policy: {banned: skip}}")


def test_effective_ai_rules_per_repo() -> None:
    targets = _targets(
        "{repo: a/one}, {repo: a/two, ai_policy: {unclear: skip}}, {repo: a/three, ai_policy_override: allowed}"
    )
    strict = parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\nvet: {ai_policy: {unstated: skip}}\n")
    default = PolicyConfig(version=1)
    assert effective_ai_rules(default, resolve_target(targets, "a/one")) == AiPolicyRules()
    assert effective_ai_rules(default, None) == AiPolicyRules()
    two = effective_ai_rules(default, resolve_target(targets, "a/two"))
    assert (two.unstated, two.unclear) == ("allow", "skip")
    assert effective_ai_rules(strict, resolve_target(targets, "a/one")).unstated == "skip"
    three = effective_ai_rules(strict, resolve_target(targets, "a/three"))
    assert (three.unstated, three.unclear, three.banned) == ("allow", "allow", "skip")


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


def test_owner_fix_rounds_max() -> None:
    """ADR-0003 addendum (2026-10-23): a separate owner cap, default 5, in the policy file too."""
    assert Limits().owner_fix_rounds_max == 5
    policy = parse_yaml_model(PolicyConfig, "policy.yaml", load("policy.yaml"))
    assert policy.limits.owner_fix_rounds_max == 5 and policy.limits.watch_rounds_max == 3


def test_network_reruns_max() -> None:
    """ADR-0024: default 1, range 0-3, in the policy file too; 4 is refused."""
    assert Limits().network_reruns_max == 1
    policy = parse_yaml_model(PolicyConfig, "policy.yaml", load("policy.yaml"))
    assert policy.limits.network_reruns_max == 1
    for value in (0, 3):
        assert Limits(network_reruns_max=value).network_reruns_max == value
    for bad in (-1, 4):
        with pytest.raises(ValidationError):
            Limits(network_reruns_max=bad)


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
        (PolicyConfig, "version: 1\nlimits: {owner_fix_rounds_max: -1}\n", "owner_fix_rounds_max"),
        (PolicyConfig, "version: 1\nlimits: {owner_fix_rounds: 5}\n", "owner_fix_rounds"),  # extra=forbid
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


def test_shipped_egress_keeps_mode_default_open() -> None:
    """O7 guard: this run ships sandbox_open/sandbox_full selectable only; the owner switches with /egress."""
    egress = parse_yaml_model(EgressConfig, "egress.yaml", load("egress.yaml"))
    assert egress.mode_default == "open"
    assert list(egress.modes) == ["allowlist", "research", "open", "sandbox_open", "sandbox_full"]
    grants = egress.modes["sandbox_open"]
    assert grants["fetch"] == grants["validate"] == ["web_public"]
    assert egress.modes["sandbox_full"] == grants  # ADR-0025: the same squid rules
    for mode in ("allowlist", "research", "open"):
        assert "fetch" not in egress.modes[mode] and "validate" not in egress.modes[mode]
    assert (egress.anomaly.max_hosts, egress.anomaly.max_upload_mb, egress.anomaly.max_download_mb) == (60, 50, 6144)


def test_shipped_egress_floors_of_adr_0025() -> None:
    """The three IPv6 floors, notify_only (a subset of the always-denied names) and the repo-code deny list."""
    egress = parse_yaml_model(EgressConfig, "egress.yaml", load("egress.yaml"))
    assert {"ff00::/8", "64:ff9b::/96", "2002::/16"} <= set(egress.always_denied.cidrs)
    assert egress.always_denied.notify_only == ["checkpoint-api.hashicorp.com"]
    assert {"api.telegram.org", ".telegram.org", "t.me", ".kiro.dev", "q.us-east-1.amazonaws.com"} <= set(
        egress.code_denied
    )
    assert "api.github.com" not in egress.code_denied  # github_read: repo code holds no token (I5 core)


def test_shipped_egress_toolchain_hosts_are_base_hosts_of_repo_code_only() -> None:
    """build-env design §6.5: pkg_toolchains and static.rust-lang.org in the fetch and validate base lists (so the
    strict pool gets them too, in every mode); never for the key-holding profiles."""
    egress = parse_yaml_model(EgressConfig, "egress.yaml", load("egress.yaml"))
    assert egress.categories["pkg_toolchains"] == ["mise-java.jdx.dev", "api.foojay.io", "cdn.azul.com", "corretto.aws",
                                                   "download.bell-sw.com", "download.java.net"]  # fmt: skip
    assert "static.rust-lang.org" in egress.categories["pkg_rust"]
    for profile in ("fetch", "validate"):
        assert "pkg_toolchains" in egress.sandbox_profiles[profile] and "pkg_rust" in egress.sandbox_profiles[profile]
    for profile in ("research", "agent-llm", "catalog"):
        assert not {"pkg_toolchains", "pkg_rust"} & set(egress.sandbox_profiles[profile]), profile
    assert "mise-versions.jdx.dev" not in {h for hosts in egress.categories.values() for h in hosts}  # M3
    for mode in egress.modes.values():
        assert not {"pkg_toolchains"} & {c for cats in mode.values() for c in cats}


def _old_snapshot_egress() -> str:
    """egress.yaml as written before ADR-0025: no sandbox_full, no code_denied, no notify_only, old CIDRs."""
    text = load("egress.yaml").decode()
    start = text.index("  # ADR-0025: the same grants as sandbox_open")
    end = text.index("# Per-job egress limits")
    text = text[:start] + text[end:]
    text = text[: text.index("# ADR-0025: names no repo-code")]
    text = re.sub(r"\n  # ADR-0025: the one always-denied name.*?notify_only: \[[^\]]*\]\n", "\n", text, flags=re.S)
    return text.replace(', "ff00::/8", "64:ff9b::/96", "2002::/16"', "")


def test_old_snapshot_without_the_new_keys_still_parses() -> None:
    """A snapshot written before ADR-0025 parses (the owner's /reload comes after the deploy): sandbox_full
    gets sandbox_open's grants, code_denied is empty, notify_only is absent."""
    text = _old_snapshot_egress()
    data = safe_load(text)
    assert "sandbox_full" not in data["modes"] and "code_denied" not in data
    assert "notify_only" not in data["always_denied"] and "ff00::/8" not in data["always_denied"]["cidrs"]
    egress = parse_yaml_model(EgressConfig, "egress.yaml", text.encode())
    assert "sandbox_full" not in egress.modes
    assert egress.mode_grants("sandbox_full") == egress.mode_grants("sandbox_open")
    assert egress.repo_code_web("sandbox_full") and egress.repo_code_web("sandbox_open")
    assert not egress.repo_code_web("open")
    assert egress.code_denied == [] and egress.always_denied.notify_only is None


def test_sandbox_full_must_equal_sandbox_open() -> None:
    text = load("egress.yaml").decode()
    full = text.index("  sandbox_full: {research: [web_public], hermes: [web_public], agent-llm: [web_public]")
    bad = text[:full] + text[full:].replace("catalog: [web_public],\n", "\n", 1)
    with pytest.raises(ConfigError, match="sandbox_full_must_equal_sandbox_open"):
        parse_yaml_model(EgressConfig, "egress.yaml", bad.encode())


def test_notify_only_must_be_an_always_denied_name() -> None:
    text = load("egress.yaml").decode().replace("notify_only: [checkpoint-api.hashicorp.com]",
                                                "notify_only: [checkpoint-api.hashicorp.com, example.com]")  # fmt: skip
    with pytest.raises(ConfigError, match="notify_only must be a subset"):
        parse_yaml_model(EgressConfig, "egress.yaml", text.encode())


SANDBOX_OPEN_LINE = (
    "  sandbox_open: {research: [web_public], hermes: [web_public], agent-llm: [web_public], catalog: [web_public],\n"
    "                 fetch: [web_public], validate: [web_public]}\n"
)


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("  open: {research: [web_public],",
         "  open: {fetch: [web_public], validate: [web_public], research: [web_public],",
         "only in egress modes sandbox_open/sandbox_full"),
        (SANDBOX_OPEN_LINE, SANDBOX_OPEN_LINE.replace("fetch: [web_public]", "fetch: [pkg_python]"), "web_public only"),
        (SANDBOX_OPEN_LINE,
         SANDBOX_OPEN_LINE.replace("fetch: [web_public], validate", "fetch: [web_public, pkg_python], validate"),
         "web_public only"),
        (SANDBOX_OPEN_LINE, SANDBOX_OPEN_LINE.replace(", validate: [web_public]", ""), "same list"),
        (SANDBOX_OPEN_LINE, SANDBOX_OPEN_LINE.replace("validate: [web_public]", "validate: []"), "web_public only"),
        ("  allowlist: {}", "  allowlist: {validate: [web_public], fetch: [web_public]}", "not allowlist"),
    ],
)  # fmt: skip
def test_egress_repo_code_grants_only_web_public_in_sandbox_open(old: str, new: str, match: str) -> None:
    text = load("egress.yaml").decode()
    assert old in text
    with pytest.raises(ConfigError, match=match):
        parse_yaml_model(EgressConfig, "egress.yaml", text.replace(old, new, 1).encode())


def test_egress_requires_the_sandbox_open_mode() -> None:
    text = load("egress.yaml").decode().replace(SANDBOX_OPEN_LINE, "")
    with pytest.raises(ConfigError, match="sandbox_open"):
        parse_yaml_model(EgressConfig, "egress.yaml", text.encode())


@pytest.mark.parametrize(
    ("key", "bad"),
    [("max_hosts", 4), ("max_hosts", 1001), ("max_upload_mb", 0), ("max_upload_mb", 10_241),
     ("max_download_mb", 63), ("max_download_mb", 102_401), ("bogus", 1)],
)  # fmt: skip
def test_egress_anomaly_bounds(key: str, bad: int) -> None:
    text = load("egress.yaml").decode()
    old = "anomaly: {max_hosts: 60, max_upload_mb: 50, max_download_mb: 6144}"
    assert old in text
    with pytest.raises(ConfigError, match=key):
        parse_yaml_model(EgressConfig, "egress.yaml", text.replace(old, f"anomaly: {{{key}: {bad}}}").encode())


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


# ------------------------------------------------------------- post-v1 open discovery (design §3.1, §8.5)
def test_open_discovery_defaults_equal_the_spec() -> None:
    p = PolicyConfig(version=1).discovery.open
    assert p.enabled is True
    assert len(p.topics) == 15 and p.topics[:2] == ["devops", "platform-engineering"]
    assert "llm-observability" in p.topics and "gitops" in p.topics
    assert (p.min_stars, p.pushed_within_days, p.new_repos_per_cycle) == (1000, 60, 5)
    assert p.languages_preferred == list(DEFAULT_PREFERRED_LANGUAGES) and len(p.languages_preferred) == 8
    assert "Go" in p.languages_preferred and "CloudFormation" in p.languages_preferred
    assert (p.topics_per_cycle, p.reserved_slots, p.repo_cooldown_days, p.search_budget) == (3, 1, 14, 10)
    assert p.language_weight == 6.0
    assert p.issue_labels == ["good first issue", "help wanted", "bug", "documentation", "docs"]
    assert tuple(p.topics) == DEFAULT_OPEN_TOPICS
    # the shipped block equals the defaults (one comment per key in config/policy.yaml)
    assert parse_bundle(bundle_files()).policy.discovery.open == p


@pytest.mark.parametrize(
    ("block", "needle"),
    [
        ("{bogus: 1}", "discovery.open.bogus"),
        ("{topics: [Bad_Topic]}", "discovery.open.topics.0"),
        ("{topics: [gitops, GitOps]}", "discovery.open.topics"),
        ("{topics: [gitops, gitops]}", "topics must be unique"),
        ("{topics: []}", "discovery.open.topics"),
        ("{topics: [" + "a" * 51 + "]}", "discovery.open.topics.0"),
        ("{languages_preferred: [Go, go]}", "case-insensitive"),
        ("{languages_preferred: ['9lang']}", "discovery.open.languages_preferred.0"),
        ("{issue_labels: ['a,b']}", "discovery.open.issue_labels.0"),
        ("{min_stars: 99}", "discovery.open.min_stars"),
        ("{search_budget: 5}", "search_budget 5 must be >="),  # 3 + 1 + 2 = 6
        ("{topics_per_cycle: 8, search_budget: 10}", "search_budget 10"),
        ("{new_repos_per_cycle: 21}", "discovery.open.new_repos_per_cycle"),
    ],
)
def test_open_discovery_rejects(block: str, needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_yaml_model(PolicyConfig, "policy.yaml", f"version: 1\ndiscovery: {{open: {block}}}\n".encode())
    assert needle in str(info.value)


def test_default_branch_age_gate_key() -> None:
    """Owner addition (2026-10-21): discovery.max_default_branch_age_days, 180 by default, bounded."""
    assert PolicyConfig(version=1).discovery.max_default_branch_age_days == 180
    assert parse_bundle(bundle_files()).policy.discovery.max_default_branch_age_days == 180
    for bad in ("29", "3651", "'180'"):
        with pytest.raises(ConfigError, match="max_default_branch_age_days"):
            parse_yaml_model(PolicyConfig, "policy.yaml",
                             f"version: 1\ndiscovery: {{max_default_branch_age_days: {bad}}}\n".encode())  # fmt: skip


def test_target_proposals_per_day() -> None:
    """Discovery fix 7: default 2, 0..50, never above ``proposal.max_per_day``, config-only (extra=forbid)."""
    shipped = parse_bundle(bundle_files()).policy
    assert shipped.discovery.target_proposals_per_day == 2 == PolicyConfig(version=1).discovery.target_proposals_per_day
    too_many = b"version: 1\nproposal: {max_per_day: 5}\ndiscovery: {target_proposals_per_day: 6}\n"
    with pytest.raises(ConfigError, match=r"target_proposals_per_day 6 exceeds proposal\.max_per_day 5"):
        parse_yaml_model(PolicyConfig, "policy.yaml", too_many)
    for bad in (b"target_proposals_per_day: -1", b"target_proposals_per_day: 51", b"target_proposal_per_day: 1"):
        with pytest.raises(ConfigError):
            parse_yaml_model(PolicyConfig, "policy.yaml", b"version: 1\ndiscovery: {" + bad + b"}\n")
    off = b"version: 1\nproposal: {max_per_day: 1}\ndiscovery: {target_proposals_per_day: 0}\n"
    assert parse_yaml_model(PolicyConfig, "policy.yaml", off).discovery.target_proposals_per_day == 0
    equal = b"version: 1\nproposal: {max_per_day: 3}\ndiscovery: {target_proposals_per_day: 3}\n"
    assert parse_yaml_model(PolicyConfig, "policy.yaml", equal).discovery.target_proposals_per_day == 3


def test_reserved_slots_within_proposals_per_cycle() -> None:
    text = b"version: 1\ndiscovery: {proposals_per_cycle: 1, open: {reserved_slots: 2}}\n"
    with pytest.raises(ConfigError, match=r"discovery\.open\.reserved_slots 2 exceeds"):
        parse_yaml_model(PolicyConfig, "policy.yaml", text)
    ok = b"version: 1\ndiscovery: {proposals_per_cycle: 1, open: {reserved_slots: 1}}\n"
    assert parse_yaml_model(PolicyConfig, "policy.yaml", ok).discovery.open.reserved_slots == 1


def test_bad_open_block_fails_the_bundle_with_the_field_path() -> None:
    files = bundle_files()
    files["policy.yaml"] = files["policy.yaml"].replace(b"    min_stars: 1000", b"    min_stars: 5")
    with pytest.raises(ConfigError) as info:
        parse_bundle(files)
    assert "discovery.open.min_stars" in " ".join(info.value.errors["policy.yaml"])


def test_target_egress_entry_and_defaults() -> None:
    text = (
        "version: 1\ndefaults: {egress: strict}\ncategories: [{id: a, priority: 1, why: x, repos: ["
        "{repo: a/one}, {repo: a/two, egress: mode}]}]\n"
    )
    targets = parse_yaml_model(TargetsConfig, "targets.yaml", text.encode())
    assert getattr(resolve_target(targets, "a/one"), "egress", None) == "strict"
    assert getattr(resolve_target(targets, "a/two"), "egress", None) == "mode"
    plain = _targets("{repo: a/one}, {repo: a/two, egress: strict}")
    assert getattr(resolve_target(plain, "a/one"), "egress", None) == "mode"
    assert getattr(resolve_target(plain, "a/two"), "egress", None) == "strict"
    with pytest.raises(ConfigError, match="egress"):
        _targets("{repo: a/one, egress: open}")
    shipped = parse_yaml_model(TargetsConfig, "targets.yaml", load("targets.yaml"))
    assert shipped.defaults.egress == "mode"


def test_discovered_target_synthesis() -> None:
    targets = parse_yaml_model(TargetsConfig, "targets.yaml", load("targets.yaml"))
    d = targets.defaults
    t = discovered_target(targets, "argoproj/argo-cd", kept=False, egress_strict=False)
    assert t is not None
    assert (t.repo, t.category, t.priority) == ("argoproj/argo-cd", OPEN_CATEGORY, OPEN_PRIORITY)
    assert t.discovery is True and t.disclosure_template is None
    assert t.labels == tuple(d.labels) and t.skip_labels == tuple(d.skip_labels)
    assert t.clone_depth == d.clone_depth and t.validation == tuple(d.validation)
    assert t.cla_signed is d.cla_signed and t.egress_extra == tuple(d.egress_extra)
    assert t.ai_unstated is None and t.ai_unclear is None and t.egress == "mode"
    kept = discovered_target(targets, "argoproj/argo-cd", kept=True, egress_strict=True)
    assert kept is not None and kept.priority == KEPT_PRIORITY and kept.egress == "strict"
    assert discovered_target(targets, "LineageOS/android", kept=False, egress_strict=False) is None  # denylist


def test_discovered_target_takes_every_default() -> None:
    """Review-3 L2: enabled, size_class, ai_policy_override and egress_extra flow from targets.defaults."""
    text = (
        "version: 1\ndefaults: {enabled: false, size_class: s, ai_policy_override: allowed, cla_signed: true,"
        " egress_extra: [charts.example.org], egress: strict}\ncategories: []\n"
    )
    targets = parse_yaml_model(TargetsConfig, "targets.yaml", text.encode())
    t = discovered_target(targets, "o/r", kept=False, egress_strict=False)
    assert t is not None
    assert (t.enabled, t.size_class, t.ai_policy_override) == (False, "s", "allowed")
    assert t.cla_signed is True and t.egress_extra == ("charts.example.org",) and t.egress == "strict"


def test_helm_hosts_lives_in_common_config() -> None:
    targets = _targets("{repo: a/one, egress_extra: [charts.example.org]}")
    assert helm_hosts(targets) == ["charts.example.org"]
    from ossbot.ctl import render

    assert render.helm_hosts is helm_hosts
