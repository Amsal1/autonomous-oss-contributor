"""Apply the Hermes capability tier (DESIGN §15.5; PLAN item 36). Root, from ossbot-hermes-tier.service
(path unit on ``control/hermes-tier``) and from provisioning:

1. render the managed scope ``/etc/hermes/config.yaml`` for the tier the flag names (unparseable =
   ``locked``), refusing links;
2. when it changed and the system is neither killed nor paused, restart ``ossbot-hermes`` and wait for
   the API server;
3. re-run the Hermes-side Kiro probe (``--force``) and the ``/v1/toolsets`` surface check for the new
   tier; a drift or unverifiable surface soft-kills ``hermes_tool_surface``.

The result goes to ``/var/lib/ossbot/probes/hermes-surface.json`` (root:gatekeeper 0640). The chat
relay reopens only when the probe file holds a fresh pass and the surface matches the tier. Open skill
approvals were already invalidated (``tier_changed``) by the gatekeeper when it wrote the flag.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ossbot.ctl import hermes_probe as hp
from ossbot.ctl.hermes_render import MANAGED_CONFIG, render, tier_from_flag, write_managed
from ossbot.gatekeeper.chat.hermes_surface import check_toolsets

SURFACE_RESULT = Path("/var/lib/ossbot/probes/hermes-surface.json")
CONTROL = Path("/var/lib/ossbot/control")
HERMES_API = "http://127.0.0.1:8642"
GATEKEEPER_ENV = Path("/etc/ossbot/secrets/gatekeeper.env")


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/usr/bin/systemctl", *args], capture_output=True, text=True, timeout=180, check=False)


def _api_key() -> str | None:
    return hp.read_env_key(GATEKEEPER_ENV, "HERMES_API_KEY")


def wait_api(seconds: float = 90.0) -> bool:
    import socket  # noqa: PLC0415

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", 8642)) == 0:
                return True
        time.sleep(1.0)
    return False


def surface(tier: str) -> dict[str, Any]:
    key = _api_key()
    if not key:
        return {"outcome": "unavailable", "detail": "HERMES_API_KEY missing"}
    res = check_toolsets(HERMES_API, key, tier)  # type: ignore[arg-type]
    return {"outcome": res.outcome, "detail": res.detail, "unexpected": list(res.unexpected), "kill": res.kill}


def apply(*, probe: bool = True) -> dict[str, Any]:
    tier = tier_from_flag()
    changed = write_managed(render(tier), MANAGED_CONFIG)
    blocked = (CONTROL / "killed").exists() or (CONTROL / "paused").exists()
    doc: dict[str, Any] = {
        "tier": tier,
        "config_changed": changed,
        "blocked": blocked,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    active = _systemctl("is-active", "--quiet", "ossbot-hermes.service").returncode == 0
    if changed and active and not blocked:
        doc["restart"] = _systemctl("restart", "ossbot-hermes.service").returncode == 0
        active = doc["restart"] and wait_api()
    if active and not blocked:
        if probe:  # a new config runs a full session; otherwise an unchanged key with a pass only refreshes
            result = hp.probe(force=changed)
            doc["probe"] = {"verdict": result.get("verdict"), "reason": result.get("reason")}
        doc["surface"] = surface(tier)
        if doc["surface"].get("kill"):
            hp.kill("hermes_tool_surface")
    else:
        doc["surface"] = {"outcome": "unavailable", "detail": "Hermes not running"}
    hp.write_result(doc, SURFACE_RESULT)
    return doc


def apply_until_stable(*, probe: bool = True, rounds: int = 3) -> dict[str, Any]:
    """A flag written while an apply runs does not re-trigger the path unit: apply again until the
    tier the flag names is the one applied."""
    doc = apply(probe=probe)
    for _ in range(rounds - 1):
        time.sleep(1.0)  # a write racing the end of this run is still seen (the timer is the backstop)
        if tier_from_flag() == doc["tier"]:
            break
        doc = apply(probe=probe)
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot.ctl.hermes_tier", description=__doc__.split("\n")[0])
    parser.add_argument("what", choices=("apply",))
    parser.add_argument("--no-probe", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("hermes_tier must run as root", file=sys.stderr)
        return 2
    os.chdir("/")
    doc = apply_until_stable(probe=not args.no_probe)
    print(json.dumps(doc, sort_keys=True))
    return 0 if doc["surface"].get("outcome") in ("ok", "unavailable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
