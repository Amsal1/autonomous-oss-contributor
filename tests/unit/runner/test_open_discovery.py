"""Post-v1 open discovery, pure part (design §5.1-§5.3, §6.1, §12.4; review-3 M2): queries, topic
cursor, budget, rules 1-14 with their boundaries, ranking, probe window, egress precheck and texts."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from ossbot.common.config import ConfigBundle, discovered_target, parse_bundle
from ossbot.common.schemas import SKIP_REASON_RE
from ossbot.runner import open_discovery as od
from tests.unit.common.test_config import bundle_files

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def item(name: str = "acme/gitops-tool", **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "full_name": name, "fork": False, "archived": False, "disabled": False, "visibility": "public",
        "stargazers_count": 5000, "pushed_at": iso(3), "language": "Go", "license": {"spdx_id": "Apache-2.0"},
        "topics": ["gitops"], "has_issues": True, "has_pull_requests": True, "default_branch": "main",
    }  # fmt: skip
    doc.update(over)
    return doc


def hit(name: str = "acme/gitops-tool", **over: Any) -> od.RepoHit:
    got = od.RepoHit.from_item(item(name, **over), topic="gitops")
    assert got is not None
    return got


@pytest.fixture(scope="module")
def bundle() -> ConfigBundle:
    return parse_bundle(bundle_files())


@pytest.fixture(scope="module")
def rules(bundle: ConfigBundle) -> od.RepoRules:
    return od.RepoRules.from_bundle(bundle)


# ------------------------------------------------------------------ queries, cursor, budget
def test_repo_query_is_exact() -> None:
    assert od.build_repo_query("gitops", 1000, date(2026, 8, 22)) == (
        "topic:gitops stars:>=1000 pushed:>=2026-08-22 archived:false mirror:false is:public"
    )


def test_issue_query_quotes_labels_with_spaces_in_one_or_list(bundle: ConfigBundle) -> None:
    labels = bundle.policy.discovery.open.issue_labels
    assert od.build_issue_query("acme/tool", labels) == (
        "repo:acme/tool is:issue is:open no:assignee archived:false "
        'label:"good first issue","help wanted",bug,documentation,docs'
    )


def test_select_topics_wraps_and_advances() -> None:
    topics = ["a", "b", "c", "d", "e"]
    assert od.select_topics(topics, 0, 3) == (["a", "b", "c"], 3)
    assert od.select_topics(topics, 3, 3) == (["d", "e", "a"], 1)
    assert od.select_topics(topics, 12, 3) == (["c", "d", "e"], 0)
    assert od.select_topics(["a", "b"], 1, 3) == (["b", "a"], 1)  # never one topic twice
    assert od.select_topics([], 4, 3) == ([], 0)


def test_search_budget_and_the_issue_search_stop() -> None:
    budget = od.SearchBudget(10)
    for _ in range(3):
        assert budget.take()
    assert budget.remaining == 7
    allowed = []
    while budget.issue_search_allowed(reserved_slots=1):
        allowed.append(budget.take())
    assert allowed == [True] * 5 and budget.remaining == 2  # stops at remaining <= reserved_slots + 1
    assert budget.take() and budget.take() and not budget.take() and budget.remaining == 0


def test_probe_window() -> None:
    assert od.probe_window(5) == (7, 21)
    assert od.probe_window(1) == (3, 9)


# ------------------------------------------------------------------------------- rules 1-12
def test_from_item_defaults_and_drops() -> None:
    h = od.RepoHit.from_item({"full_name": "o/r"})
    assert h is not None and h.has_pull_requests and h.pull_request_creation_policy == "all"
    assert h.visibility == "public" and not h.has_license and h.default_branch == "main"
    assert od.RepoHit.from_item({"full_name": "o/r", "private": True}).visibility == "private"  # type: ignore[union-attr]
    assert od.RepoHit.from_item({"full_name": "../etc"}) is None
    assert od.RepoHit.from_item({"name": "r"}) is None


def test_a_good_hit_passes(rules: od.RepoRules) -> None:
    assert od.repo_filter_reason(hit(), rules, NOW, excluded=set()) is None


@pytest.mark.parametrize(
    ("name", "over", "reason"),
    [
        ("langfuse/langfuse", {}, "excluded"),  # 1: config target (any case)
        ("LANGFUSE/Langfuse", {}, "excluded"),
        ("LineageOS/android", {}, "excluded"),  # 1: denylist
        ("test-bot-login/tool", {}, "excluded"),  # 1: own login
        ("test-work-login/tool", {}, "excluded"),  # 1: profile forbidden login
        ("acme/tool", {"fork": True}, "fork"),
        ("acme/tool", {"archived": True}, "archived"),
        ("acme/tool", {"disabled": True}, "archived"),
        ("acme/tool", {"visibility": "private"}, "private"),
        ("acme/tool", {"stargazers_count": 999}, "stars_below_min"),
        ("acme/tool", {"pushed_at": iso(61)}, "stale_push"),
        ("acme/tool", {"pushed_at": None}, "stale_push"),
        ("acme/tool", {"license": None}, "license_missing"),
        ("acme/tool", {"license": {"spdx_id": "NOASSERTION"}}, "license_not_osi"),
        ("acme/tool", {"license": {"spdx_id": "BUSL-1.1"}}, "license_not_osi"),
        ("acme/tool", {"license": {"spdx_id": None}}, "license_not_osi"),
        ("acme/tool", {"has_issues": False}, "issues_disabled"),
        ("acme/tool", {"has_pull_requests": False}, "prs_disabled"),
        ("acme/tool", {"pull_request_creation_policy": "collaborators_only"}, "pr_creation_restricted"),
    ],
)
def test_rules_1_to_12(rules: od.RepoRules, name: str, over: dict[str, Any], reason: str) -> None:
    assert od.repo_filter_reason(hit(name, **over), rules, NOW, excluded=set()) == reason


@pytest.mark.parametrize(
    "over",
    [{"stargazers_count": 1000}, {"pushed_at": iso(60)}, {"pull_request_creation_policy": "all"}],
    ids=["1000-stars", "60-days", "creation-all"],
)
def test_rule_boundaries_pass(rules: od.RepoRules, over: dict[str, Any]) -> None:
    assert od.repo_filter_reason(hit(**over), rules, NOW, excluded=set()) is None


def test_has_pull_requests_absent_passes(rules: od.RepoRules) -> None:
    doc = item()
    del doc["has_pull_requests"]
    h = od.RepoHit.from_item(doc)
    assert h is not None and od.repo_filter_reason(h, rules, NOW, excluded=set()) is None


def test_rule_2_excluded_list_and_rule_1b_active_set(rules: od.RepoRules) -> None:
    """M2: a repo with max_open_prs_by_me_per_repo open PRs is excluded like a never/kept row."""
    assert od.repo_filter_reason(hit("acme/Tool"), rules, NOW, excluded={"acme/tool"}) == "excluded"
    assert od.repo_filter_reason(hit("acme/Tool"), rules, NOW, excluded=set(), active={"acme/tool"}) == "excluded"
    # 1b comes before the item rules: even a fork in the active set reads "excluded" (not recorded)
    assert od.repo_filter_reason(hit("acme/x", fork=True), rules, NOW, excluded=(), active={"ACME/X"}) == "excluded"


def test_rule_13_contributing() -> None:
    assert od.contributing_reason(None) == "not_found"
    assert od.contributing_reason({"files": {"contributing": None}}) == "no_contributing"
    assert od.contributing_reason({"files": {}}) == "no_contributing"
    assert od.contributing_reason({"files": {"contributing": {"url": "https://api.github.com/repos/o/r/x"}}}) is None
    org_default = {
        "files": {
            "contributing": {
                "url": "https://api.github.com/repos/acme/.github/contents/CONTRIBUTING.md",
                "html_url": "https://github.com/acme/.github/blob/main/CONTRIBUTING.md",
            }
        }
    }
    assert od.contributing_reason(org_default) is None  # V6: the org's .github default passes


def test_default_branch_activity_gate() -> None:
    """Owner addition: active vs 7-month-old default branch; unknown is not a skip."""
    assert od.inactive_reason(iso(3), NOW, 180) is None
    assert od.inactive_reason(iso(180), NOW, 180) is None
    assert od.inactive_reason(iso(213), NOW, 180) == "inactive_default_branch"
    assert od.inactive_reason(None, NOW, 180) is None
    assert od.inactive_reason("garbage", NOW, 180) is None


# ------------------------------------------------------------------------------------ ranking
def test_repo_score_base_values() -> None:
    kw: dict[str, Any] = {"min_stars": 1000, "now": NOW, "languages_preferred": ["Go"], "language_weight": 6.0}
    assert od.repo_score_base(hit(stargazers_count=1000, pushed_at=iso(40), language="C"), **kw) == 1.0
    assert od.repo_score_base(hit(stargazers_count=10_000, pushed_at=iso(20), language="C"), **kw) == 8.0
    assert od.repo_score_base(hit(stargazers_count=100_000, pushed_at=iso(2)), **kw) == 21.0
    assert od.repo_score_base(hit(stargazers_count=500_000, pushed_at=iso(2)), **kw) == 21.0  # stars capped at 10


def test_language_matching_hcl_and_cloudformation_by_topic() -> None:
    assert od.language_matches(hit(language="HCL"), ["hcl"])
    assert od.language_matches(hit(language="go"), ["Go"])
    assert od.language_matches(hit(language=None, topics=["aws-cloudformation"]), ["CloudFormation"])
    assert od.language_matches(hit(language="Python", topics=["cloudformation"]), ["CloudFormation"])
    assert not od.language_matches(hit(language="Python", topics=["cloudformation"]), ["Go"])
    assert not od.language_matches(hit(language=None, topics=["aws"]), ["CloudFormation"])


def test_final_rank_with_ci_and_activity_and_tie_break() -> None:
    assert od.repo_score(10.0, ci_on_pr="yes", ci_weight=6.0) == 16.0
    assert od.repo_score(10.0, ci_on_pr="unknown", ci_weight=6.0) == 10.0
    assert od.repo_score(10.0, ci_on_pr="no", ci_weight=6.0, branch_bonus=2.0) == 12.0
    assert od.branch_activity_bonus(iso(2), NOW) == 2.0 and od.branch_activity_bonus(iso(20), NOW) == 1.0
    assert od.branch_activity_bonus(iso(90), NOW) == 0.0 and od.branch_activity_bonus(None, NOW) == 0.0
    a, b = hit("zeta/x", stargazers_count=2000), hit("Alpha/x", stargazers_count=2000)
    c = hit("m/x", stargazers_count=9000)
    ordered = sorted([(5.0, a), (5.0, b), (5.0, c), (7.0, a)], key=lambda p: od.rank_key(p[0], p[1]))
    assert [(s, h.full_name) for s, h in ordered] == [(7.0, "zeta/x"), (5.0, "m/x"), (5.0, "Alpha/x"), (5.0, "zeta/x")]
    assert od.repo_bonus(16.0) == 8.0


def test_ci_boost_moves_repo_six_into_the_scanned_five() -> None:
    """§5.2/§5.3: W = 7 passed repos get CI facts; repo #6 by base rank with CI yes enters the top 5."""
    bases = {f"o/r{i}": 20.0 - i for i in range(1, 8)}  # r1 best .. r7
    ci = {"o/r6": "yes"}
    scored = sorted(bases, key=lambda r: -od.repo_score(bases[r], ci_on_pr=ci.get(r, "unknown"), ci_weight=6.0))
    top, rest = scored[:5], scored[5:]
    assert "o/r6" in top and rest == ["o/r5", "o/r7"]  # the 2 extra passes stay untried candidates


# ------------------------------------------------------------------------- egress precheck
FILES = {
    "Chart.yaml": "dependencies:\n  - name: redis\n    repository: https://charts.example.io/stable\n"
                  "  - name: pg\n    repository: oci://registry-1.docker.io/bitnamicharts\n",
    ".npmrc": "@acme:registry=https://npm.acme.dev/\nregistry=https://registry.npmjs.org/\n",
    ".yarnrc.yml": 'npmRegistryServer: "https://yarn.acme.dev"\n',
    "requirements.txt": "--extra-index-url https://user:pw@pypi.acme.dev:8443/simple\nrequests==2.0\n",
    "pip.conf": "[global]\nindex-url = https://pypi.org/simple\n",
    "build.gradle": "plugins { id 'java' }\nrepositories {\n  mavenCentral()\n"
                    "  maven { url 'https://maven.acme.dev/r' }\n  maven { url = uri(\"https://jitpack.io\") }\n}\n"
                    "publishing { url 'https://ignored.example' }\n",
    "settings.gradle.kts": "dependencyResolutionManagement {\n  repositories {\n"
                           '    maven("https://kts.acme.dev")\n  }\n}\n',
    "pom.xml": "<project><url>https://site.example</url><repositories><repository><url>https://repo.acme.dev/m2</url>"
               "</repository></repositories><pluginRepositories><pluginRepository><url> https://plugins.acme.dev </url>"
               "</pluginRepository></pluginRepositories></project>",
    ".cargo/config.toml": '[registries]\nmy = { index = "sparse+https://cargo.acme.dev/index/" }\n'
                          '[registries.other]\nindex = "https://git.acme.dev/index"\n',
}  # fmt: skip


def test_egress_host_extraction() -> None:
    assert od.egress_hosts(FILES) == {
        "charts.example.io", "registry-1.docker.io", "npm.acme.dev", "registry.npmjs.org", "yarn.acme.dev",
        "pypi.acme.dev", "pypi.org", "maven.acme.dev", "jitpack.io", "kts.acme.dev", "repo.acme.dev",
        "plugins.acme.dev", "cargo.acme.dev", "git.acme.dev",
    }  # fmt: skip
    assert od.egress_hosts({"Chart.yaml": "repository: ${REPO}\n", "README.md": "https://x.example"}) == set()


def test_allowed_hosts_and_squid_semantics(bundle: ConfigBundle) -> None:
    allowed = od.allowed_hosts(bundle)
    assert {"pypi.org", "registry.npmjs.org", "repo.maven.apache.org", "github.com"} <= allowed
    assert "telemetry.us-east-1.kiro.dev" not in allowed and "app.kiro.dev" not in allowed  # not a fetch category
    assert od.host_allowed("a.b.github.io", [".github.io"]) and od.host_allowed("github.io", [".github.io"])
    assert not od.host_allowed("evilgithub.io", [".github.io"]) and not od.host_allowed("x.pypi.org", ["pypi.org"])
    assert od.uncovered_hosts(od.egress_hosts(FILES), allowed)[:3] == ["cargo.acme.dev", "charts.example.io",
                                                                        "git.acme.dev"]  # fmt: skip


def test_helm_hosts_and_defaults_egress_extra_are_allowed(bundle: ConfigBundle) -> None:
    targets = bundle.targets
    defaults = targets.defaults.model_copy(update={"egress_extra": ["charts.example.io"]})
    cats = list(targets.categories)
    helm_repo = cats[0].repos[0].model_copy(update={"egress_extra": ["helm.acme.dev"]})
    first = cats[0].model_copy(update={"repos": [helm_repo, *cats[0].repos[1:]]})
    widened = dataclasses.replace(
        bundle, targets=targets.model_copy(update={"defaults": defaults, "categories": [first, *cats[1:]]})
    )
    allowed = od.allowed_hosts(widened)
    assert {"charts.example.io", "helm.acme.dev"} <= allowed
    t = discovered_target(widened.targets, "acme/tool", kept=False, egress_strict=False)
    assert t is not None and t.egress_extra == ("charts.example.io",)


def test_effective_strict() -> None:
    assert not od.effective_strict("sandbox_open", "mode")  # rule 14 skipped
    assert od.effective_strict("sandbox_open", "strict")
    assert od.effective_strict("open", "mode") and od.effective_strict("allowlist", "mode")
    assert od.effective_strict(None, "mode")  # unknown mode -> strict (fail closed)


def test_egress_reasons_and_the_two_texts() -> None:
    import re

    hosts = ["charts.example.io", "git.acme.dev", "jitpack.io", "kts.acme.dev"]
    mode = od.egress_reason(hosts, repo_strict=False)
    strict = od.egress_reason(hosts, repo_strict=True)
    assert mode == "egress_extra_needed:charts.example.io,git.acme.dev,jitpack.io"
    assert strict == "egress_extra_needed_strict:charts.example.io,git.acme.dev,jitpack.io"
    long = od.egress_reason(["a" * 60 + ".io", "b" * 60 + ".io", "c.io"], repo_strict=False)
    for reason in (mode, strict, long):
        assert re.match(SKIP_REASON_RE, reason), reason
    text_mode = od.reason_text("acme/tool", mode)
    text_strict = od.reason_text("acme/tool", strict)
    assert text_mode == ("needs egress to charts.example.io, git.acme.dev, jitpack.io (not in an egress category). "
                         "To use it, switch to egress mode sandbox_open, or add it to config/targets.yaml with "
                         "egress_extra and /reload.")  # fmt: skip
    assert text_strict is not None and "this repo is strict: ossctl targets strict acme/tool --off" in text_strict
    assert "sandbox_open" not in text_strict  # a mode switch would not help
    assert od.reason_text("acme/tool", "no_contributing") is None
