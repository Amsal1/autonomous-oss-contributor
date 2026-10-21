"""Open discovery beyond the seed targets (post-v1, design §5, §6.1, §12.4): pure functions.

Repo search by topic (``build_repo_query``, ``select_topics``, ``SearchBudget``), the deterministic repo
rules 1-14 (rules 1-12 from the search item, no request and no row; 13 CONTRIBUTING and 14 the egress
precheck cost reads and are recorded as ``skipped``; the default-branch activity gate of the owner
addition 2026-10-21 is recorded like them), the ranking (``repo_score_base`` + CI + default-branch
activity) and the probe window. Rule 1b (review-3 M2): a repo in the ``active`` set (one-open-PR rule)
is ``excluded`` and not recorded, so its row stays as it is.

Every value here comes from GitHub search items or files; nothing is LLM output. The cycle itself
(``OpenPhase``) is in the second half of this module.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ossbot.common.clock import from_iso
from ossbot.common.config import REPO_RE, ConfigBundle, helm_hosts
from ossbot.common.osi_licenses import is_osi

OPEN_NOTE = "_open"  # runner note: {"cursor": int, "search_until": float}
SANDBOX_OPEN = "sandbox_open"  # the only egress mode in which a mode-egress repo skips rule 14
EXCLUDED = "excluded"
INACTIVE = "inactive_default_branch"
NOT_FOUND = "not_found"
NO_CONTRIBUTING = "no_contributing"
EGRESS_MODE_REASON = "egress_extra_needed"
EGRESS_STRICT_REASON = "egress_extra_needed_strict"
REASON_HOSTS_MAX = 3
REASON_TAIL_MAX = 120  # common.schemas.SKIP_REASON_RE: ``:[a-z0-9.,-]{1,120}``
CLOUDFORMATION_TOPICS = frozenset({"cloudformation", "aws-cloudformation"})
_REPO = re.compile(REPO_RE)
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+$")


# ------------------------------------------------------------------------ queries and budget
def build_repo_query(topic: str, min_stars: int, pushed_since: date) -> str:
    """§5.1, exactly: forks are left out by GitHub's default (and post-filtered), no AND/OR/NOT."""
    return (f"topic:{topic} stars:>={min_stars} pushed:>={pushed_since.isoformat()} archived:false mirror:false"
            " is:public")  # fmt: skip


def _label(label: str) -> str:
    return f'"{label}"' if " " in label else label


def build_issue_query(repo: str, labels: Iterable[str]) -> str:
    """§6.1: a comma label list is an OR under the default (lexical) search syntax (V2)."""
    return f"repo:{repo} is:issue is:open no:assignee archived:false label:{','.join(_label(x) for x in labels)}"


def select_topics(topics: list[str], cursor: int, n: int) -> tuple[list[str], int]:
    """``n`` topics from ``cursor % len(topics)``, wrapping (never one twice); the next cursor."""
    if not topics:
        return [], 0
    n = max(0, min(n, len(topics)))
    start = cursor % len(topics)
    picked = [topics[(start + i) % len(topics)] for i in range(n)]
    return picked, (start + n) % len(topics)


class SearchBudget:
    """Every search request of the open part of a cycle (§5.1): repo and issue searches and the
    tier-1 mention checks. ``take()`` is False once spent."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.spent

    def take(self) -> bool:
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True

    def issue_search_allowed(self, reserved_slots: int) -> bool:
        """§6.1 (finding 6): issue searches stop at ``remaining <= reserved_slots + 1``."""
        return self.remaining > reserved_slots + 1


def probe_window(new_repos_per_cycle: int) -> tuple[int, int]:
    """§5.2 (finding 5): rules 13-14 run until ``W = n + 2`` repos passed or ``3 W`` were probed."""
    window = new_repos_per_cycle + 2
    return window, 3 * window


# --------------------------------------------------------------------------------- repo hits
@dataclass(frozen=True)
class RepoHit:
    """The fields of a search item (or ``GET /repos/{r}``) the rules and the ranking use."""

    full_name: str
    fork: bool
    archived: bool
    disabled: bool
    visibility: str
    stars: int
    pushed_at: str | None
    language: str | None
    has_license: bool
    license_spdx: str | None
    topics: tuple[str, ...]
    has_issues: bool
    has_pull_requests: bool
    pull_request_creation_policy: str
    default_branch: str
    topic: str | None = None  # the search topic that found it (None for a carry-over or a suggestion)

    @classmethod
    def from_item(cls, item: Mapping[str, Any], topic: str | None = None) -> RepoHit | None:
        """None for an item without a valid ``full_name`` (dropped at DEBUG by the caller)."""
        name = item.get("full_name")
        if not isinstance(name, str) or not _REPO.match(name):
            return None
        lic = item.get("license")
        spdx = lic.get("spdx_id") if isinstance(lic, dict) else None
        stars = item.get("stargazers_count")
        topics = item.get("topics")
        visibility = item.get("visibility")
        if not isinstance(visibility, str):
            visibility = "private" if item.get("private") else "public"
        pushed = item.get("pushed_at")
        language = item.get("language")
        policy = item.get("pull_request_creation_policy")
        branch = item.get("default_branch")
        return cls(
            full_name=name, fork=bool(item.get("fork")), archived=bool(item.get("archived")),
            disabled=bool(item.get("disabled")), visibility=visibility,
            stars=stars if isinstance(stars, int) and stars >= 0 else 0,
            pushed_at=pushed if isinstance(pushed, str) else None,
            language=language if isinstance(language, str) else None,
            has_license=isinstance(lic, dict), license_spdx=spdx if isinstance(spdx, str) else None,
            topics=tuple(str(t).lower() for t in topics if isinstance(t, str)) if isinstance(topics, list) else (),
            has_issues=item.get("has_issues") is not False,
            has_pull_requests=item.get("has_pull_requests") is not False,  # absent -> True
            pull_request_creation_policy=policy if isinstance(policy, str) else "all",
            default_branch=branch if isinstance(branch, str) and branch else "main", topic=topic,
        )  # fmt: skip


@dataclass(frozen=True)
class RepoRules:
    """The inputs of rules 1 and 6-7: config targets, denylist, the owner's logins, the thresholds."""

    config_repos: frozenset[str]  # lower-cased targets.yaml repos (any status)
    deny_patterns: tuple[str, ...]
    own_logins: frozenset[str]  # lower-cased profile.github_login and profile.forbidden_logins
    min_stars: int
    pushed_within_days: int

    @classmethod
    def from_bundle(cls, bundle: ConfigBundle, *, min_stars: int | None = None,
                    pushed_within_days: int | None = None) -> RepoRules:  # fmt: skip
        open_cfg = bundle.policy.discovery.open
        profile = bundle.profile
        return cls(
            config_repos=frozenset(e.repo.lower() for c in bundle.targets.categories for e in c.repos),
            deny_patterns=tuple(d.pattern for d in bundle.targets.denylist),
            own_logins=frozenset({profile.github_login.lower(), *(x.lower() for x in profile.forbidden_logins)}),
            min_stars=min_stars if min_stars is not None else open_cfg.min_stars,
            pushed_within_days=pushed_within_days if pushed_within_days is not None else open_cfg.pushed_within_days,
        )

    def is_denied(self, repo: str) -> bool:
        return any(fnmatch.fnmatchcase(repo.lower(), p.lower()) for p in self.deny_patterns)


def _age(now: datetime, iso: str | None) -> timedelta | None:
    if not iso:
        return None
    try:
        return now - from_iso(iso)
    except ValueError:
        return None


def repo_filter_reason(hit: RepoHit, rules: RepoRules, now: datetime, excluded: Iterable[str],
                       active: Iterable[str] = ()) -> str | None:  # fmt: skip
    """Rules 1-12 of §5.2 (first match wins), from the search item only. None = passed.

    Rule 1b (M2): a repo in ``active`` (``max_open_prs_by_me_per_repo`` open PRs already) is
    ``excluded``, not recorded, like rules 1 and 2."""
    key = hit.full_name.lower()
    owner = key.split("/", 1)[0]
    if key in rules.config_repos or rules.is_denied(key) or owner in rules.own_logins:
        return EXCLUDED  # 1
    if key in {r.lower() for r in active}:
        return EXCLUDED  # 1b
    if key in {r.lower() for r in excluded}:
        return EXCLUDED  # 2
    if hit.fork:
        return "fork"  # 3
    if hit.archived or hit.disabled:
        return "archived"  # 4
    if hit.visibility != "public":
        return "private"  # 5
    if hit.stars < rules.min_stars:
        return "stars_below_min"  # 6
    age = _age(now, hit.pushed_at)
    if age is None or age > timedelta(days=rules.pushed_within_days):
        return "stale_push"  # 7
    if not hit.has_license:
        return "license_missing"  # 8
    if not hit.license_spdx or not is_osi(hit.license_spdx):
        return "license_not_osi"  # 9 (NOASSERTION included)
    if not hit.has_issues:
        return "issues_disabled"  # 10
    if not hit.has_pull_requests:
        return "prs_disabled"  # 11
    if hit.pull_request_creation_policy == "collaborators_only":
        return "pr_creation_restricted"  # 12
    return None


def contributing_reason(profile: Mapping[str, Any] | None) -> str | None:
    """Rule 13 from ``GET /repos/{r}/community/profile``: 404 -> ``not_found``; ``files.contributing``
    null -> ``no_contributing``. A guide GitHub takes from the org's ``.github`` repo passes (V6)."""
    if profile is None:
        return NOT_FOUND
    files = profile.get("files")
    contributing = files.get("contributing") if isinstance(files, dict) else None
    return None if contributing else NO_CONTRIBUTING


def inactive_reason(commit_at: str | None, now: datetime, max_days: int) -> str | None:
    """Owner addition (2026-10-21): the default branch's last commit older than ``max_days`` ->
    ``inactive_default_branch``. Unknown (no date, API failure) is not a skip."""
    age = _age(now, commit_at)
    return INACTIVE if age is not None and age > timedelta(days=max_days) else None


# ------------------------------------------------------------------------------------- ranking
def language_matches(hit: RepoHit, preferred: Iterable[str]) -> bool:
    """Case-insensitive; ``CloudFormation`` also by topic (linguist has no such language, V8).
    Terraform repos report ``HCL``."""
    wanted = {p.casefold() for p in preferred}
    if hit.language is not None and hit.language.casefold() in wanted:
        return True
    return "cloudformation" in wanted and bool(CLOUDFORMATION_TOPICS & set(hit.topics))


def repo_score_base(hit: RepoHit, *, min_stars: int, now: datetime, languages_preferred: Iterable[str],
                    language_weight: float) -> float:  # fmt: skip
    """§5.3: stars (0..10), push activity (5/3/1) and the preferred-language weight."""
    ratio = math.log10(max(hit.stars, 1) / max(min_stars, 1)) / 2
    stars = 10.0 * min(1.0, max(0.0, ratio))
    age = _age(now, hit.pushed_at)
    if age is not None and age <= timedelta(days=7):
        activity = 5.0
    elif age is not None and age <= timedelta(days=30):
        activity = 3.0
    else:
        activity = 1.0
    language = language_weight if language_matches(hit, languages_preferred) else 0.0
    return stars + activity + language


def branch_activity_bonus(commit_at: str | None, now: datetime) -> float:
    """Owner addition: a recent default-branch commit is a small boost (2 within 7 days, 1 within
    30); unknown or older is 0. Used for open repos (in ``repo_score``) and seed repos (``repo_bonus``)."""
    age = _age(now, commit_at)
    if age is None:
        return 0.0
    return 2.0 if age <= timedelta(days=7) else 1.0 if age <= timedelta(days=30) else 0.0


def repo_score(base: float, *, ci_on_pr: str, ci_weight: float, branch_bonus: float = 0.0) -> float:
    """§5.3 final repo score: base + ``ci_on_pr_weight`` when CI ran on merged PRs + activity."""
    return round(base + (ci_weight if ci_on_pr == "yes" else 0.0) + branch_bonus, 4)


def rank_key(score: float, hit: RepoHit) -> tuple[float, int, str]:
    """Sort key: higher score, then more stars, then the name (case-insensitive)."""
    return (-score, -hit.stars, hit.full_name.lower())


def repo_bonus(score: float) -> float:
    """§5.3: the repo score reaches the issue ranking as ``0.5 * repo_score`` (``Candidate.repo_bonus``)."""
    return round(0.5 * score, 4)


# --------------------------------------------------------------------------- egress precheck
# §12.4: root files read for the precheck (≤ EGRESS_READS_MAX of those that exist), each ≤ 200 KB
EGRESS_FILES: tuple[str, ...] = (
    "Chart.yaml", ".npmrc", ".yarnrc.yml", "pip.conf", "requirements.txt", "requirements-dev.txt",
    "settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts", "pom.xml", ".cargo/config.toml",
)  # fmt: skip
EGRESS_READS_MAX = 6
_CHART = re.compile(r"^\s*-?\s*repository:\s*[\"']?((?:https|oci)://[^\s\"']+)", re.MULTILINE)
_NPMRC = re.compile(r"^\s*(?:@[^:\s]+:)?registry\s*=\s*[\"']?([^\s\"']+)", re.MULTILINE)
_YARNRC = re.compile(r"npmRegistryServer:\s*[\"']?([^\s\"']+)")
_PIP = re.compile(r"(?:--(?:extra-)?index-url[\s=]+|^\s*(?:extra-)?index[-_]url\s*=\s*)([^\s\"']+)", re.MULTILINE)
_GRADLE_URL = re.compile(
    r"(?:\burl\b\s*(?:=\s*)?\(?\s*(?:uri\s*\(\s*)?|\buri\s*\(\s*|\bmaven\s*\(\s*)[\"']([^\"']+)[\"']"
)
_MAVEN_BLOCK = re.compile(r"<(repositories|pluginRepositories)>(.*?)</\1>", re.DOTALL)
_MAVEN_URL = re.compile(r"<url>\s*([^<\s]+)\s*</url>")
_CARGO = re.compile(r"\bindex\s*=\s*[\"']([^\"']+)[\"']")


def _blocks(text: str, name: str) -> list[str]:
    """The bodies of every ``name { … }`` block (brace-matched; Gradle ``repositories {}``)."""
    out: list[str] = []
    for m in re.finditer(rf"\b{name}\s*\{{", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out.append(text[m.end() : i - 1])
    return out


def host_of(url: str) -> str | None:
    """Lower-cased host of a registry URL, port and userinfo stripped; None for a placeholder or junk."""
    url = url.strip().removeprefix("sparse+").removeprefix("git+")
    if "://" not in url:
        url = "https://" + url
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host if host and _HOST.match(host) else None


def egress_hosts(files: Mapping[str, str]) -> set[str]:
    """Registry hosts named in the §12.4 files (path -> text), by fixed regexes per file kind."""
    urls: list[str] = []
    for path, text in files.items():
        name = path.rsplit("/", 1)[-1]
        if name == "Chart.yaml":
            urls += _CHART.findall(text)
        elif name == ".npmrc":
            urls += _NPMRC.findall(text)
        elif name == ".yarnrc.yml":
            urls += _YARNRC.findall(text)
        elif name in {"pip.conf", "requirements.txt", "requirements-dev.txt"}:
            urls += _PIP.findall(text)
        elif name.startswith(("settings.gradle", "build.gradle")):
            urls += [u for block in _blocks(text, "repositories") for u in _GRADLE_URL.findall(block)]
        elif name == "pom.xml":
            urls += [u for _, block in _MAVEN_BLOCK.findall(text) for u in _MAVEN_URL.findall(block)]
        elif path == ".cargo/config.toml":
            urls += _CARGO.findall(text)
    return {h for h in (host_of(u) for u in urls) if h is not None}


def allowed_hosts(bundle: ConfigBundle) -> frozenset[str]:
    """§12.4: the fetch base categories of the snapshot's egress.yaml (minus ``denied_by_default``),
    plus ``helm_hosts`` (the rendered ``pkg_helm``) and ``targets.defaults.egress_extra``."""
    egress = bundle.egress
    cats = [c for c in egress.sandbox_profiles.get("fetch", []) if c not in set(egress.denied_by_default)]
    hosts = {h.lower() for c in cats for h in egress.categories.get(c, [])}
    hosts |= {h.lower() for h in helm_hosts(bundle.targets)}
    hosts |= {h.lower() for h in bundle.targets.defaults.egress_extra}
    return frozenset(hosts)


def host_allowed(host: str, allowed: Iterable[str]) -> bool:
    """Squid ``dstdomain``: ``.example.com`` covers the domain and its subdomains, else exact."""
    host = host.lower()
    for raw in allowed:
        entry = raw.lower()
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def uncovered_hosts(hosts: Iterable[str], allowed: Iterable[str]) -> list[str]:
    allowed = list(allowed)
    return sorted(h for h in set(hosts) if not host_allowed(h, allowed))


def effective_strict(mode: str | None, repo_egress: str) -> bool:
    """§12.4: strict unless the active mode is ``sandbox_open``; an unknown mode is strict (fail
    closed); a repo with ``egress: strict`` is strict in every mode."""
    return mode != SANDBOX_OPEN or repo_egress == "strict"


def egress_reason(hosts: list[str], *, repo_strict: bool) -> str:
    """The stored reason (fits ``SKIP_REASON_RE``): the repo-strict cause wins over the mode cause."""
    code = EGRESS_STRICT_REASON if repo_strict else EGRESS_MODE_REASON
    tail = ""
    for host in hosts[:REASON_HOSTS_MAX]:
        nxt = f"{tail},{host}" if tail else host
        if len(nxt) > REASON_TAIL_MAX:
            break
        tail = nxt
    return f"{code}:{tail}" if tail else code


def egress_text(repo: str, hosts: list[str], *, repo_strict: bool) -> str:
    """The two fixed rule-14 texts (§12.4, review 2 finding 9); strict wins when both apply."""
    named = ", ".join(hosts[:REASON_HOSTS_MAX]) or "a package host"
    if repo_strict:
        return (f"needs egress to {named} (not in an egress category); this repo is strict: ossctl targets strict "
                f"{repo} --off (or defaults.egress in config/targets.yaml), or add it to config/targets.yaml with "
                "egress_extra and /reload.")  # fmt: skip
    return (f"needs egress to {named} (not in an egress category). To use it, switch to egress mode sandbox_open, "
            "or add it to config/targets.yaml with egress_extra and /reload.")  # fmt: skip


def reason_text(repo: str, reason: str) -> str | None:
    """The fixed rule-14 text for a stored ``egress_extra_needed[_strict]:<hosts>`` reason, else None."""
    code, _, tail = reason.partition(":")
    if code not in {EGRESS_MODE_REASON, EGRESS_STRICT_REASON}:
        return None
    return egress_text(repo, [h for h in tail.split(",") if h], repo_strict=code == EGRESS_STRICT_REASON)
