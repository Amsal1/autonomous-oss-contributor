"""Production runtime: the pinned ``docker`` CLI with argv lists, talking only to the Docker
API filter (``DOCKER_HOST=unix:///run/ossbot/docker-filter.sock``). sandboxd is not in the
docker group; every request is re-validated by the root filter (DESIGN §10.8)."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

from ossbot.sandboxd.argv import build_argv
from ossbot.sandboxd.runtime.base import ContainerInfo, RunSpec, RuntimeFailure

FILTER_SOCKET = Path("/run/ossbot/docker-filter.sock")
DOCKER_BIN = "/usr/bin/docker"
CHUNK = 1 << 16


class DockerCliRuntime:
    def __init__(self, socket: Path = FILTER_SOCKET, docker: str = DOCKER_BIN, timeout_s: int = 120) -> None:
        self.docker = docker
        self.timeout_s = timeout_s
        home = Path(os.environ.get("OSSBOT_DOCKER_CONFIG", "/srv/ossbot/sandboxd/docker-config"))
        self._env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "DOCKER_HOST": f"unix://{socket}",
            "DOCKER_CONFIG": str(home),  # never read a user's ~/.docker (contexts, credential helpers)
            "DOCKER_CLI_HINTS": "false",
            "LANG": "C.UTF-8",
        }
        key = os.environ.get("KIRO_API_KEY")
        if key:
            self._env["KIRO_API_KEY"] = key  # only consumed by the name-only --env KIRO_API_KEY
        read = os.environ.get("GITHUB_READ_TOKEN")
        if read:
            self._env["GITHUB_READ_TOKEN"] = read  # only the name-only --env of scout/reviewer jobs (GHMCP-2)

    def _run(self, args: list[str], *, data: bytes | None = None, timeout: int | None = None) -> bytes:
        try:
            proc = subprocess.run(
                [self.docker, *args], input=data, env=self._env, capture_output=True, check=False,
                timeout=timeout or self.timeout_s,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure(f"docker {args[0]} timed out") from exc
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()[-500:]
            raise RuntimeFailure(f"docker {args[0]} failed ({proc.returncode}): {message}")
        return proc.stdout

    def create_network(self, name: str, subnet: str, internal: bool, labels: dict[str, str]) -> None:
        args = ["network", "create", "--driver", "bridge", "--subnet", subnet]
        if internal:
            args.append("--internal")
        for key, value in sorted(labels.items()):
            args += ["--label", f"{key}={value}"]
        self._run([*args, name])

    def remove_network(self, name: str) -> None:
        self._run(["network", "rm", name])

    def connect(self, network: str, container: str, alias: str | None = None) -> None:
        args = ["network", "connect"]
        if alias:
            args += ["--alias", alias]
        self._run([*args, network, container])

    def disconnect(self, network: str, container: str) -> None:
        self._run(["network", "disconnect", "--force", network, container])

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        args = ["volume", "create", "--driver", "local"]
        for key, value in sorted(labels.items()):
            args += ["--label", f"{key}={value}"]
        self._run([*args, name])

    def remove_volume(self, name: str) -> None:
        self._run(["volume", "rm", "--force", name])

    def run(self, spec: RunSpec) -> str:
        argv = build_argv(spec, docker=self.docker)
        return self._run(argv[1:]).decode().strip().splitlines()[-1]

    def put_archive(self, container: str, path: str, tar: bytes) -> None:
        # `docker cp - <c>:<dir>` streams a tar from stdin to PUT /containers/<c>/archive?path=<dir>.
        self._run(["cp", "-", f"{container}:{path}"], data=tar)

    def inspect_status(self, container: str) -> str:
        out = self._run(["container", "inspect", "--format", "{{json .State.Status}}", container])
        return str(json.loads(out))

    def wait(self, container: str, timeout_s: int) -> int:
        try:
            out = self._run(["wait", container], timeout=timeout_s)
        except RuntimeFailure as exc:
            if "timed out" in str(exc):
                raise TimeoutError(container) from exc
            raise
        return int(out.decode().strip().splitlines()[-1])

    def logs(self, container: str, tail: int | None) -> bytes:
        args = ["logs"]
        if tail is not None:
            args += ["--tail", str(int(tail))]
        proc = subprocess.run(
            [self.docker, *args, container], env=self._env, capture_output=True, check=False, timeout=self.timeout_s
        )
        if proc.returncode != 0:
            raise RuntimeFailure("docker logs failed")
        return proc.stdout + proc.stderr

    def get_archive(self, container: str, path: str) -> Iterator[bytes]:
        proc = subprocess.Popen(
            [self.docker, "cp", f"{container}:{path}", "-"], env=self._env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )  # fmt: skip
        assert proc.stdout is not None
        try:
            while chunk := proc.stdout.read(CHUNK):
                yield chunk
        finally:
            proc.stdout.close()
            code = proc.wait(timeout=self.timeout_s)
            if code != 0:
                raise RuntimeFailure("docker cp from container failed")

    def kill(self, container: str) -> None:
        self._run(["kill", container])

    def remove(self, container: str) -> None:
        self._run(["rm", "--force", "--volumes", container])

    def list(self, labels: dict[str, str]) -> list[ContainerInfo]:
        args = ["ps", "--all", "--no-trunc", "--format", "{{json .}}", "--filter", "label=ossbot.sandbox=1"]
        for key, value in sorted(labels.items()):
            if key != "ossbot.sandbox":
                args += ["--filter", f"label={key}={value}"]
        out: list[ContainerInfo] = []
        for line in self._run(args).decode().splitlines():
            doc = json.loads(line)
            parsed = dict(item.split("=", 1) for item in str(doc.get("Labels", "")).split(",") if "=" in item)
            out.append(
                ContainerInfo(id=str(doc["ID"]), name=str(doc["Names"]), labels=parsed, status=str(doc["State"]))
            )
        return out
