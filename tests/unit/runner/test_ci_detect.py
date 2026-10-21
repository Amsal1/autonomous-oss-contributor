"""CI-on-PR detection (DESIGN §9.4, §9.5; post-v1 owner rule and its 2026-10-21 amendment).

Primary evidence: checks that ran on recently merged PRs; workflow files are secondary (``unknown``,
never ``no``); gating from triggers, Prow/Tide and CONTRIBUTING. All reads are GETs on the fake.
"""

from __future__ import annotations

from typing import Any

from ossbot.runner import ci_detect
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.pipeline import StageContext
from tests.fakes.fake_github import FakeGitHub

REPO = "acme/widgets"
KEY = REPO.lower()
PR_WORKFLOW = """
name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest -q
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
"""


def merged(fake: FakeGitHub, sha: str, *, assoc: str = "CONTRIBUTOR", labels: tuple[str, ...] = ()) -> None:
    fake.add_pull(REPO, state="closed", merged_at="2026-10-01T00:00:00Z", head={"sha": sha, "label": f"x:{sha}"},
                  author_association=assoc, labels=[{"name": n} for n in labels])  # fmt: skip


def run(name: str, conclusion: str = "success") -> dict[str, Any]:
    return {"name": name, "status": "completed", "conclusion": conclusion}


def make(**files: str) -> tuple[FakeGitHub, GitHubRead]:
    fake = FakeGitHub()
    fake.add_repo(REPO)
    for path, text in files.items():
        fake.files[(KEY, path)] = text
    return fake, GitHubRead(fake.sync_client(), sleep=lambda s: None)


def only_gets(fake: FakeGitHub) -> None:
    assert fake.writes == [] and {r.method for r in fake.requests} == {"GET"}


def test_merged_pr_checks_present_is_yes_with_counts() -> None:
    fake, gh = make(**{".github/workflows/ci.yml": PR_WORKFLOW})
    for i in range(5):
        merged(fake, f"s{i}")
        if i < 4:
            fake.check_runs[(KEY, f"s{i}")] = [run("test (3.12)"), run("lint", "failure"), run("docs", "skipped")]
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.kinds, facts.sampled, facts.with_checks) == ("yes", ("tests", "lint"), 5, 4)
    assert facts.checks == ("test (3.12)", "lint") and facts.source == "merged_prs" and facts.gating == "auto"
    only_gets(fake)


def test_push_only_workflow_but_a_lint_check_on_merged_prs_is_yes() -> None:
    fake, gh = make(**{".github/workflows/ci.yml": "on: [push]\njobs:\n  build: {runs-on: x}\n"})
    merged(fake, "a")
    fake.check_runs[(KEY, "a")] = [run("lint")]
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.kinds, facts.source) == ("yes", ("lint",), "merged_prs")


def test_a_commit_status_context_counts() -> None:
    fake, gh = make()
    merged(fake, "a")
    fake.statuses[(KEY, "a")] = [{"context": "ci/circleci: test", "state": "success", "creator": {"login": "c"}}]
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.kinds, facts.checks) == ("yes", ("tests",), ("ci/circleci: test",))


def test_workflows_present_but_no_runs_is_unknown_not_no() -> None:
    fake, gh = make(**{".github/workflows/ci.yml": PR_WORKFLOW})
    for sha in ("a", "b"):
        merged(fake, sha)  # merged PRs, but nothing ran on them (gated, or CI added later)
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.source, facts.kinds) == ("unknown", "workflows", ("tests", "lint"))
    assert facts.checks == ("test", "lint") and facts.gating == "unknown"


def test_pull_request_target_with_a_label_condition_is_label_gated() -> None:
    wf = """
on:
  pull_request_target:
    types: [labeled, synchronize]
jobs:
  e2e:
    if: contains(github.event.pull_request.labels.*.name, 'safe to test')
    runs-on: ubuntu-latest
    steps: [{run: make e2e}]
"""
    fake, gh = make(**{".github/workflows/e2e.yaml": wf})
    merged(fake, "a")
    fake.check_runs[(KEY, "a")] = [run("e2e")]
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.gating) == ("yes", "label_or_comment")
    only_label = "on:\n  pull_request_target:\n    types: [labeled]\njobs:\n  t: {runs-on: x}\n"
    _, gh2 = make(**{".github/workflows/t.yml": only_label})
    assert ci_detect.detect(gh2, REPO).gating == "label_or_comment"
    plain = "on:\n  pull_request:\n    types: [opened, synchronize, labeled]\njobs:\n  t: {runs-on: x}\n"
    _, gh3 = make(**{".github/workflows/t.yml": plain})  # re-runs on a label, no label condition
    assert ci_detect.detect(gh3, REPO).gating == "unknown"


def test_a_prow_ok_to_test_repo() -> None:
    fake, gh = make()
    for sha in ("a", "b", "c"):
        merged(fake, sha, labels=("lgtm",))
        fake.statuses[(KEY, sha)] = [  # the combined-status shape: no ``creator``
            {"context": "pull-widgets-unit-test", "state": "success",
             "target_url": "https://prow.k8s.io/view/gs/x/pr-logs/1"},
            {"context": "tide", "state": "success", "target_url": "https://prow.k8s.io/tide"},
        ]  # fmt: skip
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.gating, facts.with_checks) == ("yes", "label_or_comment", 3)
    assert facts.checks == ("pull-widgets-unit-test",)  # tide is a merge bot, not a check
    labelled, gh_l = make()
    merged(labelled, "a", labels=("ok-to-test",))
    labelled.statuses[(KEY, "a")] = [{"context": "unit-test", "state": "success"}]
    assert ci_detect.detect(gh_l, REPO).gating == "label_or_comment"
    comment = (
        "on:\n  issue_comment:\n    types: [created]\njobs:\n  t:\n    if: github.event.comment.body == '/ok-to-test'\n"
    )
    _, gh2 = make(**{".github/workflows/ok.yml": comment})
    assert ci_detect.detect(gh2, REPO).gating == "label_or_comment"


def test_approval_required_from_runs_dispatch_or_contributing() -> None:
    fake, gh = make(**{".github/workflows/ci.yml": PR_WORKFLOW})
    merged(fake, "a")
    fake.check_runs[(KEY, "a")] = [run("test"), {"name": "e2e", "status": "completed", "conclusion": "action_required"}]
    assert ci_detect.detect(gh, REPO).gating == "approval_required"
    _, gh2 = make(**{".github/workflows/m.yml": "on: workflow_dispatch\njobs:\n  test: {runs-on: x}\n"})
    assert ci_detect.detect(gh2, REPO).gating == "approval_required"
    text = "A maintainer needs to approve the CI workflows for first-time contributors."
    _, gh3 = make(**{"CONTRIBUTING.md": text})
    assert ci_detect.detect(gh3, REPO).gating == "approval_required"
    assert ci_detect.detect(gh3, REPO, contributing="").gating == "unknown"  # the caller's text is used


def test_no_ci_anywhere_is_no() -> None:
    fake, gh = make(**{"README.md": "# w"})
    merged(fake, "a")
    merged(fake, "b")
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.sampled, facts.source) == ("no", 2, "merged_prs")
    _, empty = make()  # no merged PR at all: nothing to conclude from
    assert ci_detect.detect(empty, REPO).status == "unknown"


def test_a_minority_of_prs_with_checks_is_unknown() -> None:
    fake, gh = make()
    for sha in ("a", "b", "c"):
        merged(fake, sha)
    fake.check_runs[(KEY, "a")] = [run("unit tests")]
    assert ci_detect.detect(gh, REPO).status == "unknown"


def test_outside_authors_are_sampled_first() -> None:
    fake, gh = make()
    for i in range(12):
        merged(fake, f"m{i}", assoc="MEMBER")
    merged(fake, "out", assoc="FIRST_TIME_CONTRIBUTOR")
    ci_detect.detect(gh, REPO)
    shas = [r.path.split("/")[5] for r in fake.requests if r.path.endswith("/check-runs")]
    assert shas[0] == "out" and len(shas) == ci_detect.SAMPLE


def test_yaml_true_key_and_malformed_yaml() -> None:
    assert ci_detect.pr_triggered({True: ["pull_request"]}) and ci_detect.pr_triggered({"on": "pull_request_target"})
    assert not ci_detect.pr_triggered({"on": {"push": None}}) and not ci_detect.pr_triggered("x")
    fake, gh = make(
        **{".github/workflows/bad.yml": "on: [pull_request\njobs: {", ".github/workflows/ok.yml": PR_WORKFLOW}
    )
    merged(fake, "a")
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.source) == ("unknown", "workflows")  # bad.yml skipped, ok.yml read


def test_a_403_on_check_runs_is_unknown() -> None:
    fake, gh = make()
    merged(fake, "a")
    fake.failures[f"GET /repos/{REPO}/commits/a/check-runs"] = [403]
    facts = ci_detect.detect(gh, REPO)
    assert (facts.status, facts.source) == ("unknown", "none")


def test_kinds_of() -> None:
    assert ci_detect.kinds_of(["Unit-Tests", "golangci-lint", "build (linux)"]) == ("tests", "lint", "build")
    assert ci_detect.kinds_of(["latest", "contest", "deploy"]) == ()


# ------------------------------------------------------------------ the shared cache
def test_cache_honours_the_ttl(sc: StageContext, github: FakeGitHub) -> None:
    github.add_repo(REPO)
    merged(github, "a")
    github.check_runs[(KEY, "a")] = [run("pytest")]
    assert ci_detect.cached(sc, REPO, 1).status == "yes"
    reads = len(github.requests)
    assert ci_detect.cached(sc, REPO, 1).status == "yes" and len(github.requests) == reads  # cached
    sc.clocks.advance(3601)  # type: ignore[attr-defined]
    ci_detect.cached(sc, REPO, 1)
    assert len(github.requests) > reads
    assert sc.load(ci_detect.NOTE)["repos"][KEY]["ci_on_pr"] == "yes"


def test_a_rate_limit_falls_back_to_unknown_and_is_not_cached(sc: StageContext, github: FakeGitHub) -> None:
    github.add_repo(REPO)
    merged(github, "a")
    github.check_runs[(KEY, "a")] = [run("pytest")]
    github.failures[f"GET /repos/{REPO}/pulls"] = [429]
    facts = ci_detect.cached(sc, REPO, 168)
    assert (facts.status, facts.source) == ("unknown", "none")
    assert KEY not in (sc.load(ci_detect.NOTE).get("repos") or {})
    sc.gh.limited_until = 0.0  # the reset passed
    assert ci_detect.cached(sc, REPO, 168).status == "yes"
