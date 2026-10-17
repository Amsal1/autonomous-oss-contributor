"""In-memory runtime for tests (DESIGN §10.2, §22.4).

Volumes are temp directories; every container create is rendered through the CLI model and
checked by ``ossbot.dockerfilter.rules`` exactly as the production filter would (M4), so a
template regression fails unit tests. ``start`` runs a per-entrypoint handler (default: write a
successful ``result.json``); tests and the FEAT-006 e2e suite plug in their own handlers.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ossbot.dockerfilter import rules
from ossbot.sandboxd.argv import build_argv
from ossbot.sandboxd.runtime.base import ContainerInfo, RunSpec, RuntimeFailure
from ossbot.sandboxd.runtime.cli_model import parse_run


@dataclass
class FakeContainer:
    id: str
    spec: RunSpec
    status: str = "created"
    exit_code: int = 0
    logs: bytes = b""

    def volume_path(self, runtime: FakeRuntime, target: str) -> Path | None:
        for mount in self.spec.mounts:
            if mount.target == target:
                return runtime.volume_dir(mount.source)
        return None


Handler = Callable[["FakeRuntime", FakeContainer], int]


def default_handler(runtime: FakeRuntime, container: FakeContainer) -> int:
    out = container.volume_path(runtime, "/io/output")
    if out is not None:
        (out / "result.json").write_text('{"ok":true,"error_class":null}\n', encoding="utf-8")
    return 0


def exec_handler(runtime: FakeRuntime, container: FakeContainer) -> int:
    """agent-exec keeps running (execd) until the pair is torn down; -1 = still running."""
    return -1


class FilterLookup:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime

    def container(self, ref: str) -> rules.ContainerFacts | None:
        c = self.runtime.find(ref)
        if c is None:
            return None
        labels = {
            "ossbot.sandbox": "1", "ossbot.cid": c.spec.cid, "ossbot.run": c.spec.run_id,
            "ossbot.profile": c.spec.profile,
        }  # fmt: skip
        return rules.ContainerFacts(id=c.id, name=c.spec.name, labels=labels, status=c.status)

    def network(self, ref: str) -> rules.NetworkFacts | None:
        return rules.NetworkFacts(id=ref, name=ref) if ref in self.runtime.networks else None


@dataclass
class FakeRuntime:
    root: Path
    image_digests: dict[str, frozenset[str]] = field(default_factory=dict)
    control_dir: Path | None = None
    environ: dict[str, str] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)
    containers: dict[str, FakeContainer] = field(default_factory=dict)
    networks: dict[str, str] = field(default_factory=dict)  # name -> subnet
    connections: list[tuple[str, str, str | None]] = field(default_factory=list)
    volumes: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    argvs: list[list[str]] = field(default_factory=list)
    _seq: int = 0

    def policy(self) -> rules.FilterPolicy:
        control = self.control_dir or (self.root / "control")
        control.mkdir(parents=True, exist_ok=True)
        return rules.FilterPolicy(self.image_digests, control)

    def volume_dir(self, name: str) -> Path:
        path = self.root / "volumes" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def find(self, ref: str) -> FakeContainer | None:
        if ref in self.containers:
            return self.containers[ref]
        return next((c for c in self.containers.values() if c.spec.name == ref), None)

    def _require(self, ref: str) -> FakeContainer:
        c = self.find(ref)
        if c is None:
            raise RuntimeFailure(f"no such container {ref}")
        return c

    def _filter(self, req: rules.Request) -> None:
        decision = rules.decide(req, FilterLookup(self), self.policy())
        if not decision.allow:
            raise RuntimeFailure(f"docker filter denied {req.method} {req.path}: {decision.reason}")

    # ------------------------------------------------------------------ protocol
    def create_network(self, name: str, subnet: str, internal: bool, labels: dict[str, str]) -> None:
        self.calls.append(("create_network", name))
        ipam = {"Driver": "default", "Config": [{"Subnet": subnet}], "Options": {}}
        body = {"Name": name, "Driver": "bridge", "Internal": internal, "Labels": labels, "IPAM": ipam, "Options": {}}
        self._filter(rules.Request("POST", "/v1.52/networks/create", {}, json.dumps(body).encode()))
        if name in self.networks:
            raise RuntimeFailure(f"network {name} exists")
        self.networks[name] = subnet

    def remove_network(self, name: str) -> None:
        self.calls.append(("remove_network", name))
        self._filter(rules.Request("DELETE", f"/v1.52/networks/{name}"))
        self.networks.pop(name, None)

    def connect(self, network: str, container: str, alias: str | None = None) -> None:
        self.calls.append(("connect", f"{network}:{container}"))
        self.connections.append((network, container, alias))

    def disconnect(self, network: str, container: str) -> None:
        self.calls.append(("disconnect", f"{network}:{container}"))
        self.connections = [c for c in self.connections if (c[0], c[1]) != (network, container)]

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self.calls.append(("create_volume", name))
        body = {"Name": name, "Driver": "local", "DriverOpts": {}, "Labels": labels}
        self._filter(rules.Request("POST", "/v1.52/volumes/create", {}, json.dumps(body).encode()))
        self.volumes.add(name)
        self.volume_dir(name)

    def remove_volume(self, name: str) -> None:
        self.calls.append(("remove_volume", name))
        self._filter(rules.Request("DELETE", f"/v1.52/volumes/{name}"))
        self.volumes.discard(name)

    def run(self, spec: RunSpec) -> str:
        argv = build_argv(spec)
        self.argvs.append(argv)
        create = parse_run(argv, self.environ)
        self._filter(rules.Request("POST", "/v1.52/containers/create", create.query(), create.body_bytes()))
        for mount in spec.mounts:
            if mount.source not in self.volumes and not mount.source.startswith("ossbot-task-"):
                raise RuntimeFailure(f"volume {mount.source} was not created")
            self.volume_dir(mount.source)
        self._seq += 1
        cid = f"{self._seq:064x}"
        container = FakeContainer(id=cid, spec=spec)
        self.containers[cid] = container
        self.calls.append(("create", spec.name))
        if not spec.create_only:
            self._filter(rules.Request("POST", f"/v1.52/containers/{cid}/start"))
            self.calls.append(("start", spec.name))
            container.status = "running"
            handler = self.handlers.get(spec.entrypoint, exec_handler if spec.entrypoint == "exec" else default_handler)
            container.exit_code = handler(self, container)
            container.status = "running" if container.exit_code == -1 else "exited"
        return cid

    def put_archive(self, container: str, path: str, tar: bytes) -> None:
        c = self._require(container)
        self._filter(rules.Request("PUT", f"/v1.52/containers/{c.id}/archive",
                                   {"path": [path], "noOverwriteDirNonDir": ["true"]}, tar))  # fmt: skip
        dest = c.volume_path(self, path)
        if dest is None:
            raise RuntimeFailure("archive target is not a volume")
        with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
            for member in archive.getmembers():
                data = archive.extractfile(member)
                if data is None:
                    continue
                target = dest / member.name
                target.write_bytes(data.read())
                os.chmod(target, member.mode)
        self.calls.append(("put_archive", c.spec.name))

    def inspect_status(self, container: str) -> str:
        return self._require(container).status

    def wait(self, container: str, timeout_s: int) -> int:
        c = self._require(container)
        self._filter(rules.Request("POST", f"/v1.52/containers/{c.id}/wait"))
        if c.status == "running":
            raise TimeoutError(container)
        return c.exit_code

    def logs(self, container: str, tail: int | None) -> bytes:
        return self._require(container).logs

    def get_archive(self, container: str, path: str) -> Iterator[bytes]:
        c = self._require(container)
        self._filter(rules.Request("GET", f"/v1.52/containers/{c.id}/archive", {"path": [path]}))
        src = c.volume_path(self, path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            if src is not None:
                archive.add(str(src), arcname=Path(path).name)
        yield buf.getvalue()

    def kill(self, container: str) -> None:
        c = self._require(container)
        self.calls.append(("kill", c.spec.name))
        if c.status == "running":
            c.status = "exited"
            c.exit_code = 137

    def remove(self, container: str) -> None:
        c = self.find(container)
        if c is not None:
            self.calls.append(("remove", c.spec.name))
            del self.containers[c.id]

    def list(self, labels: dict[str, str]) -> list[ContainerInfo]:
        out = []
        for c in self.containers.values():
            info_labels = {"ossbot.sandbox": "1", "ossbot.cid": c.spec.cid, "ossbot.run": c.spec.run_id,
                           "ossbot.profile": c.spec.profile}  # fmt: skip
            if all(info_labels.get(k) == v for k, v in labels.items()):
                out.append(ContainerInfo(id=c.id, name=c.spec.name, labels=info_labels, status=c.status))
        return out
