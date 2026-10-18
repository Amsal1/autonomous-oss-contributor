"""The scout clone before gate 1 (FEAT-006 R1-5, owner decision A; DESIGN §9.5a, ADR-0005 addendum).

1. plain shallow clone: depth 1, single branch, no tags, no submodules, no LFS smudge, https only,
   no hooks, no credentials;
2. nothing from the repo runs before Start work (no dependency fetch, build, plan or implement);
3. size cap -> ``snapshot_too_large``, a clean SKIPPED with a note;
4. the task volume is destroyed when the scout stage ends, and swept for terminal and
   pre-Start-work items (skip, never, later, expired, superseded) also while killed or draining;
5. the scout session reads the clone read-only in the ``research`` profile (no shell, no write).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import Scheduler
from ossbot.runner.stages import common, scout
from ossbot.sandboxd import profiles
from ossbot.sandboxd.argv import job_spec
from ossbot.sandboxd.jobs import PRE_START_STATES
from tests.conftest import insert_contribution
from tests.unit.gatekeeper.test_proposals import sample
from tests.unit.runner.conftest import FakeSbx, fenced

ROOT = Path(__file__).resolve().parents[3]
FETCH_SH = (ROOT / "sandbox" / "entrypoints" / "fetch.sh").read_text(encoding="utf-8")


def state(ctx: AppContext, cid: str) -> tuple[str, str | None]:
    row = db.one(ctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return str(row["state"]), row["state_reason"]


# ------------------------------------------------------------------ 1. the clone command
def test_fetch_sh_clone_is_plain_shallow_https_only() -> None:
    clone = re.search(r'"\$\{G\[@\]\}" clone (.+?)\\\n\s+(.+?) \|\|', FETCH_SH, re.DOTALL)
    assert clone is not None
    argv = clone.group(1) + clone.group(2)
    for flag in ("--single-branch", "--depth", "--no-tags", "--no-recurse-submodules"):
        assert flag in argv
    assert "--recurse-submodules " not in argv.replace("--no-recurse-submodules", "")
    g = re.search(r"^G=\(git (.+?)\)", FETCH_SH, re.MULTILINE | re.DOTALL)
    assert g is not None
    for opt in ("core.hooksPath=/dev/null", "protocol.allow=never", "protocol.https.allow=always",
                "submodule.recurse=false", "credential.helper="):  # fmt: skip
        assert f"-c {opt}" in g.group(1)
    assert "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null" in FETCH_SH  # no helper or LFS filter config
    assert '[[ "$PREFETCH_ON" == 1 ]] || export GIT_LFS_SKIP_SMUDGE=1' in FETCH_SH
    assert "https://github.com/$SLUG.git" in FETCH_SH


def test_every_fetch_starts_from_a_fresh_clone() -> None:
    """A re-queued fetch finds a partial clone and a new version after a rescope finds the old
    build tree (F006-R2-1): git clone refuses a non-empty directory, so fetch.sh always clears it."""
    assert re.search(r'^rm -rf "\$REPO" "\$SCOUT_MARK"$', FETCH_SH, re.MULTILINE)
    assert FETCH_SH.index('rm -rf "$REPO" "$SCOUT_MARK"') < FETCH_SH.index("clone -q --single-branch")


def test_scout_clone_args_are_fixed_and_carry_no_credentials() -> None:
    args = profiles.FetchArgs(repo="langfuse/langfuse", clone_depth=1, prefetch=False)
    for bad in ({"clone_depth": 2}, {"base_ref": "main"}):
        with pytest.raises(ValidationError):
            profiles.FetchArgs.model_validate({"repo": "langfuse/langfuse", "prefetch": False, "clone_depth": 1,
                                               **bad})  # fmt: skip
    env = {"OSSBOT_ARG_REPO": args.repo, "OSSBOT_ARG_CLONE_DEPTH": "1", "OSSBOT_ARG_PREFETCH": "0"}
    spec = job_spec("fetch", run_id="scout-1", cid="c_01ABCDEFGH", entrypoint="fetch",
                    image_ref="ossbot/sandbox-full@sha256:" + "0" * 64, network="ossbot-t-x", repo_mode="rw",
                    env=env)  # fmt: skip
    assert spec.key is False  # the fetch profile never gets the Kiro key
    assert {k for k, _ in spec.env} == {"OSSBOT_ARG_REPO", "OSSBOT_ARG_CLONE_DEPTH", "OSSBOT_ARG_PREFETCH"}


# ------------------------------------------------------------------ 2. nothing from the repo runs
def test_scout_mode_runs_nothing_from_the_repo() -> None:
    prefetch_fn = re.search(r"^prefetch\(\) \{.*?^\}", FETCH_SH, re.MULTILINE | re.DOTALL)
    assert prefetch_fn is not None and '[[ "$PREFETCH_ON" == 1 ]] || return 0' in prefetch_fn.group(0)
    after_clone = FETCH_SH.split("HEAD_SHA=", 1)[1]
    commands = [ln.strip() for ln in after_clone.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for line in commands:  # every repo-derived command goes through prefetch (skipped in scout mode)
        for tool in ("npm ", "pnpm ", "yarn ", "uv sync", "uv pip", "go mod", "gradlew", "tofu ", "helm ", "cargo ",
                     "mise install", "make "):  # fmt: skip
            if tool in line:
                assert "prefetch " in line, line


def test_no_fetch_plan_or_implement_job_before_start_work(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    sbx.kiro("vetter", fenced({"ai_policy": "allowed", "claim_status": "unclaimed", "suitability": 0.8}))
    sbx.kiro("scout", fenced(sample()))
    cid = insert_contribution(ctx, State.DISCOVERED)
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    scheduler.first_start_done = True
    scheduler.last_discovery = scheduler.last_catalog = ctx.clock.now()
    for _ in range(4):
        scheduler.tick()
    assert state(ctx, cid)[0] == "PROPOSED"
    fetches = [j["args"] for j in sbx.jobs if j["entrypoint"] == "fetch"]
    assert fetches == [{"repo": "langfuse/langfuse", "clone_depth": 1, "prefetch": False}]  # the scout clone only
    roles = [j["args"]["role"] for j in sbx.jobs if j["entrypoint"] == "kiro"]
    assert roles == ["vetter", "scout"]
    assert not [j for j in sbx.jobs if j["entrypoint"] in {"validate", "package", "unbundle"}]


# ------------------------------------------------------------------ 3. size cap
def test_snapshot_too_large_skips_cleanly_with_a_note(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    said: list[str] = []
    sc.notify = said.append
    sbx.handlers["fetch"] = lambda spec, out: 3
    sbx.error_class["fetch"] = "snapshot_too_large"  # fetch.sh result.json
    first = insert_contribution(ctx, State.VETTED, issue_number=11)
    second = insert_contribution(ctx, State.VETTED, issue_number=12)
    for cid in (first, second):
        scout.run(sc, sc.gk.contribution(cid))
        assert state(ctx, cid) == ("SKIPPED", "snapshot_too_large")
    assert not [j for j in sbx.jobs if j["entrypoint"] == "kiro"]  # no scout session on a refused clone
    assert len(said) == 1 and "too large to scout" in said[0] and str(scout.SCOUT_MAX_MB) in said[0]
    user = insert_contribution(ctx, State.VETTED, issue_number=13, source="user")
    scout.run(sc, sc.gk.contribution(user))
    assert len(said) == 2 and "#13" in said[1]  # 👤: always told
    assert sbx.destroyed == [first, second, user]
    assert "fail snapshot_too_large" in FETCH_SH and f"SCOUT_MAX_MB={scout.SCOUT_MAX_MB}" in FETCH_SH


# ------------------------------------------------------------------ 4. task-volume lifecycle
def test_scout_destroys_its_volume_on_success_and_on_interrupt(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    sbx.kiro("scout", fenced(sample()))
    ok = insert_contribution(ctx, State.VETTED)
    scout.run(sc, sc.gk.contribution(ok))
    assert state(ctx, ok)[0] == "PROPOSED" and sbx.destroyed == [ok]  # gate 1 waits without a volume
    cut = insert_contribution(ctx, State.VETTED)
    sbx.interrupt.add("kiro")  # drain timeout / crash mid-session
    with pytest.raises(Exception):  # noqa: B017 - Interrupted, re-queued by the scheduler
        scout.run(sc, sc.gk.contribution(cut))
    assert sbx.destroyed == [ok, cut]
    assert common.volume_cids(sc) == []


@pytest.mark.parametrize("stopped", ["killed", "draining", "running"])
def test_sweep_destroys_leftover_volumes(sc: StageContext, ctx: AppContext, sbx: FakeSbx, stopped: str) -> None:
    """A kill or crash mid-scout leaves the clone; skip, never, later, expired and superseded items
    and terminal ones are swept, items past Start work are not."""
    gone = {
        "skip": insert_contribution(ctx, State.PROPOSAL_REJECTED, state_reason="skipped"),
        "never": insert_contribution(ctx, State.PROPOSAL_REJECTED, state_reason="never_repo"),
        "later": insert_contribution(ctx, State.PROPOSAL_SNOOZED),
        "expired": insert_contribution(ctx, State.PROPOSAL_EXPIRED),
        "superseded": insert_contribution(ctx, State.PROPOSED),
        "killed_mid_scout": insert_contribution(ctx, State.SCOUTING),
        "merged": insert_contribution(ctx, State.MERGED),
    }
    kept = {s: insert_contribution(ctx, getattr(State, s)) for s in ("PROPOSAL_APPROVED", "FETCHING", "WATCHING")}
    for cid in [*gone.values(), *kept.values()]:
        common.note_volume(sc, cid, True)
    if stopped == "killed":
        ctx.control.kill("test", "uid:1000/operator")
    elif stopped == "draining":
        ctx.control.drain("uid:0/operator", "ossctl down", timeout_s=60, scope="distro")
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    if stopped == "running":
        assert scheduler.sweep_volumes() == len(gone)  # every tick starts with it
    else:
        assert scheduler.tick().skipped == "stopped"  # the sweep still ran: cleanup only
    assert sorted(sbx.destroyed) == sorted(gone.values())
    assert sorted(common.volume_cids(sc)) == sorted(kept.values())
    assert set(PRE_START_STATES) >= {"PROPOSED", "PROPOSAL_SNOOZED", "PROPOSAL_EXPIRED", "PROPOSAL_REJECTED"}
    assert not set(PRE_START_STATES) & set(kept)


# ------------------------------------------------------------------ 5. read-only, no shell
def test_scout_session_is_read_only_research(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    sbx.kiro("scout", fenced(sample()))
    scout.run(sc, sc.gk.contribution(insert_contribution(ctx, State.VETTED)))
    session = next(j for j in sbx.jobs if j["entrypoint"] == "kiro")
    assert (session["profile"], session["repo_mode"]) == ("research", "snapshot")
    assert profiles.ROLE_REPO_MODE["scout"] == "snapshot"
    tools = set(profiles.TRUST_TOOLS["scout"].split(","))
    assert tools <= {"read", "web_search", "web_fetch"}  # no shell, no write
