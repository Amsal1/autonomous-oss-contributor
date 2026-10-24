"""Helpers of the floor-reach test (build-env design §4.6), run as root with the deployed ossbot package.

floor_argv.py argv SHAPE NETWORK RUN_ID IMAGE_REF NET [--add-host NAME:IP]...
    The production ``docker run`` argv of one repo-code shape, one argument per line. SHAPE is ``fetch``,
    ``validate``, ``agent-exec``, ``fetch-strict`` or ``validate-strict``; NET (``web``/``base``) is what
    sandboxd's admission would put into ``OSSBOT_NET``. The argv comes from ``ossbot.sandboxd.argv.build_argv``
    for that profile's ``RunSpec`` exactly as sandboxd builds it (user, read-only root, tmpfs, ``--cap-drop
    ALL``, ``no-new-privileges``, ``--dns 127.0.0.1``, ``--dns-search .``, env, network, mounts); only
    ``--detach`` becomes ``--rm -i`` and the command becomes ``bash -s`` (the probe is fed on stdin). An
    ``--add-host`` is the one non-production flag, used by a single case of the test.
floor_argv.py public
    Exit 0 and print the first IPv4 of the live public floor when it is known (the caller never prints it);
    exit 1 when it is unknown or unreadable.
floor_argv.py servers LOG PUBLIC_FILE
    Count the squid lines of LOG whose server address (``%<a``) is a floor or the public floor.
"""

from __future__ import annotations

import ipaddress
import sys
import time
from pathlib import Path

from ossbot.common.egress_log import Floors, parse_ip, parse_line
from ossbot.common.public_floor import FLOOR_DIR, STATE_FILE, load_state, parse_public_text
from ossbot.sandboxd.argv import build_argv, job_spec

CID = "c_0000000F7S"  # the test's own cid: its task volume and network names never collide with a job's
SHAPES = {  # shape -> (template, entrypoint, env); strict shapes differ only in the pool of their network
    "fetch": ("fetch", "fetch", {"OSSBOT_ARG_REPO": "owner/repo", "OSSBOT_ARG_CLONE_DEPTH": "1"}),
    "validate": ("validate", "validate", {"OSSBOT_ARG_PLAN_FILE": "validate.json"}),
    "agent-exec": ("agent-exec", "exec", {}),
    "fetch-strict": ("fetch", "fetch", {"OSSBOT_ARG_REPO": "owner/repo", "OSSBOT_ARG_CLONE_DEPTH": "1"}),
    "validate-strict": ("validate", "validate", {"OSSBOT_ARG_PLAN_FILE": "validate.json"}),
}


def argv(shape: str, network: str, run_id: str, image_ref: str, net: str, add_hosts: list[str]) -> list[str]:
    template, entrypoint, base_env = SHAPES[shape]
    env = dict(base_env)
    if template == "agent-exec":
        if network != "none":
            env["OSSBOT_EXEC_NETWORK"] = "web"  # what sandboxd's exec_env adds for a networked agent-exec
    else:
        env["OSSBOT_NET"] = net
    spec = job_spec(template, run_id=run_id, cid=CID, entrypoint=entrypoint, image_ref=image_ref, network=network,
                    repo_mode="rw", env=env)  # fmt: skip
    out = build_argv(spec)
    assert out[:3] == ["docker", "run", "--detach"], out[:3]
    image_at = out.index(image_ref)
    assert out[image_at + 1].endswith(f"/{entrypoint}.sh") and len(out) == image_at + 2
    extra = [x for h in add_hosts for x in ("--add-host", h)]
    return ["docker", "run", "--rm", "-i", *out[3:image_at], *extra, image_ref, "bash", "-s"]


def public() -> int:
    state = load_state(FLOOR_DIR / STATE_FILE)
    if state is None or not state.is_known(time.time()) or not state.v4:
        return 1
    print(state.v4[0].ip)
    return 0


def servers(log: Path, public_file: Path) -> int:
    floors = Floors.of([], [], parse_public_text(public_file.read_text(encoding="utf-8")))
    hits = 0
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = parse_line(raw)
        if entry is None or entry.server in ("-", ""):
            continue
        ip = parse_ip(entry.server)
        if ip is not None and (floors.is_floor(ip) or floors.is_loop(ip)):
            hits += 1
    print(hits)
    return 0


def main(args: list[str]) -> int:
    if args[:1] == ["public"]:
        return public()
    if args[:1] == ["servers"] and len(args) == 3:
        return servers(Path(args[1]), Path(args[2]))
    if args[:1] == ["argv"] and len(args) >= 6 and args[1] in SHAPES and args[5] in ("web", "base"):
        rest = args[6:]
        hosts = [rest[i + 1] for i in range(len(rest) - 1) if rest[i] == "--add-host"]
        for h in hosts:
            ipaddress.ip_address(h.split(":", 1)[1])  # NAME:IP only
        print("\n".join(argv(args[1], args[2], args[3], args[4], args[5], hosts)))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
