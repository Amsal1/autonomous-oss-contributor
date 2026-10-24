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
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ossbot.common.clock import from_iso
from ossbot.common.config import (
    LANGUAGE_RE,
    OPEN_CATEGORY,
    OPEN_PRIORITY,
    REPO_RE,
    TOPIC_RE,
    ConfigBundle,
    helm_hosts,
)
from ossbot.common.control_flags import CODE_WEB_MODES
from ossbot.common.osi_licenses import is_osi
from ossbot.common.schemas import ISO_TS_RE, SPDX_ID_RE
from ossbot.runner import ci_detect, scoring
from ossbot.runner.github_read import UNAVAILABLE, GitHubRateLimited, GitHubRead, GitHubReadError
from ossbot.runner.gk_client import GkError

if TYPE_CHECKING:
    from ossbot.runner.pipeline import StageContext

log = logging.getLogger(__name__)
OPEN_NOTE = "_open"  # runner note: {"cursor": int, "search_until": float}
SANDBOX_OPEN = "sandbox_open"  # named in the rule-14 text; CODE_WEB_MODES decides (ADR-0025)
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


def has_any_label(issue: Mapping[str, Any], wanted: Iterable[str]) -> bool:
    """Discovery pacing (2026-10-22): the client-side label OR of one ``GET /repos/{r}/issues`` page
    (the REST ``labels`` filter is an AND). Case-insensitive; an empty ``wanted`` takes every issue."""
    want = {w.lower() for w in wanted}
    if not want:
        return True
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    return any(isinstance(lbl, dict) and str(lbl.get("name", "")).lower() in want for lbl in labels)


def select_topics(topics: list[str], cursor: int, n: int) -> tuple[list[str], int]:
    """``n`` topics from ``cursor % len(topics)``, wrapping (never one twice); the next cursor."""
    if not topics:
        return [], 0
    n = max(0, min(n, len(topics)))
    start = cursor % len(topics)
    picked = [topics[(start + i) % len(topics)] for i in range(n)]
    return picked, (start + n) % len(topics)


class SearchBudget:
    """Every search request of the open part of a cycle (§5.1): the repo searches and the tier-1 mention
    checks (open repos' issues are a core read since 2026-10-22). ``take()`` is False once spent."""

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


def kept_reason(hit: RepoHit) -> str | None:
    """§6.2: a kept repo's re-check runs only rules 3-5 and 10-12 (the owner kept it: stars, push age,
    license and CONTRIBUTING are not re-checked)."""
    if hit.fork:
        return "fork"
    if hit.archived or hit.disabled:
        return "archived"
    if hit.visibility != "public":
        return "private"
    if not hit.has_issues:
        return "issues_disabled"
    if not hit.has_pull_requests:
        return "prs_disabled"
    if hit.pull_request_creation_policy == "collaborators_only":
        return "pr_creation_restricted"
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
    """§12.4: strict unless the active mode is a code-web mode (``sandbox_open``, ``sandbox_full``); an
    unknown mode is strict (fail closed); a repo with ``egress: strict`` is strict in every mode."""
    return mode not in CODE_WEB_MODES or repo_egress == "strict"


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


# ------------------------------------------------------------------------------------- reads (GET)
def branch_commit_at(gh: GitHubRead, repo: str, branch: str) -> str | None:
    """The default branch's last commit date, once per repo per cycle; a 4xx read is unknown (not a
    skip, re-read next cycle). Outages and rate limits propagate like every read."""
    try:
        return gh.default_branch_commit_at(repo, branch)
    except GitHubReadError as exc:
        if exc.error_class == UNAVAILABLE:
            raise
        log.info("default-branch commit of %s unknown (%s)", repo, exc.error_class,
                 extra={"event": "branch_commit_unknown"})  # fmt: skip
        return None


def precheck_files(gh: GitHubRead, repo: str) -> dict[str, str]:
    """§12.4: the root listing (1 read), then ≤ ``EGRESS_READS_MAX`` of the files that exist."""
    names = {str(e.get("name")) for e in gh.dir_entries(repo, "")}
    wanted = [p for p in EGRESS_FILES if p.split("/", 1)[0] in names][:EGRESS_READS_MAX]
    out: dict[str, str] = {}
    for path in wanted:
        text = gh.file_text(repo, path)
        if text:
            out[path] = text
    return out


@dataclass(frozen=True)
class Probe:
    """Rules 13-14 and the activity gate for one repo that passed rules 1-12."""

    reason: str | None
    commit_at: str | None
    hosts: tuple[str, ...] = ()  # uncovered hosts (rule 14), also when only reported (dry run)


def probe_repo(gh: GitHubRead, hit: RepoHit, *, now: datetime, max_age_days: int, allowed: frozenset[str],
               strict: bool, repo_strict: bool) -> Probe:  # fmt: skip
    """The default-branch activity gate, rule 13 (CONTRIBUTING) and, only when ``strict``, rule 14.
    Raises ``GitHubReadError`` for a failed read (the caller skips the repo on a 4xx)."""
    commit_at = branch_commit_at(gh, hit.full_name, hit.default_branch)
    inactive = inactive_reason(commit_at, now, max_age_days)
    if inactive:
        return Probe(inactive, commit_at)
    missing = contributing_reason(gh.community_profile(hit.full_name))
    if missing:
        return Probe(missing, commit_at)
    if strict:
        hosts = uncovered_hosts(egress_hosts(precheck_files(gh, hit.full_name)), allowed)
        if hosts:
            return Probe(egress_reason(hosts, repo_strict=repo_strict), commit_at, tuple(hosts))
    return Probe(None, commit_at)


_TOPIC = re.compile(TOPIC_RE)
_LANGUAGE = re.compile(LANGUAGE_RE)
_SPDX = re.compile(SPDX_ID_RE)
_ISO = re.compile(ISO_TS_RE)


def facts_body(
    repo: str, *, source: str, status: str, topic: str | None = None, stars: int | None = None,
    language: str | None = None, license_spdx: str | None = None, pushed_at: str | None = None,
    commit_at: str | None = None, ci_on_pr: str | None = None, score: float | None = None,
    skip_reason: str | None = None, tried: bool = False, kept_check: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """A ``DiscoveredFacts`` body with every GitHub value bounded to the schema (a value that would
    not validate is sent as unknown, so a strange language name never costs the repo)."""

    def iso(v: str | None) -> str | None:
        return v if isinstance(v, str) and len(v) <= 40 and _ISO.match(v) else None

    return {
        "repo": repo, "source": source if source in {"search", "hermes"} else "search", "status": status,
        "kept_check": kept_check, "topic": topic if topic and _TOPIC.match(topic) else None,
        "stars": stars if isinstance(stars, int) and 0 <= stars <= 10_000_000 else None,
        "language": language if language and _LANGUAGE.match(language) else None,
        "license_spdx": license_spdx if license_spdx and _SPDX.match(license_spdx) else None,
        "pushed_at": iso(pushed_at), "default_branch_commit_at": iso(commit_at),
        "ci_on_pr": ci_on_pr if ci_on_pr in {"yes", "no", "unknown"} else None,
        "repo_score": None if score is None else min(200.0, max(0.0, round(score, 4))),
        "skip_reason": skip_reason, "tried": tried,
    }  # fmt: skip


def hit_facts(hit: RepoHit, **kw: Any) -> dict[str, Any]:
    return facts_body(hit.full_name, topic=kw.pop("topic", hit.topic), stars=hit.stars, language=hit.language,
                      license_spdx=hit.license_spdx, pushed_at=hit.pushed_at, **kw)  # fmt: skip


# ----------------------------------------------------------------------------- the open phase
class OpenEnded(Exception):  # a control-flow signal, not an error
    """A 4xx on a search (a 422 would repeat every cycle) or a search limit longer than the bounded
    wait ends the open phase for this cycle; the seed candidates and the merge go on."""


@dataclass
class Selected:
    """A repo chosen for an issue scan this cycle (carry-over, Hermes suggestion or search)."""

    repo: str
    source: str  # discovered_targets source: "search" | "hermes"
    facts: dict[str, Any]  # its DiscoveredFacts body (status candidate), re-sent with tried: true
    score: float
    language: str | None
    suggested_issue: int | None = None


@dataclass
class OpenResult:
    """Tier-1 candidates for the merge (§7.3) and the budget their mention checks are paid from."""

    budget: SearchBudget
    candidates: list[scoring.Candidate] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # "repo#n" -> contribution source
    # repos whose issues were read this cycle; ``mark_tried`` records them once the merge has used them
    scanned: list[Selected] = field(default_factory=list)
    ended: str | None = None  # "rate_limited" | "http_error" when a search ended the phase early


REFUSED_CODES = frozenset({"never_repo", "is_target", "denied", "is_kept", "cooling", "not_kept"})
CARRY_MAX_AGE = timedelta(days=7)
SUGGESTIONS_MAX = 3


def record(sc: StageContext, body: dict[str, Any]) -> bool:
    """``POST /v1/discovered-targets``; False when the repo is dropped for the cycle. Review-3 M3: the
    answer is keyed on the gatekeeper's ``error`` code, not the status: a refusal is INFO
    ``open_repo_refused``; ``schema_error`` (400) or any other code is a runner bug, ERROR
    ``open_facts_invalid``; a 5xx is re-raised (the cycle ends like any gatekeeper outage)."""
    try:
        sc.gk.record_discovered(body)
    except GkError as exc:
        if exc.status >= 500:
            raise
        if exc.code in REFUSED_CODES:
            log.info("open discovery: %s refused (%s)", body["repo"], exc.code,
                     extra={"event": "open_repo_refused", "repo": body["repo"], "code": exc.code})  # fmt: skip
        else:
            log.error("open discovery: facts for %s rejected (%s %s)", body["repo"], exc.status, exc.code,
                      extra={"event": "open_facts_invalid", "repo": body["repo"], "code": exc.code})  # fmt: skip
        return False
    return True


def mark_tried(sc: StageContext, opened: OpenResult, *, reached: set[str], ranked: set[str]) -> None:
    """Discovery pacing (2026-10-22, fix 2): called only after a cycle's merge ended normally. A scanned
    repo is recorded ``tried`` (``tried_at`` and the cooldown) when the merge reached one of its tier-1
    candidates (``reached``: mention check, cached refusal or an unpaid budget skip) or when it had no
    ranked tier-1 candidate (``ranked``); both sets hold lower-cased repos. Any other scanned repo is
    re-recorded without ``tried``, so it stays an untried candidate with fresh facts and the next cycle
    carries it over. A cut cycle never gets here and records nothing."""
    for sel in opened.scanned:
        key = sel.repo.lower()
        tried = key in reached or key not in ranked
        record(sc, {**sel.facts, "tried": tried})


class OpenPhase:
    """§5.4, §6.1, §7.1 step 3 for one cycle. ``check`` (kill/pause/stop/drain) runs before every repo,
    every search and every CI read, and during a bounded search wait. A search limit longer than that
    wait saves ``_open.search_until`` and ends only the open phase (``ended = "rate_limited"``): the seed
    candidates and the merge go on, and nothing is created without its mention check. Open repos' issues
    are one core read each. M2: the ``active`` set filters search hits, carry-over rows, suggestions and
    named issues, and leaves their rows alone."""

    def __init__(
        self, sc: StageContext, *, bundle: ConfigBundle, prefs_open: Mapping[str, Any] | None, active: set[str],
        egress_mode: str | None, now: datetime, check: Callable[[], None],
    ) -> None:  # fmt: skip
        self.sc = sc
        self.bundle = bundle
        self.cfg = bundle.policy.discovery.open
        self.topics = list((prefs_open or {}).get("topics") or self.cfg.topics)
        self.languages = list((prefs_open or {}).get("languages_preferred") or self.cfg.languages_preferred)
        self.active = {r.lower() for r in active}
        self.now = now
        self.check = check
        self.rules = RepoRules.from_bundle(bundle)
        self.allowed = allowed_hosts(bundle)
        self.repo_strict = bundle.targets.defaults.egress == "strict"
        self.strict = effective_strict(egress_mode, "strict" if self.repo_strict else "mode")
        self.budget = SearchBudget(self.cfg.search_budget)
        self.result = OpenResult(self.budget)

    # ------------------------------------------------------------------ helpers
    def _record(self, body: dict[str, Any]) -> bool:
        return record(self.sc, body)

    def _save_until(self, until: float) -> None:
        self.sc.save(OPEN_NOTE, search_until=until)
        log.info("open discovery rate limited until %d", int(until), extra={"event": "open_discovery_rate_limited"})

    def _search(self, fn: Callable[..., list[dict[str, Any]]], q: str) -> list[dict[str, Any]]:
        try:
            return fn(q, check=self.check)  # a limit of ≤ 65 s is waited out inside, with self.check
        except GitHubRateLimited as exc:
            self._save_until(exc.until)
            self.result.ended = "rate_limited"
            raise OpenEnded(q) from exc
        except GitHubReadError as exc:
            if exc.error_class == UNAVAILABLE:
                raise
            log.warning("open discovery search failed (%s): %s", exc.error_class, q[:200],
                        extra={"event": "open_search_failed"})  # fmt: skip
            self.result.ended = "http_error"
            raise OpenEnded(q) from exc

    def _base(self, hit: RepoHit) -> float:
        return repo_score_base(hit, min_stars=self.cfg.min_stars, now=self.now, languages_preferred=self.languages,
                               language_weight=self.cfg.language_weight)  # fmt: skip

    def _ci(self, repo: str) -> str:
        self.check()
        try:
            return ci_detect.cached(self.sc, repo, self.bundle.policy.discovery.ci_facts_ttl_hours).status
        except GitHubReadError as exc:
            if exc.error_class == UNAVAILABLE:
                raise
            log.warning("CI detection failed for %s: %s", repo, exc, extra={"event": "discover_ci_failed"})
            return "unknown"

    def _probe(self, hit: RepoHit) -> Probe | None:
        """None when a read failed with a 4xx (the repo is skipped this cycle, not recorded)."""
        try:
            return probe_repo(self.sc.gh, hit, now=self.now, max_age_days=self.bundle.policy.discovery
                              .max_default_branch_age_days, allowed=self.allowed, strict=self.strict,
                              repo_strict=self.repo_strict)  # fmt: skip
        except GitHubReadError as exc:
            if exc.error_class == UNAVAILABLE:
                raise
            log.warning("open discovery read failed for %s: %s", hit.full_name, exc,
                        extra={"event": "open_repo_read_failed"})  # fmt: skip
            return None

    def _passed(self, hit: RepoHit, source: str, probe: Probe) -> Selected:
        """CI facts and the final repo score of a repo that passed every rule; recorded ``candidate``."""
        ci = self._ci(hit.full_name)
        score = repo_score(self._base(hit), ci_on_pr=ci, ci_weight=self.bundle.policy.discovery.ci_on_pr_weight,
                           branch_bonus=branch_activity_bonus(probe.commit_at, self.now))  # fmt: skip
        body = hit_facts(hit, source=source, status="candidate", commit_at=probe.commit_at, ci_on_pr=ci, score=score)
        return Selected(hit.full_name, source, body, score, hit.language)

    def _skip(self, hit: RepoHit, source: str, reason: str, commit_at: str | None = None) -> None:
        self._record(hit_facts(hit, source=source, status="skipped", skip_reason=reason, commit_at=commit_at))

    def _named_out(self, repo: str) -> bool:
        """Rule 1 by name and rule 1b (M2): no request, no write; the row stays as it is."""
        key = repo.lower()
        if key in self.active:
            log.info("open discovery: %s has an open PR of ours; not scanned", repo,
                     extra={"event": "open_repo_active"})  # fmt: skip
            return True
        owner = key.split("/", 1)[0]
        return key in self.rules.config_repos or self.rules.is_denied(key) or owner in self.rules.own_logins

    # ------------------------------------------------------------------ selection (§5.4)
    def _carry_over(self, rows: list[dict[str, Any]], slots: int) -> list[Selected]:
        out: list[Selected] = []
        for row in rows:
            if len(out) >= slots:
                break
            repo = str(row.get("repo") or "")
            age = _age(self.now, str(row.get("updated_at") or ""))
            if not repo or self._named_out(repo) or age is None or age > CARRY_MAX_AGE:
                continue  # stale facts re-enter through the search
            score = float(row.get("repo_score") or 0.0)
            body = facts_body(repo, source=str(row.get("source") or "search"), status="candidate",
                              topic=row.get("topic"), stars=row.get("stars"), language=row.get("language"),
                              license_spdx=row.get("license_spdx"), pushed_at=row.get("pushed_at"),
                              commit_at=row.get("default_branch_commit_at"), ci_on_pr=row.get("ci_on_pr"),
                              score=score)  # fmt: skip
            out.append(Selected(repo, body["source"], body, score, row.get("language")))
        return out

    def _suggestions(self, rows: list[dict[str, Any]], n: int) -> list[Selected]:
        out: list[Selected] = []
        for row in sorted(rows, key=lambda r: str(r.get("first_seen") or "")):
            if len(out) >= n:
                break
            repo = str(row.get("repo") or "")
            if not repo or self._named_out(repo):
                continue
            self.check()
            try:
                info = self.sc.gh.repo(repo)
            except GitHubReadError as exc:
                if exc.error_class == UNAVAILABLE:
                    raise
                log.warning("open discovery read failed for %s: %s", repo, exc,
                            extra={"event": "open_repo_read_failed"})  # fmt: skip
                continue
            hit = RepoHit.from_item(info) if info is not None else None
            if hit is None:
                self._record(facts_body(repo, source="hermes", status="skipped", skip_reason=NOT_FOUND))
                continue
            reason = repo_filter_reason(hit, self.rules, self.now, excluded=(), active=self.active)
            if reason == EXCLUDED:
                continue
            if reason is not None:
                self._skip(hit, "hermes", reason)
                continue
            probe = self._probe(hit)
            if probe is None:
                continue
            if probe.reason is not None:
                self._skip(hit, "hermes", probe.reason, probe.commit_at)
                continue
            sel = self._passed(hit, "hermes", probe)
            if self._record(sel.facts):
                number = row.get("suggested_issue")
                sel.suggested_issue = int(number) if isinstance(number, int) and number > 0 else None
                out.append(sel)
        return out

    def _repo_search(self, excluded: set[str], slots: int) -> list[Selected]:
        note = self.sc.load(OPEN_NOTE)
        cursor = int(note.get("cursor") or 0) if isinstance(note.get("cursor"), int) else 0
        picked, _ = select_topics(self.topics, cursor, self.cfg.topics_per_cycle)
        since = (self.now - timedelta(days=self.cfg.pushed_within_days)).date()
        hits: dict[str, RepoHit] = {}
        searched = 0
        for topic in picked:
            self.check()
            if not self.budget.take():
                break
            items = self._search(self.sc.gh.search_repos, build_repo_query(topic, self.cfg.min_stars, since))
            searched += 1
            for item in items:
                hit = RepoHit.from_item(item, topic)
                if hit is None:
                    log.debug("open discovery: unusable search item", extra={"event": "open_repo_filtered"})
                    continue
                if hit.full_name.lower() in hits:
                    continue
                reason = repo_filter_reason(hit, self.rules, self.now, excluded, self.active)
                if reason is not None:  # rules 1-12: no request, no row (finding 10)
                    extra = {"event": "open_repo_filtered", "repo": hit.full_name, "reason": reason}
                    log.debug("open discovery: %s filtered (%s)", hit.full_name, reason, extra=extra)
                    continue
                hits[hit.full_name.lower()] = hit
        # finding 14: the cursor moves right after the repo searches, before any probe or issue search
        self.sc.save(OPEN_NOTE, cursor=(cursor + searched) % max(1, len(self.topics)))
        bases = {key: self._base(hit) for key, hit in hits.items()}
        ordered = sorted(hits.values(), key=lambda h: rank_key(bases[h.full_name.lower()], h))
        window, max_probes = probe_window(slots)
        passed: list[tuple[RepoHit, Probe]] = []
        probes = 0
        for hit in ordered:
            if len(passed) >= window or probes >= max_probes:
                break
            self.check()
            probes += 1
            probe = self._probe(hit)
            if probe is None:
                continue
            if probe.reason is not None:
                self._skip(hit, "search", probe.reason, probe.commit_at)
                continue
            passed.append((hit, probe))
        scored = [self._passed(hit, "search", probe) for hit, probe in passed]
        by_name = {h.full_name: h for h, _ in passed}
        scored.sort(key=lambda s: rank_key(s.score, by_name[s.repo]))
        out: list[Selected] = []
        for sel in scored:  # the rest stay untried candidates: the next cycle takes them first
            if self._record(sel.facts) and len(out) < slots:
                out.append(sel)
        return out

    # ------------------------------------------------------------------ issues (§6.1)
    def _scan(self, sel: Selected) -> None:
        from ossbot.runner.stages.discover import issue_hash, keep_issue  # noqa: PLC0415 - discover imports us

        self.check()
        skip_labels = {s.lower() for s in self.bundle.targets.defaults.skip_labels}
        issues: list[dict[str, Any]] = []
        if sel.suggested_issue is not None:  # a named issue: labels not required (§11)
            try:
                issue = self.sc.gh.issue(sel.repo, sel.suggested_issue)
            except GitHubReadError as exc:
                if exc.error_class == UNAVAILABLE:
                    raise
                log.warning("open discovery read failed for %s: %s", sel.repo, exc,
                            extra={"event": "open_repo_read_failed"})  # fmt: skip
                return
            issues = [issue] if issue is not None else []
        else:  # one core read (per_page 100), the issue_labels OR on the client; no search, no budget
            try:
                page = self.sc.gh.issues(sel.repo)
            except GitHubReadError as exc:
                if exc.error_class == UNAVAILABLE:
                    raise
                log.warning("open discovery read failed for %s: %s", sel.repo, exc,
                            extra={"event": "open_repo_read_failed"})  # fmt: skip
                return  # stays an untried candidate for the next cycle
            issues = [i for i in page if has_any_label(i, self.cfg.issue_labels)]
        source = "hermes_request" if sel.source == "hermes" else "discover"
        seen: set[int] = set()
        for issue in issues:
            number = int(issue.get("number") or 0)
            if number <= 0 or number in seen or not keep_issue(issue, skip_labels, self.now):
                continue
            seen.add(number)
            key = f"{sel.repo}#{number}"
            self.result.hashes[key] = issue_hash(issue)
            self.result.sources[key] = source
            self.result.candidates.append(scoring.candidate_from_issue(
                sel.repo, issue, priority=OPEN_PRIORITY, category=OPEN_CATEGORY, language=sel.language,
                repo_bonus=repo_bonus(sel.score)))  # fmt: skip
        self.result.scanned.append(sel)  # tried only once the merge has used it (mark_tried)

    # ------------------------------------------------------------------ the cycle
    def run(self) -> OpenResult:
        try:
            excluded = self.sc.gk.excluded()
            carry = self.sc.gk.discovered("candidate", untried=True, limit=50)
            suggested = self.sc.gk.discovered("suggested", limit=50)
        except GkError as exc:
            log.warning("open discovery skipped: gatekeeper answered %s %s", exc.status, exc.code,
                        extra={"event": "open_phase_skipped"})  # fmt: skip
            return self.result
        slots = self.cfg.new_repos_per_cycle
        try:
            selected = self._carry_over(carry, slots)
            selected += self._suggestions(suggested, min(SUGGESTIONS_MAX, slots - len(selected)))
            if len(selected) < slots:
                selected += self._repo_search(excluded, slots - len(selected))
            for sel in sorted(selected, key=lambda s: (-s.score, s.repo.lower())):
                self._scan(sel)
        except OpenEnded:
            pass  # the merge goes on with what the open phase found so far
        return self.result
