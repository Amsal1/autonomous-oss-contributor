"""Pipeline jobs on real Docker (marker ``e2e_docker``; root, provisioned distro, DESIGN §22.4).

The real sandboxd ``JobService`` admits and runs each job through the Docker filter and the egress
proxy, with the pinned images and the baked entrypoints:

* the CR-004 scout clone (``fetch`` with ``prefetch: false``: depth 1, nothing from the repo runs)
  of a tiny public repository, then ``destroy`` while the item has not passed Start work;
* the build fetch -> ``validate`` (a command edits the tree, the fixed diff command records it) ->
  ``package`` (the real commit, bundle and patch) -> artifacts readable by group ``ossart`` ->
  ``destroy`` once terminal.

Kiro sessions are not part of it: the sandbox-llm image bakes the real kiro-cli and the filter
forbids bind mounts, so the stub cannot be swapped in; the Kiro path runs live in item 32
(``ossctl kiro smoke``, ``ossctl doctor --probes``). The clone is an anonymous read of a public
repository through the proxy's ``github_read`` category; nothing is written anywhere.
"""

from __future__ import annotations

import grp
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ossbot.sandboxd.argv import Volumes
from ossbot.sandboxd.profiles import JobSpec
from tests.sandbox.conftest import NoGatekeeper, Sandbox, make_box, require_provisioned

pytestmark = pytest.mark.e2e_docker
REPO = "octocat/Hello-World"  # a few KB, public, stable
SCOUT_CID = "c_E2EDOCKER1"
BUILD_CID = "c_E2EDOCKER2"


class States(NoGatekeeper):
    def __init__(self) -> None:
        super().__init__()
        self.states: dict[str, str] = {}

    def contribution_state(self, cid: str) -> str | None:
        return self.states.get(cid)


@pytest.fixture
def box(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sandbox]:
    require_provisioned()
    root = tmp_path_factory.mktemp("e2e-docker")
    sandbox = make_box(root)
    states = States()
    sandbox.svc.gatekeeper = states
    sandbox.gatekeeper = states
    yield sandbox
    for cid in (SCOUT_CID, BUILD_CID):
        sandbox.cleanup(cid)
    sandbox.svc.store.close()
    shutil.rmtree(root, ignore_errors=True)


def submit(box: Sandbox, run_id: str, cid: str, profile: str, entrypoint: str, args: dict[str, Any],
           inputs: dict[str, str] | None = None, timeout_s: int = 600) -> dict[str, Any]:  # fmt: skip
    egress = {"egress": "mode"} if profile in {"fetch", "validate"} else {}  # required there (design §12.3)
    spec = JobSpec.model_validate({"run_id": run_id, "cid": cid, "profile": profile, "entrypoint": entrypoint,
                                   "args": args, "repo_mode": "rw", "inputs": inputs or {},
                                   "timeout_s": timeout_s, **egress})  # fmt: skip
    job = box.svc.submit(spec)
    assert job["state"] == "succeeded", (job, box.svc.logs(run_id, 200)[-3000:])
    return job


def volume_exists(cid: str) -> bool:
    proc = subprocess.run(["/usr/bin/docker", "volume", "inspect", Volumes("x", cid).task], capture_output=True,
                          check=False)  # fmt: skip
    return proc.returncode == 0


def write_input(box: Sandbox, name: str, data: str) -> str:
    path = box.root / "inputs" / name
    path.write_text(data, encoding="utf-8")
    return str(path)


def test_scout_clone_runs_nothing_and_is_destroyed_before_start_work(box: Sandbox) -> None:
    job = submit(box, "e2e-scout-1", SCOUT_CID, "fetch", "fetch", {"repo": REPO, "clone_depth": 1, "prefetch": False})
    out = Path(job["artifact_dir"])
    info = json.loads((out / "fetch.json").read_text())
    assert len(info["base_sha"]) == 40 and info["prefetch"] == {} and "has_test_suite" in info
    assert not list(out.glob("prefetch-*.log"))  # no dependency fetch: nothing from the repo ran
    assert volume_exists(SCOUT_CID)
    box.gatekeeper.states[SCOUT_CID] = "PROPOSED"  # gate 1 pending: the volume holds only the scout clone
    box.svc.destroy_task(SCOUT_CID)
    assert not volume_exists(SCOUT_CID)


def test_build_fetch_validate_package(box: Sandbox) -> None:
    job = submit(box, "e2e-fetch-1", BUILD_CID, "fetch", "fetch", {"repo": REPO, "clone_depth": 2}, timeout_s=1800)
    base = json.loads((Path(job["artifact_dir"]) / "fetch.json").read_text())["base_sha"]
    diff_cmd = (f"git -c core.hooksPath=/dev/null add -A --intent-to-add && git -c core.quotePath=true diff"
                f" --no-ext-diff --no-textconv --no-color {base}")  # fmt: skip
    # c_D1CYR0EKGR: an ignored dependency tree bigger than the package job's 2 GiB /tmp (sparse: free on the
    # volume). The old copy-to-/tmp package step died here with rsync exit 11; in-place staging skips it.
    deps = ("mkdir -p node_modules/big && truncate -s 3G node_modules/big/blob"
            " && printf 'node_modules/\\n' >> .gitignore")  # fmt: skip
    plan = {"commands": [{"name": "edit", "cmd": "printf 'hello from e2e\\n' >> README", "timeout_s": 60},
                         {"name": "deps", "cmd": deps, "timeout_s": 60},
                         {"name": "ossbot-diff", "cmd": diff_cmd, "timeout_s": 120}]}  # fmt: skip
    job = submit(box, "e2e-validate-1", BUILD_CID, "validate", "validate", {"plan_file": "validate.json"},
                 {"validate.json": write_input(box, "validate.json", json.dumps(plan))})  # fmt: skip
    out = Path(job["artifact_dir"])
    assert [c["exit_code"] for c in json.loads((out / "validation.json").read_text())["commands"]] == [0, 0, 0]
    assert "+hello from e2e" in (out / "logs" / "ossbot-diff.log").read_text()
    args = {"rev": 1, "round_base": base, "package_ts": "2026-10-18T00:00:00Z", "name": "Test Person",
            "email": "test.person@example.org"}  # fmt: skip
    job = submit(box, "e2e-package-1", BUILD_CID, "package", "package", args,
                 {"commit_message.txt": write_input(box, "commit_message.txt", "docs: e2e\n")})  # fmt: skip
    out = Path(job["artifact_dir"])
    for name in ("change.bundle", "change.patch", "diffstat.json"):
        assert (out / name).is_file()
    patch = (out / "change.patch").read_text()
    assert "+hello from e2e" in patch and "+node_modules/" in patch and "node_modules/big/blob" not in patch
    ossart = grp.getgrnam("ossart").gr_gid
    if (box.root / "artifacts").stat().st_gid == ossart:  # sandboxd's real artifacts root
        assert all(p.stat().st_gid == ossart for p in out.rglob("*"))
    box.gatekeeper.states[BUILD_CID] = "DRY_RUN_DONE"
    box.svc.destroy_task(BUILD_CID)
    assert not volume_exists(BUILD_CID)
