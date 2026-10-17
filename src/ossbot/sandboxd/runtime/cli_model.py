"""Model of the Docker CLI's ``docker run``/``docker create`` -> ``POST /containers/create`` mapping,
for exactly the flags ``sandboxd.argv`` emits (M4, §23 item 15).

``FakeRuntime`` and the filter tests use it to turn every generated argv into the request the
CLI would send, so ``ossbot.dockerfilter.rules`` checks the same bytes in unit tests that it
checks in production. The bodies equal what the pinned CLI (29.9.1) sends, recorded in
``tests/unit/dockerfilter/corpus/recorded.json`` by ``record_corpus.py`` (FEAT-004); only the Env
order differs (the CLI does not keep the flag order), which no consumer depends on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FLAGS_WITH_VALUE = {
    "--pull", "--name", "--hostname", "--label", "--user", "--tmpfs", "--cap-drop", "--security-opt",
    "--pids-limit", "--memory", "--memory-swap", "--cpus", "--ulimit", "--network", "--dns", "--dns-search",
    "--mount", "--env", "--workdir",
}  # fmt: skip
BOOL_FLAGS = {"--detach", "--init", "--read-only"}


class CliModelError(ValueError):
    pass


@dataclass(frozen=True)
class CreateRequest:
    name: str
    body: dict[str, Any]
    started: bool  # docker run (create + start) vs docker create

    def query(self) -> dict[str, list[str]]:
        return {"name": [self.name]}

    def body_bytes(self) -> bytes:
        return json.dumps(self.body, separators=(",", ":")).encode("utf-8")


def _bytes(text: str) -> int:
    units = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    suffix = text[-1].lower()
    if suffix in units:
        return int(text[:-1]) * units[suffix]
    return int(text)


def _mount(spec: str) -> dict[str, Any]:
    fields: dict[str, str] = {}
    read_only = False
    for part in spec.split(","):
        if part in {"readonly", "ro"}:
            read_only = True
            continue
        key, sep, value = part.partition("=")
        if not sep:
            raise CliModelError(f"mount field {part}")
        fields[key] = value
    if fields.get("type") != "volume" or set(fields) - {"type", "src", "dst"}:
        raise CliModelError("only type=volume,src,dst[,readonly] is modelled")
    mount: dict[str, Any] = {"Type": "volume", "Source": fields["src"], "Target": fields["dst"]}
    if read_only:  # the CLI omits ReadOnly=false (omitempty)
        mount["ReadOnly"] = True
    return mount


def parse_run(argv: list[str], environ: Mapping[str, str]) -> CreateRequest:
    """``argv`` = [docker, run|create, flags..., image, cmd...]."""
    if len(argv) < 3 or argv[1] not in {"run", "create"}:
        raise CliModelError("not docker run/create")
    started = argv[1] == "run"
    values: dict[str, list[str]] = {}
    bools: set[str] = set()
    i = 2
    while i < len(argv) and argv[i].startswith("--"):
        flag = argv[i]
        if flag in BOOL_FLAGS:
            bools.add(flag)
            i += 1
        elif flag in FLAGS_WITH_VALUE:
            if i + 1 >= len(argv):
                raise CliModelError(f"{flag} needs a value")
            values.setdefault(flag, []).append(argv[i + 1])
            i += 2
        else:
            raise CliModelError(f"unmodelled flag {flag}")
    if i >= len(argv):
        raise CliModelError("image missing")
    image, cmd = argv[i], argv[i + 1 :]
    if started != ("--detach" in bools):
        raise CliModelError("run needs --detach, create must not have it")

    def one(flag: str, default: str = "") -> str:
        got = values.get(flag, [])
        if len(got) > 1:
            raise CliModelError(f"{flag} repeated")
        return got[0] if got else default

    labels: dict[str, str] = {}
    for item in values.get("--label", []):
        key, _, value = item.partition("=")
        labels[key] = value
    env: list[str] = []
    for item in values.get("--env", []):
        if "=" in item:
            env.append(item)
        elif item in environ:  # name-only form: value from the CLI's environment
            env.append(f"{item}={environ[item]}")
    tmpfs: dict[str, str] = {}
    for item in values.get("--tmpfs", []):
        path, _, options = item.partition(":")
        tmpfs[path] = options
    ulimits = []
    for item in values.get("--ulimit", []):
        name, _, limits = item.partition("=")
        soft, _, hard = limits.partition(":")
        ulimits.append({"Name": name, "Hard": int(hard or soft), "Soft": int(soft)})
    network = one("--network", "default")
    memory = _bytes(one("--memory", "0"))
    swap = one("--memory-swap")
    host_config: dict[str, Any] = {
        "Binds": None, "ContainerIDFile": "", "LogConfig": {"Type": "", "Config": {}}, "NetworkMode": network,
        "PortBindings": {}, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "AutoRemove": False,
        "VolumeDriver": "", "VolumesFrom": None, "ConsoleSize": [0, 0], "CapAdd": None,
        "CapDrop": values.get("--cap-drop"), "CgroupnsMode": "", "Dns": values.get("--dns"),
        "DnsOptions": [], "DnsSearch": values.get("--dns-search", []), "ExtraHosts": None, "GroupAdd": None,
        "IpcMode": "", "Cgroup": "", "Links": None, "OomScoreAdj": 0, "PidMode": "", "Privileged": False,
        "PublishAllPorts": False, "ReadonlyRootfs": "--read-only" in bools, "SecurityOpt": values.get("--security-opt"),
        "Tmpfs": tmpfs or None, "UTSMode": "", "UsernsMode": "", "ShmSize": 0, "Isolation": "", "CpuShares": 0,
        "Memory": memory, "NanoCpus": round(float(one("--cpus", "0")) * 1e9), "CgroupParent": "",
        "BlkioWeight": 0, "BlkioWeightDevice": [], "BlkioDeviceReadBps": [], "BlkioDeviceWriteBps": [],
        "BlkioDeviceReadIOps": [], "BlkioDeviceWriteIOps": [], "CpuPeriod": 0, "CpuQuota": 0, "CpuRealtimePeriod": 0,
        "CpuRealtimeRuntime": 0, "CpusetCpus": "", "CpusetMems": "", "Devices": [], "DeviceCgroupRules": None,
        "DeviceRequests": None, "MemoryReservation": 0, "MemorySwap": _bytes(swap) if swap else 0,
        "MemorySwappiness": -1, "OomKillDisable": False, "PidsLimit": int(one("--pids-limit", "0")) or None,
        "Ulimits": ulimits or None, "CpuCount": 0, "CpuPercent": 0, "IOMaximumIOps": 0, "IOMaximumBandwidth": 0,
        "Mounts": [_mount(m) for m in values.get("--mount", [])] or None, "MaskedPaths": None,
        "ReadonlyPaths": None, "Init": True if "--init" in bools else None,
    }  # fmt: skip
    body: dict[str, Any] = {
        "Hostname": one("--hostname"), "Domainname": "", "User": one("--user"), "AttachStdin": False,
        "AttachStdout": not started, "AttachStderr": not started, "Tty": False, "OpenStdin": False,
        "StdinOnce": False, "Env": env, "Cmd": cmd or None, "Image": image, "Volumes": {},
        "WorkingDir": one("--workdir"), "Entrypoint": None, "Labels": labels,
        "HostConfig": host_config,
        "NetworkingConfig": {"EndpointsConfig": {}},
    }  # fmt: skip
    # `docker create` (no -a, no -d) sets AttachStdout/AttachStderr like a foreground run; nothing
    # ever attaches (the attach endpoint is not in the filter's allowlist).
    return CreateRequest(name=one("--name"), body=body, started=started)
