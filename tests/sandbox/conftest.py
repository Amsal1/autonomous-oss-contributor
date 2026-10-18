"""Sandbox smoke tests (DESIGN §22.5, marker ``sandbox``): real Docker behind the root filter.

Run as root on the provisioned distro (ROOTDEV pytest -m sandbox). Containers are created
through sandboxd's own code (``JobService.execute`` with the fixed templates and
``DockerCliRuntime`` on ``/run/ossbot/docker-filter.sock``), so every request also passes the
Docker filter. This JobService holds no key; the key-dependent checks go through the live
sandboxd and ``ossctl`` (product code) instead (test_profiles.py ``live_probes``).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.clock import SystemClock
from ossbot.common.images import DEFAULT_LOCK_COPY, load_image_pins
from ossbot.sandboxd.argv import Volumes, probe_spec
from ossbot.sandboxd.db import Store
from ossbot.sandboxd.jobs import JobService, Outcome, Plan, Settings
from ossbot.sandboxd.keyscan import KeyScanner
from ossbot.sandboxd.models import CatalogCache
from ossbot.sandboxd.networks import Networks
from ossbot.sandboxd.runtime.base import Mount
from ossbot.sandboxd.runtime.docker_cli import FILTER_SOCKET, DockerCliRuntime
from ossbot.sandboxd.subnets import SubnetAllocator

CONTROL = Path("/var/lib/ossbot/control")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/sandbox/" in item.nodeid.replace("\\", "/") or item.nodeid.startswith("tests/sandbox/"):
            item.add_marker(pytest.mark.sandbox)


class NoGatekeeper:
    """sandboxd's gatekeeper calls are not needed for probe-entrypoint plans."""

    paused: list[str]

    def __init__(self) -> None:
        self.paused = []

    def catalog(self) -> list[dict[str, Any]]:
        return []

    def pause(self, reason: str) -> None:
        self.paused.append(reason)

    def contribution_state(self, cid: str) -> str | None:
        return None

    def probe_result(self, probe_key: str, probe: str, passed: bool, evidence_sha256: str, *,
                     verdict: str | None = None, attempts: int = 1) -> None:  # fmt: skip
        return None


@dataclass
class Sandbox:
    svc: JobService
    runtime: DockerCliRuntime
    root: Path
    gatekeeper: NoGatekeeper

    def plan(self, cid: str, template: str, entrypoint: str = "probe", repo_mode: str = "none",
             env: dict[str, str] | None = None, inputs: dict[str, Path] | None = None,
             create_task: bool = False, timeout: int = 600) -> Plan:  # fmt: skip
        run_id = f"smk-{template.replace('_', '-')}-{secrets.token_hex(3)}"
        return Plan(
            run_id=run_id, cid=cid, template=template, entrypoint=entrypoint, repo_mode=repo_mode, env=env or {},
            inputs=inputs or {}, timeout_s=timeout, cpus=1.0, memory_mb=1024,
            dest=self.svc.settings.artifacts_dir / cid / "runs" / run_id, create_task_volume=create_task,
        )  # fmt: skip

    def run(self, plan: Plan) -> Outcome:
        return self.svc.execute(plan)

    def smoke(self, cid: str, template: str, repo_mode: str = "none", **kw: Any) -> dict[str, Any]:
        env = {"OSSBOT_PROBE": "smoke", **kw.pop("env", {})}
        out = self.run(self.plan(cid, template, repo_mode=repo_mode, env=env, **kw))
        assert out.exit_code == 0 and out.error_class is None, (out.exit_code, out.error_class, out.logs[-2000:])
        facts: dict[str, Any] = json.loads(out.files["probe.json"])
        return facts

    def fixture_task_volume(self, cid: str) -> None:
        """Fresh task volume with the probe fixture untarred (symlinks preserved), like ProbeRunner."""
        task = Volumes("x", cid).task
        with contextlib.suppress(Exception):  # absent is fine
            self.runtime.remove_volume(task)
        self.runtime.create_volume(task, {"ossbot.sandbox": "1", "ossbot.cid": cid.lower()})
        run_id = f"smk-fixture-{secrets.token_hex(3)}"
        spec = probe_spec(run_id, cid, self.svc._image("sandbox-full"), "extract_fixture", (Mount(task, "/work"),),
                          suffix="fixture")  # fmt: skip
        self.runtime.run(spec)
        try:
            assert self.runtime.wait(spec.name, 300) == 0
        finally:
            self.runtime.remove(spec.name)

    def cleanup(self, cid: str) -> None:
        self.svc.networks.release(cid)
        with contextlib.suppress(Exception):
            self.runtime.remove_volume(Volumes("x", cid).task)


def proxy_ip_on(network: str) -> str:
    out = subprocess.run(
        ["/usr/bin/docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", "ossbot-proxy"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return str(json.loads(out.stdout)[network]["IPAddress"])


def reserve_live_subnets(store: Store, clock: SystemClock) -> int:
    """The test JobService has its own subnet table; the live sandboxd's per-task networks (the
    runner is active since FEAT-006) must not be handed out again ("Pool overlaps")."""
    import ipaddress

    from ossbot.common.clock import to_iso
    from ossbot.sandboxd.profiles import POOLS
    from ossbot.sandboxd.subnets import PREFIX

    names = subprocess.run(["/usr/bin/docker", "network", "ls", "-q", "--filter", "name=ossbot-t-"],
                           capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    if not names:
        return 0
    out = subprocess.run(["/usr/bin/docker", "network", "inspect", "--format",
                          "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *names],
                         capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    rows = 0
    with store.tx() as conn:
        for n, subnet in enumerate(out):
            net = ipaddress.IPv4Network(subnet, strict=False)
            for profile, pool in POOLS.items():
                base = ipaddress.IPv4Network(pool)
                if net.subnet_of(base):
                    idx = (int(net.network_address) - int(base.network_address)) // 2 ** (32 - PREFIX)
                    row = (profile, idx, f"live-{n}", to_iso(clock.now()), pool)
                    conn.execute("INSERT OR IGNORE INTO subnets (profile, idx, cid, allocated_at, pool)"
                                 " VALUES (?, ?, ?, ?, ?)", row)  # fmt: skip
                    rows += 1
                    break
    return rows


def make_box(root: Path) -> Sandbox:
    """A JobService like sandboxd's (real filter socket), state under ``root`` (also used by the
    crash-safety child process, tests/sandbox/crash_child.py)."""
    for sub in ("artifacts", "logs", "staging", "inputs"):
        (root / sub).mkdir(exist_ok=True)
    runtime = DockerCliRuntime()
    store = Store(root / "sandboxd.db")
    clock = SystemClock()
    reserve_live_subnets(store, clock)
    gatekeeper = NoGatekeeper()
    svc = JobService(
        store=store, runtime=runtime, pins=lambda: load_image_pins(DEFAULT_LOCK_COPY),
        catalog=CatalogCache(gatekeeper.catalog, clock), gatekeeper=gatekeeper, scanner=KeyScanner([]), clock=clock,
        settings=Settings(artifacts_dir=root / "artifacts", logs_dir=root / "logs", staging_dir=root / "staging",
                          control_dir=CONTROL, input_roots=(root / "inputs",)),
        trust_mode=lambda: "ids", networks=Networks(runtime, SubnetAllocator(store, clock)), background=False,
    )  # fmt: skip
    return Sandbox(svc=svc, runtime=runtime, root=root, gatekeeper=gatekeeper)


def require_provisioned() -> None:
    if os.geteuid() != 0:
        pytest.fail("the sandbox suite runs as root (ROOTDEV pytest -m sandbox)")
    if not FILTER_SOCKET.exists():
        pytest.fail("Docker filter socket missing: provision first")
    if (CONTROL / "killed").exists() or (CONTROL / "paused").exists():
        pytest.fail("ossbot is killed or paused; resume first")
    pins = load_image_pins(DEFAULT_LOCK_COPY)
    for image in ("sandbox-full", "sandbox-llm"):
        if pins.ref(image) is None:
            pytest.fail(f"{image} digest not pinned in {DEFAULT_LOCK_COPY}")


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sandbox]:
    require_provisioned()
    root = tmp_path_factory.mktemp("sandbox")
    box = make_box(root)
    store = box.svc.store
    yield box
    for cid in ("c_SMOKETEST1", "c_SMOKETEST2", "c_SMOKETEST3", "c_SMOKETEST4", "sys-catalog"):
        box.cleanup(cid)
    store.close()
    shutil.rmtree(root, ignore_errors=True)
