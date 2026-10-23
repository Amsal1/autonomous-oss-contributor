"""End-to-end harness (marker ``e2e``, DESIGN §22.4): every real component in-process, fakes at the edges.

* gatekeeper: the real app (``gctx``: real git TCB on the ``LocalRemote`` upstream), reached by the
  runner over a TestClient as the worker uid;
* sandboxd: the real ``JobService`` and API (admission, spec rules, the probe gate, catalog checks,
  key scan, artifacts) on ``FakeRuntime``, whose create calls also pass the Docker filter rules;
  the container entrypoints are emulated by handlers that run the real ``package.sh`` and
  ``unbundle.sh`` and the stub ``tests/bin/kiro-cli`` (canned stream-json per agent);
* runner: the real ``Scheduler`` and stages, GitHub reads on ``tests/fakes/fake_github.py``.

No network, no Docker. Submissions run the real ``Submitter`` against the fake GitHub.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient

from ossbot.common.models import ModelsFile
from ossbot.common.schemas import CatalogEntry
from ossbot.gatekeeper import db
from ossbot.gatekeeper.app import create_app as create_gatekeeper
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.notify import plain_text
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.gk_client import Gatekeeper
from ossbot.runner.pipeline import Gate, StageContext
from ossbot.runner.sbx_client import Sandbox
from ossbot.runner.scheduler import Scheduler
from ossbot.sandboxd.app import PEER_KEY
from ossbot.sandboxd.app import create_app as create_sandboxd
from ossbot.sandboxd.db import Store
from ossbot.sandboxd.jobs import JobService, Settings
from ossbot.sandboxd.keyscan import KeyScanner
from ossbot.sandboxd.models import CatalogCache
from ossbot.sandboxd.networks import Networks
from ossbot.sandboxd.probes import PROBES
from ossbot.sandboxd.runtime.fake import FakeContainer, FakeRuntime
from ossbot.sandboxd.subnets import SubnetAllocator
from tests.conftest import UID_WORKER
from tests.fakes import upstream_fixture
from tests.fakes.fake_github import FakeGitHub, FakePusher
from tests.githarness import GitWorld, run_script, tgit
from tests.sandboxharness import DIGESTS
from tests.unit.gatekeeper.api_helpers import InjectPeer
from tests.unit.sandboxd.conftest import KEY, PINS

ROOT = Path(__file__).resolve().parents[2]
STUB = ROOT / "tests" / "bin" / "kiro-cli"
ENTRYPOINTS = ROOT / "sandbox" / "entrypoints"
RUNNER_UID = 2001
MODELS = ("claude-opus-5.5", "claude-sonnet-5", "claude-sonnet-5.5", "claude-haiku-4.5", "claude-opus-5")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        node = item.nodeid.replace("\\", "/")
        if ("/tests/e2e/" in node or node.startswith("tests/e2e/")) and "e2e_docker" not in item.keywords:
            item.add_marker(pytest.mark.e2e)


def fenced(doc: dict[str, Any]) -> str:
    return "Done.\n```json\n" + json.dumps(doc) + "\n```\n"


VETTING = {"ai_policy": "allowed", "claim_status": "unclaimed", "suitability": 0.8}
PROPOSAL = {
    "problem": "Export throws when a trace has no spans.", "approach": ["Guard the empty case", "Add a test"],
    "areas": ["src/app.py", "tests/test_app.py"], "kind": "bug",
    "estimate": {"files": 2, "code_lines": 10, "test_lines": 10, "class": "xs"},
    "tests": ["tests/test_app.py: empty spans"], "validation_plan": "pytest", "confidence": 0.8,
}  # fmt: skip
PLAN = {"feasible": True, "summary": "guard the empty case", "files_to_touch": ["src/app.py", "tests/test_app.py"],
        "validation_commands": [{"name": "pytest", "cmd": "python -m pytest -q"}]}  # fmt: skip
CODER = {"rationale": "guarded the empty case", "files_changed": ["src/app.py", "tests/test_app.py"],
         "tests_changed": ["tests/test_app.py"]}  # fmt: skip
REVIEW = {"verdict": "pass"}
SUMMARY = {"commit_message": "fix: handle traces without spans\n\nGuard the empty case.",
           "pr_title": "fix: handle traces without spans",
           "pr_body": "Guards the empty case and adds a test."}  # fmt: skip
EDITS = {"src/app.py": "def export(spans):\n    return [] if not spans else spans\n",
         "tests/test_app.py": "def test_empty():\n    assert True\n"}  # fmt: skip


class SandboxdGatekeeper:
    """sandboxd's calls on the gatekeeper (sandbox role), answered from the same DB."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def catalog(self) -> list[dict[str, Any]]:
        return [{"model_id": r[0], "forced": bool(r[1])} for r in
                self.ctx.conn.execute("SELECT model_id, forced FROM model_catalog")]  # fmt: skip

    def pause(self, reason: str) -> None:
        self.ctx.control.pause(reason, "uid:2002/sandbox")

    def contribution_state(self, cid: str) -> str | None:
        row = db.one(self.ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
        return None if row is None else str(row["state"])

    def probe_result(self, probe_key: str, probe: str, passed: bool, evidence_sha256: str, *,
                     verdict: str | None = None, attempts: int = 1) -> None:  # fmt: skip
        pass

    def hold(self, cid: str, body: dict[str, Any]) -> None:
        """``POST /v1/contributions/{cid}/hold`` as the sandbox role, through the gatekeeper's service."""
        from ossbot.common.schemas import HoldRequest
        from ossbot.gatekeeper.holds import HoldRefused
        from ossbot.sandboxd.jobs import HoldError

        try:
            self.ctx.holds.hold(cid, HoldRequest.model_validate(body), "uid:2002/sandbox")
        except HoldRefused as exc:
            raise HoldError(exc.status, exc.code) from exc

    def implement_egress(self, cid: str, run_id: str) -> str:
        """``POST /v1/contributions/{cid}/implement-egress`` as the sandbox role (ADR-0024): the audited decision."""
        from ossbot.gatekeeper.implement_network import decide_and_audit

        return decide_and_audit(self.ctx, cid, run_id, "uid:2002/sandbox")[0]


class StubProbes:
    """``POST /v1/probes/run``: the five probes "pass" for the current tuple (the probe jobs
    themselves are covered by tests/unit/sandboxd); models are checked against the catalog."""

    def __init__(self, jobs: JobService) -> None:
        self.jobs = jobs
        self.runs: list[dict[str, str]] = []

    def run_all(self, models: dict[str, str]) -> dict[str, Any]:
        for model in models.values():
            self.jobs.catalog.validate(model)
        self.runs.append(dict(models))
        key = self.jobs.probe_key()
        assert key is not None
        for probe in PROBES:
            self.jobs.probes.record(key, probe, True, "e" * 64)
        return dict.fromkeys(PROBES, True)  # ProbeRunner.run_all shape


@dataclass
class E2E:
    ctx: AppContext
    gk: Gatekeeper
    jobs: JobService
    runtime: FakeRuntime
    probes: StubProbes
    sc: StageContext
    world: GitWorld
    gh: FakeGitHub
    upstream: upstream_fixture.Upstream
    answers: Path
    edits: dict[str, str] = field(default_factory=lambda: dict(EDITS))
    log: list[str] = field(default_factory=list)  # every container entrypoint, in order
    said: list[str] = field(default_factory=list)
    scheduler: Scheduler | None = None
    pusher: Any = None
    upstreams: dict[str, Path] = field(default_factory=dict)  # repo -> local upstream (default: the world's)
    refetch_deps: list[str] = field(default_factory=list)  # fetch.json deps_changed of a refetch (§9.8)
    exec_networks: list[str] = field(default_factory=list)  # agent-exec's network per coder run (ADR-0024)

    # ------------------------------------------------------------------ canned answers
    def answer(self, role: str, *texts: str | dict[str, Any]) -> None:
        values = [t if isinstance(t, str) else fenced(t) for t in texts]
        (self.answers / f"{role}.json").write_text(json.dumps(values), encoding="utf-8")
        (self.answers / f"{role}.n").unlink(missing_ok=True)

    def prompts(self, role: str) -> list[str]:
        path = self.answers / f"{role}.prompts"
        return path.read_text(encoding="utf-8").split("\n\x1e\n")[:-1] if path.exists() else []

    def defaults(self) -> None:
        for role, doc in (("vetter", VETTING), ("scout", PROPOSAL), ("planner", PLAN), ("coder", CODER),
                          ("reviewer", REVIEW), ("summarizer", SUMMARY)):  # fmt: skip
            self.answer(role, doc)

    # ------------------------------------------------------------------ driving
    def ready_scheduler(self) -> Scheduler:
        if self.scheduler is None:
            self.scheduler = Scheduler(self.sc, synchronous=True, now=self.ctx.clock.now)
        return self.scheduler

    def state(self, cid: str) -> str:
        row = db.one(self.ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
        assert row is not None
        return str(row["state"])

    def why(self, cid: str) -> str:
        row = db.one(self.ctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
        sql = "SELECT stage, status, error_class, error_message FROM stage_runs WHERE cid = ? ORDER BY rowid"
        runs = [tuple(r) for r in self.ctx.conn.execute(sql, (cid,))]
        return f"{tuple(row) if row else None} runs={runs} log={self.log} said={self.said}"

    def run_until(self, cid: str, *states: str, ticks: int = 30) -> str:
        scheduler = self.ready_scheduler()
        for _ in range(ticks):
            if self.state(cid) in states:
                return self.state(cid)
            scheduler.tick()
            self.ctx.clock.advance(seconds=20)  # type: ignore[attr-defined]
        return self.state(cid)

    def has_task_volume(self, cid: str) -> bool:
        from ossbot.sandboxd.argv import Volumes

        return Volumes("x", cid).task in self.runtime.volumes

    # ------------------------------------------------------------------ gates
    def discover_one(self) -> str:
        created = self.ready_scheduler().discover(["langfuse/langfuse"])
        assert len(created) == 1, created
        return created[0]

    def start_work(self, cid: str) -> None:
        from tests.unit.gatekeeper.test_proposals import start

        assert self.run_until(cid, "PROPOSED") == "PROPOSED", self.why(cid)
        pid = db.one(self.ctx.conn, "SELECT id FROM proposal_approvals WHERE cid = ? AND status = 'pending'", (cid,))
        assert pid is not None
        start(self.ctx, str(pid[0]))

    def to_preview(self, cid: str | None = None) -> tuple[str, str]:
        """discover -> ... -> AWAITING_APPROVAL; returns (cid, the open_pr approval id)."""
        cid = cid or self.discover_one()
        self.start_work(cid)
        assert self.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", self.why(cid)
        return cid, self.pending_approval(cid)

    def pending_approval(self, cid: str) -> str:
        row = db.one(self.ctx.conn, "SELECT a.id FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                     " WHERE o.cid = ? AND a.status = 'pending' ORDER BY a.created_at DESC", (cid,))  # fmt: skip
        assert row is not None, self.why(cid)
        return str(row[0])

    def approve(self, approval_id: str) -> str:
        """Approve + confirm on the card, then the real Submitter against the fake GitHub."""
        import asyncio

        from tests.unit.gatekeeper.test_submit import confirm, submitter

        confirm(self.ctx, approval_id)
        return str(asyncio.run(submitter(self.ctx, self.gh, self.pusher).submit(approval_id)))

    def to_watching(self) -> tuple[str, int]:
        """Dry-run OFF (this test database only) -> PR opened on the fake GitHub -> WATCHING."""
        self.ctx.control.set_dry_run(False, "uid:1000/operator")
        cid, approval = self.to_preview()
        assert self.approve(approval) == "done"
        _, head, branch = self.pusher.pushes[-1]
        self.world.push_fork(self.ctx.paths.artifacts_dir / cid / "1" / "change.bundle", head, branch)
        assert self.run_until(cid, "WATCHING") == "WATCHING", self.why(cid)
        row = db.one(self.ctx.conn, "SELECT pr_number FROM contributions WHERE id = ?", (cid,))
        return cid, int(row[0])

    def maintainer_says(self, number: int, body: str, at: str) -> None:
        self.gh.comments.setdefault(("langfuse/langfuse", number), []).append(
            {"user": {"login": "maintainer"}, "body": body, "created_at": at})  # fmt: skip
        due = self.sc.clocks.wall() + 1000  # the 15 min watch interval is over (constant during a stage)
        self.sc.clocks.wall = lambda: due  # type: ignore[method-assign]

    def notes(self) -> list[str]:
        """Message texts as shown (HTML tags such as the ids' ``<code>`` stripped, entities decoded)."""
        return [plain_text(json.loads(r[0])) for r in self.ctx.conn.execute("SELECT payload_json FROM tg_outbox")
                if "text" in json.loads(r[0])]  # fmt: skip

    def pipeline_log(self) -> list[str]:
        """Container entrypoints run for contributions (the periodic catalog job left out)."""
        return [e for e in self.log if e != "list_models"]

    def jobs_of(self, entrypoint: str) -> list[str]:
        return [e for e in self.log if e.split(":", 1)[0] == entrypoint]

    def writes(self) -> list[Any]:
        return list(self.gh.writes)


# ------------------------------------------------------------------ container handlers
def _vol(rt: FakeRuntime, c: FakeContainer, target: str) -> Path:
    path = c.volume_path(rt, target)
    assert path is not None, target
    return path


def _env(c: FakeContainer) -> dict[str, str]:
    return dict(c.spec.env)


def _result(out: Path, ok: bool = True, error_class: str | None = None, head: str | None = None) -> None:
    (out / "result.json").write_text(json.dumps({"ok": ok, "error_class": error_class, "head_sha": head}))


def handlers(h: E2E) -> dict[str, Callable[[FakeRuntime, FakeContainer], int]]:
    def fetch(rt: FakeRuntime, c: FakeContainer) -> int:
        env, work, out = _env(c), _vol(rt, c, "/work"), _vol(rt, c, "/io/output")
        scout = env.get("OSSBOT_ARG_PREFETCH") == "0"
        repo = work / "repo"
        if env.get("OSSBOT_ARG_REFETCH") == "1":  # §9.8: the working tree stays, only the caches are refreshed
            h.log.append("fetch:refetch")
            head = tgit(repo, "rev-parse", "HEAD")
            (out / "fetch.json").write_text(json.dumps({"base_sha": head, "deps_changed": list(h.refetch_deps)}))
            _result(out, head=head)
            return 0
        h.log.append("fetch:scout" if scout else "fetch:build")
        if repo.exists():
            shutil.rmtree(repo)
        tgit(work, "clone", "-q", str(h.upstreams.get(env["OSSBOT_ARG_REPO"], h.world.upstream)), str(repo))
        base = env.get("OSSBOT_ARG_BASE_SHA")
        if base:
            tgit(repo, "checkout", "-q", "--detach", base)
        head = tgit(repo, "rev-parse", "HEAD")
        info = {"base_sha": head, "base_ref": env.get("OSSBOT_ARG_BASE_REF", "main"), "ecosystems": ["python"],
                "test_files": 1, "has_test_suite": True}  # fmt: skip
        (out / "fetch.json").write_text(json.dumps(info))
        if scout:
            (work / ".scout-clone").write_text("")
        _result(out, head=head)
        return 0

    def snapshot(rt: FakeRuntime, c: FakeContainer) -> int:
        work, snap = _vol(rt, c, "/work"), _vol(rt, c, "/snap")
        for path in (work / "repo").rglob("*"):
            rel = path.relative_to(work / "repo")
            if rel.parts[0] == ".git" or path.is_symlink() or not path.is_file():
                continue
            (snap / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, snap / rel)
        return 0

    def kiro(rt: FakeRuntime, c: FakeContainer) -> int:
        env, inp, out = _env(c), _vol(rt, c, "/io/input"), _vol(rt, c, "/io/output")
        role = env["ROLE"]
        h.log.append(f"kiro:{role}")
        argv = [sys.executable, str(STUB), "chat", "--v3", "--no-interactive", "--agent", f"ossbot-{role}",
                "--model", env["MODEL"], "--effort", env["EFFORT"], f"--trust-tools={env['TRUST_TOOLS']}",
                "--output-format", "stream-json"]  # fmt: skip
        prompt = (inp / env["PROMPT_FILE"]).read_bytes()
        proc = subprocess.run(argv, input=prompt, capture_output=True, check=False,
                              env={"PATH": "/usr/bin:/bin", "OSSBOT_STUB_ANSWERS": str(h.answers)})  # fmt: skip
        (out / f"{role}.events.jsonl").write_bytes(proc.stdout)
        (out / f"{role}.stderr").write_bytes(proc.stderr)
        if role == "coder" and proc.returncode == 0:  # the coder's MCP write_file calls, done by agent-exec
            exec_c = rt.find(f"ossbot-{c.spec.run_id}-exec")
            assert exec_c is not None
            h.exec_networks.append(exec_c.spec.network)
            h.world.write(_vol(rt, exec_c, "/work") / "repo", h.edits)
        _result(out, proc.returncode == 0, None if proc.returncode == 0 else f"kiro_exit_{proc.returncode}")
        return proc.returncode

    def validate(rt: FakeRuntime, c: FakeContainer) -> int:
        work, inp, out = _vol(rt, c, "/work"), _vol(rt, c, "/io/input"), _vol(rt, c, "/io/output")
        h.log.append("validate")
        spec = json.loads((inp / "validate.json").read_text())
        repo = work / "repo"
        base = spec["commands"][0]["cmd"].rsplit(" ", 1)[1]
        tgit(repo, "add", "-A", "--intent-to-add")
        diff = subprocess.run(["/usr/bin/git", "-c", "core.quotePath=true", "diff", "--no-ext-diff", base],
                              cwd=repo, capture_output=True, text=True, check=True).stdout  # fmt: skip
        (out / "logs").mkdir(exist_ok=True)
        (out / "logs" / "ossbot-diff.log").write_text(diff)
        cmds = [{"name": cmd["name"], "exit_code": 0} for cmd in spec["commands"]]
        (out / "validation.json").write_text(json.dumps({"commands": cmds}))
        _result(out)
        return 0

    def _script(rt: FakeRuntime, c: FakeContainer, script: str, args: list[str], extra: dict[str, str]) -> int:
        work, inp, out = _vol(rt, c, "/work"), _vol(rt, c, "/io/input"), _vol(rt, c, "/io/output")
        io = h.world.root / f"io-{c.spec.run_id}"
        io.mkdir(exist_ok=True)
        for name, target in (("input", inp), ("output", out)):
            if not (io / name).exists():
                (io / name).symlink_to(target)
        world = GitWorld.__new__(GitWorld)
        world.work, world.io, world.root, world.bin = work, io, h.world.root, h.world.bin
        proc = run_script(ENTRYPOINTS / script, args, world)
        return proc.returncode

    def package(rt: FakeRuntime, c: FakeContainer) -> int:
        env = _env(c)
        h.log.append("package")
        return _script(rt, c, "package.sh", ["--round-base", env["OSSBOT_ARG_ROUND_BASE"], "--package-ts",
                       env["OSSBOT_ARG_PACKAGE_TS"], "--name", env["OSSBOT_ARG_NAME"], "--email",
                       env["OSSBOT_ARG_EMAIL"]], {})  # fmt: skip

    def unbundle(rt: FakeRuntime, c: FakeContainer) -> int:
        env = _env(c)
        h.log.append(f"unbundle:{env['OSSBOT_ARG_MODE']}")
        return _script(rt, c, "unbundle.sh", ["--mode", env["OSSBOT_ARG_MODE"], "--bundle-ref",
                       env["OSSBOT_ARG_BUNDLE_REF"], "--checkout-sha", env["OSSBOT_ARG_CHECKOUT_SHA"], "--rev",
                       env["OSSBOT_ARG_REV"]], {})  # fmt: skip

    def list_models(rt: FakeRuntime, c: FakeContainer) -> int:
        out = _vol(rt, c, "/io/output")
        h.log.append("list_models")
        proc = subprocess.run([sys.executable, str(STUB), "chat", "--list-models", "--format", "json"],
                              capture_output=True, check=False, env={"PATH": "/usr/bin:/bin"})  # fmt: skip
        (out / "models.json").write_bytes(proc.stdout)
        _result(out)
        return proc.returncode

    return {"fetch": fetch, "snapshot": snapshot, "kiro": kiro, "validate": validate, "package": package,
            "unbundle": unbundle, "list_models": list_models}  # fmt: skip


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def e2e(gctx: AppContext, world: GitWorld, tmp_path: Path) -> Iterator[E2E]:
    with build_e2e(gctx, world, tmp_path) as h:
        yield h


@contextmanager
def build_e2e(gctx: AppContext, world: GitWorld, tmp_path: Path, *, repo: str = upstream_fixture.REPO) -> Iterator[E2E]:
    """The ``e2e`` harness as a context manager (``repo``: the upstream the fake GitHub serves)."""
    gh = FakeGitHub()
    upstream = upstream_fixture.seed(world, gh, repo=repo)
    gctx.models.upsert_catalog([CatalogEntry(model_id=m) for m in MODELS], "uid:2001/worker")
    gk_app = create_gatekeeper(gctx, housekeeping=False)
    store = Store(tmp_path / "sandboxd.db")
    runtime = FakeRuntime(tmp_path / "rt", image_digests=dict(DIGESTS), control_dir=gctx.paths.control_dir,
                          environ={"KIRO_API_KEY": KEY})  # fmt: skip
    runner_root = gctx.paths.srv / "runner"
    settings = Settings(
        artifacts_dir=gctx.paths.artifacts_dir, logs_dir=gctx.paths.srv / "logs",
        staging_dir=gctx.paths.srv / "sandboxd" / "staging", control_dir=gctx.paths.control_dir,
        input_roots=(runner_root, gctx.paths.artifacts_dir),
    )  # fmt: skip
    jobs = JobService(
        store=store, runtime=runtime, pins=lambda: PINS, catalog=CatalogCache(SandboxdGatekeeper(gctx).catalog,
        gctx.clock), gatekeeper=SandboxdGatekeeper(gctx), scanner=KeyScanner([KEY]), clock=gctx.clock,
        settings=settings, trust_mode=lambda: "ids", networks=Networks(runtime, SubnetAllocator(store, gctx.clock)),
        background=False, exec_egress_mode=lambda: gctx.control.egress_mode(gctx.config.egress.mode_default),
    )  # fmt: skip
    probes = StubProbes(jobs)
    sbx_app = create_sandboxd(jobs, probes, frozenset({RUNNER_UID}))  # type: ignore[arg-type]

    async def with_peer(scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        await sbx_app({**scope, PEER_KEY: RUNNER_UID}, receive, send)

    answers = tmp_path / "answers"
    answers.mkdir()
    with TestClient(InjectPeer(gk_app, UID_WORKER), base_url="http://gatekeeper") as gk_client, \
            TestClient(with_peer, base_url="http://sandboxd") as sbx_client:  # type: ignore[arg-type]  # fmt: skip
        gk = Gatekeeper(client=gk_client)
        said: list[str] = []
        sc = StageContext(
            gk=gk, sbx=Sandbox(client=sbx_client), gh=GitHubRead(gh.sync_client()),
            models=ModelsFile(gctx.paths.config_dir / "models.yaml"), gate=Gate(gctx.paths.control_dir),
            inputs_root=runner_root / "inputs", config=lambda: gctx.config, state_dir=runner_root / "state",
            notify=said.append,
        )  # fmt: skip
        h = E2E(gctx, gk, jobs, runtime, probes, sc, world, gh, upstream, answers, said=said, pusher=FakePusher(gh))
        runtime.handlers.update(handlers(h))
        h.defaults()
        yield h
    store.close()


def pass_probes(h: E2E) -> None:
    h.probes.run_all({"planner": MODELS[0], "coder": MODELS[0]})
