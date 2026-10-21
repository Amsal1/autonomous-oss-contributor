"""CI-on-PR detection (DESIGN §9.4, §9.5; post-v1 owner rule and its 2026-10-21 amendment), GET only.

Many projects don't run CI on an outside PR by itself: GitHub asks a maintainer to "Approve and run"
for first-time contributors, and some repos start CI only after a label or an ``/ok-to-test`` comment.
So the primary evidence is what actually ran on recently **merged** PRs:

1. ``pulls(state=closed)`` → up to ``SAMPLE`` merged PRs, outside authors first (``author_association``
   not OWNER/MEMBER/COLLABORATOR), then the rest by recency; for each head SHA the check runs and the
   commit statuses (``check_runs``, ``commit_status``). A PR counts when a test, lint or build check
   ran on it (completed run, or a success/failure/error status). Most PRs counted → ``yes``.
2. Workflow files (``.github/workflows/*.yml|yaml``, ≤ 8, ≤ 100 KB, ``common.yamlio.safe_load``) are
   secondary: a PR-triggered workflow without visible runs is ``unknown``, never ``no``.
3. Gating (``auto | approval_required | label_or_comment | unknown``) from the workflow triggers
   (``pull_request_target``/``labeled`` with a label condition, ``issue_comment`` with ``/ok-to-test``,
   ``/test`` or ``/retest``, ``workflow_dispatch`` only), Prow/Tide signals (``k8s-ci-robot``,
   ``tide``, ``ok-to-test`` labels), ``action_required`` runs and CONTRIBUTING wording about
   maintainers starting CI.

``no`` needs sampled merged PRs without any check and no PR-triggered workflow. A 4xx read (e.g. 403)
leaves that evidence out; a rate limit gives ``unknown`` without caching (``cached``); a
``github_unavailable`` read propagates like every other stage read. Results are cached per repo in
the runner note ``_ci`` for ``policy.discovery.ci_facts_ttl_hours``; discover and vet share it.
The read client has no write verb; this module uses only the read token's client.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ossbot.common import yamlio
from ossbot.common.ai_policy import MAINTAINER_ASSOCIATIONS, POLICY_DOC_GROUPS
from ossbot.common.ci_facts import CHECKS_MAX, CI_KINDS, CiFacts, CiGating, check_name, from_stored
from ossbot.runner.github_read import UNAVAILABLE, GitHubRateLimited, GitHubRead, GitHubReadError

if TYPE_CHECKING:
    from ossbot.runner.pipeline import StageContext

log = logging.getLogger(__name__)
NOTE = "_ci"
SAMPLE = 10  # merged PRs sampled
PULLS_READ = 30  # closed PRs listed to find them
WORKFLOWS_MAX = 8
WORKFLOW_BYTES_MAX = 100_000
CACHE_MAX = 500
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
_B = r"(?<![A-Za-z])"
_E = r"(?![A-Za-z])"
TESTS_RE = re.compile(
    rf"{_B}(?:tests?|testing|pytest|jest|vitest|mocha|go test|cargo test|ctest|tox|nox|phpunit|rspec|unittest"
    rf"|e2e|integration){_E}",
    re.IGNORECASE,
)
LINT_RE = re.compile(
    rf"{_B}(?:lint|linters?|linting|ruff|flake8|eslint|golangci|clippy|rubocop|prettier|black|mypy|pylint|isort"
    rf"|pre-commit|shellcheck|fmt|format|formatting|typecheck|style){_E}",
    re.IGNORECASE,
)
BUILD_RE = re.compile(rf"{_B}(?:build|builds|compile|compilation){_E}", re.IGNORECASE)
KIND_RES: tuple[tuple[str, re.Pattern[str]], ...] = (("tests", TESTS_RE), ("lint", LINT_RE), ("build", BUILD_RE))
LABEL_GATE_RE = re.compile(r"github\.event\.label\.name|github\.event\.pull_request\.labels|contains\([^)]*labels",
                           re.IGNORECASE)  # fmt: skip
COMMENT_GATE_RE = re.compile(r"/(?:ok-to-test|test|retest)\b", re.IGNORECASE)
PROW_BOTS = frozenset({"k8s-ci-robot", "openshift-ci-robot", "openshift-ci[bot]", "istio-testing"})
PROW_URL_RE = re.compile(r"^https://prow\.[a-z0-9.-]+/", re.IGNORECASE)  # Prow's job links (prow.k8s.io/view/…)
OK_TO_TEST_LABELS = frozenset({"ok-to-test", "needs-ok-to-test"})
APPROVAL_TEXT_RE = re.compile(
    r"\bmaintainers?\b[^.\n]{0,80}?\b(?:approve|trigger|start|kick\s+off|run)s?\b[^.\n]{0,40}?"
    r"\b(?:CI|workflows?|tests|checks|actions|pipelines?)\b"
    r"|\b(?:CI|workflows?|checks|pipelines?)\b[^.\n]{0,60}?\b(?:requires?|needs?)\b[^.\n]{0,40}?\bapproval\b",
    re.IGNORECASE,
)
COMMENT_TEXT_RE = re.compile(r"(?<![\w-])/?ok-to-test\b|(?<![\w/])/retest\b", re.IGNORECASE)
RAN_CONCLUSIONS = frozenset({"success", "failure", "neutral", "timed_out"})
RAN_STATES = frozenset({"success", "failure", "error"})
_LOCK = threading.Lock()


def kinds_of(names: Iterable[str]) -> tuple[str, ...]:
    """The CI kinds (tests, lint, build) that a set of check or job names covers, in ``CI_KINDS`` order."""
    found = {kind for name in names for kind, rx in KIND_RES if rx.search(name)}
    return tuple(k for k in CI_KINDS if k in found)


def triggers(doc: object) -> dict[str, Any]:
    """A workflow's ``on:`` as {event: config}. ruamel reads YAML 1.2 (``on`` stays a string); a
    YAML-1.1 reader turns the key into ``True``, so both are accepted."""
    if not isinstance(doc, dict):
        return {}
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(e): None for e in on if isinstance(e, str)}
    if isinstance(on, dict):
        return {str(k): v for k, v in on.items()}
    return {}


def pr_triggered(doc: object) -> bool:
    return bool(PR_EVENTS & set(triggers(doc)))


def _job_names(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return out
    for job_id, job in jobs.items():
        out.append(str(job_id))
        if isinstance(job, dict):
            if isinstance(job.get("name"), str):
                out.append(job["name"])
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    out += [str(step[key]) for key in ("name", "run", "uses") if isinstance(step.get(key), str)]
    return out


def _labeled(config: object, *, only: bool = False) -> bool:
    """``pull_request_target: {types: [labeled]}``: the workflow runs when a maintainer adds a label
    (``only``: ``labeled`` is its only activity type). A label gate also needs a label condition
    (``LABEL_GATE_RE``) unless ``labeled`` is the only type of a ``pull_request_target`` trigger."""
    types = config.get("types") if isinstance(config, dict) else None
    listed = [types] if isinstance(types, str) else [str(t) for t in types] if isinstance(types, list) else []
    return listed == ["labeled"] if only else "labeled" in listed


class _Workflows:
    """What the workflow files say: PR-triggered ones, their kinds, and gating signals."""

    def __init__(self) -> None:
        self.pr_triggered = False
        self.kinds: tuple[str, ...] = ()
        self.jobs: list[str] = []
        self.label_gate = False
        self.comment_gate = False
        self.dispatch_only = False


def _workflows(gh: GitHubRead, repo: str) -> _Workflows:
    out = _Workflows()
    entries = [e for e in gh.dir_entries(repo, ".github/workflows")
               if e.get("type") == "file" and str(e.get("name", "")).endswith((".yml", ".yaml"))]  # fmt: skip
    names: list[str] = []
    dispatch = False
    for entry in sorted(entries, key=lambda e: str(e.get("name")))[:WORKFLOWS_MAX]:
        text = gh.file_text(repo, str(entry.get("path") or f".github/workflows/{entry.get('name')}"))
        if not text or len(text) > WORKFLOW_BYTES_MAX:
            continue
        try:
            doc = yamlio.safe_load(text)
        except yamlio.YamlError:
            continue  # a malformed workflow is not evidence
        on = triggers(doc)
        if not isinstance(doc, dict) or not on:
            continue
        if PR_EVENTS & set(on):
            out.pr_triggered = True
            names += _job_names(doc)
            jobs = doc.get("jobs")
            out.jobs += [str(j) for j in jobs if str(j) not in out.jobs] if isinstance(jobs, dict) else []
            labeled = [ev for ev in PR_EVENTS if _labeled(on.get(ev))]
            only_label = _labeled(on.get("pull_request_target"), only=True)
            if only_label or (labeled and LABEL_GATE_RE.search(text)):
                out.label_gate = True
        if "issue_comment" in on and COMMENT_GATE_RE.search(text):
            out.comment_gate = True
        if "workflow_dispatch" in on:
            dispatch = True
    out.kinds = kinds_of(names)
    out.dispatch_only = dispatch and not out.pr_triggered
    return out


class _Merged:
    """What ran on the sampled merged PRs."""

    def __init__(self) -> None:
        self.sampled = 0
        self.with_ci = 0
        self.with_any = 0
        self.names: list[str] = []
        self.kinds: set[str] = set()
        self.prow = False
        self.action_required = False


def _sample(pulls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [p for p in pulls if p.get("merged_at") and (p.get("head") or {}).get("sha")]
    outside = [p for p in merged if str(p.get("author_association") or "") not in MAINTAINER_ASSOCIATIONS]
    inside = [p for p in merged if p not in outside]
    return (outside + inside)[:SAMPLE]


def _merged(gh: GitHubRead, repo: str) -> _Merged:
    out = _Merged()
    for pr in _sample(gh.pulls(repo, "closed", PULLS_READ)):
        sha = str(pr["head"]["sha"])
        labels = {str(lbl.get("name", "")).lower() for lbl in pr.get("labels") or [] if isinstance(lbl, dict)}
        if labels & OK_TO_TEST_LABELS:
            out.prow = True
        ran: list[str] = []
        for run in gh.check_runs(repo, sha):
            if run.get("conclusion") == "action_required":
                out.action_required = True
            if run.get("status") == "completed" and run.get("conclusion") in RAN_CONCLUSIONS:
                ran.append(str(run.get("name") or ""))
        for st in gh.commit_status(repo, sha):
            context = str(st.get("context") or "")
            # the combined status lists no creator ([VERIFY] 2026-10-21): Prow is seen by its Tide context
            # and its job links; ``creator`` is read when a status carries one
            creator = str((st.get("creator") or {}).get("login") or "").lower()
            if creator in PROW_BOTS or context.lower() == "tide" or PROW_URL_RE.search(str(st.get("target_url") or "")):
                out.prow = True
            if st.get("state") in RAN_STATES and context.lower() != "tide":
                ran.append(context)
        ran = [n for n in (check_name(r) for r in ran) if n]
        out.sampled += 1
        if ran:
            out.with_any += 1
        kinds = kinds_of(ran)
        if kinds:
            out.with_ci += 1
            out.kinds |= set(kinds)
            out.names += [n for n in ran if kinds_of([n]) and n not in out.names]
    return out


def _part(fn: Any, gh: GitHubRead, repo: str, what: str) -> Any:
    """One evidence part; a 4xx read leaves it out (None). Rate limits and outages propagate."""
    try:
        return fn(gh, repo)
    except GitHubReadError as exc:
        if exc.error_class == UNAVAILABLE:
            raise
        log.info("CI detection: %s of %s not readable (%s)", what, repo, exc.error_class,
                 extra={"event": "ci_detect_partial"})  # fmt: skip
        return None


def _gating(wf: _Workflows | None, merged: _Merged | None, contributing: str) -> CiGating | None:
    if (wf is not None and (wf.label_gate or wf.comment_gate)) or (merged is not None and merged.prow):
        return "label_or_comment"
    if COMMENT_TEXT_RE.search(contributing):
        return "label_or_comment"
    if (merged is not None and merged.action_required) or (wf is not None and wf.dispatch_only):
        return "approval_required"
    if APPROVAL_TEXT_RE.search(contributing):
        return "approval_required"
    return None


def _contributing(gh: GitHubRead, repo: str) -> str:
    for path in POLICY_DOC_GROUPS["contributing"]:
        try:
            text = gh.file_text(repo, path)
        except GitHubReadError as exc:
            if exc.error_class == UNAVAILABLE:
                raise
            return ""
        if text:
            return text
    return ""


def detect(gh: GitHubRead, repo: str, *, contributing: str | None = None) -> CiFacts:
    """The CI facts of ``repo`` (see the module doc). ``contributing`` saves the read when the caller
    (vet) already has the text. Raises ``GitHubRateLimited`` and ``github_unavailable`` reads."""
    merged: _Merged | None = _part(_merged, gh, repo, "merged PR checks")
    wf: _Workflows | None = _part(_workflows, gh, repo, "workflow files")
    text = contributing if contributing is not None else _contributing(gh, repo)
    gated = _gating(wf, merged, text)
    if merged is not None and merged.sampled and merged.with_ci * 2 > merged.sampled:
        kinds = tuple(k for k in CI_KINDS if k in merged.kinds)
        return CiFacts("yes", kinds, tuple(merged.names[:CHECKS_MAX]), merged.sampled, merged.with_ci,
                       gated or "auto", "merged_prs")  # fmt: skip
    sampled, with_ci = (merged.sampled, merged.with_ci) if merged is not None else (0, 0)
    if (merged is not None and merged.sampled and merged.with_any == 0 and wf is not None and not wf.pr_triggered
            and gated is None):  # fmt: skip
        return CiFacts("no", (), (), sampled, 0, "unknown", "merged_prs")
    if merged is not None and merged.with_any:
        names = tuple(merged.names[:CHECKS_MAX])
        return CiFacts("unknown", tuple(k for k in CI_KINDS if k in merged.kinds), names, sampled, with_ci,
                       gated or "unknown", "merged_prs")  # fmt: skip
    if wf is not None and wf.pr_triggered:
        jobs = tuple(n for n in (check_name(j) for j in wf.jobs) if n)[:CHECKS_MAX]
        return CiFacts("unknown", wf.kinds, jobs, sampled, with_ci, gated or "unknown", "workflows")
    return CiFacts("unknown", (), (), sampled, with_ci, gated or "unknown", "none")


def cached(sc: StageContext, repo: str, ttl_hours: int, *, contributing: str | None = None) -> CiFacts:
    """``detect`` through the runner note ``_ci`` (shared by discover and vet). A rate limit gives
    ``unknown`` and is not cached, so the next cycle reads again; other reads fail as in ``detect``."""
    key = repo.lower()
    now = sc.clocks.wall()
    with _LOCK:
        entry = (sc.load(NOTE).get("repos") or {}).get(key)
    if isinstance(entry, dict) and isinstance(entry.get("at"), int | float) and now - entry["at"] < ttl_hours * 3600:
        return from_stored(entry)
    try:
        facts = detect(sc.gh, repo, contributing=contributing)
    except GitHubRateLimited:
        log.info("CI detection for %s deferred: rate limited", repo, extra={"event": "ci_detect_rate_limited"})
        return CiFacts()
    with _LOCK:
        repos = dict(sc.load(NOTE).get("repos") or {})
        repos[key] = {**facts.stored(), "at": now}
        if len(repos) > CACHE_MAX:
            keep = sorted(repos, key=lambda r: float(repos[r].get("at") or 0), reverse=True)[:CACHE_MAX]
            repos = {r: repos[r] for r in keep}
        sc.save(NOTE, repos=repos)
    return facts
