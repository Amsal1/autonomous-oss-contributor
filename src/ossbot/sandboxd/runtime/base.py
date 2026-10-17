"""Container runtime abstraction (DESIGN §10.2). ``DockerCliRuntime`` is production,
``FakeRuntime`` serves tests, a future ``WslcRuntime`` would implement the same protocol."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Mount:
    source: str  # volume name
    target: str  # fixed image directory
    read_only: bool = False


@dataclass(frozen=True)
class RunSpec:
    """Everything a container needs; built only by ``sandboxd.argv`` from fixed templates."""

    name: str
    run_id: str
    cid: str  # label value (lowercase slug)
    profile: str  # ossbot.profile label
    entrypoint: str  # entrypoint name; "true" for stage-in (/bin/true)
    image_ref: str  # ossbot/<image>@sha256:<digest>
    network: str  # "none" or ossbot-t-...
    mounts: tuple[Mount, ...]
    env: tuple[tuple[str, str], ...] = ()
    key: bool = False  # --env KIRO_API_KEY (name-only, value from sandboxd's environment)
    tmp_exec: bool = False
    llm_home: bool = False
    workdir: str = "/"
    home: str | None = None
    cpus: float = 1.0
    memory_mb: int = 256
    create_only: bool = False  # stage-in: created, never started
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    labels: dict[str, str]
    status: str


class RuntimeFailure(RuntimeError):
    """A runtime call failed (docker exit code non-zero, filter 403, timeout)."""


class ContainerRuntime(Protocol):
    def create_network(self, name: str, subnet: str, internal: bool, labels: dict[str, str]) -> None: ...
    def remove_network(self, name: str) -> None: ...
    def connect(self, network: str, container: str, alias: str | None = None) -> None: ...
    def disconnect(self, network: str, container: str) -> None: ...
    def create_volume(self, name: str, labels: dict[str, str]) -> None: ...
    def remove_volume(self, name: str) -> None: ...
    def run(self, spec: RunSpec) -> str: ...  # create (+ start unless create_only); returns container id
    def put_archive(self, container: str, path: str, tar: bytes) -> None: ...
    def inspect_status(self, container: str) -> str: ...
    def wait(self, container: str, timeout_s: int) -> int: ...
    def logs(self, container: str, tail: int | None) -> bytes: ...
    def get_archive(self, container: str, path: str) -> Iterator[bytes]: ...
    def kill(self, container: str) -> None: ...
    def remove(self, container: str) -> None: ...
    def list(self, labels: dict[str, str]) -> list[ContainerInfo]: ...
