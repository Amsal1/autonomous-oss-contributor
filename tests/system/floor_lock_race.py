"""Helper of tests/system/test_floor_lock.sh (C-M1, build-env design §4.7). Run as root:

  floor_lock_race.py race      two root PROCESSES run ossbot-pubip's update routine at once against a temporary
                               floor directory with a stubbed iptables recorder; their critical sections must
                               never overlap and the final public.txt, code-web.txt, state.json and recorded rule
                               set must agree (no lost update)
  floor_lock_race.py hold-live take the LIVE lock shared for a moment (root's trust checks pass on the real file)

Never touches the live floor files, the kernel or the proxy (the race uses a temp dir, the stubs record only).
Prints PASS/FAIL lines; exits non-zero on any failure.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ossbot.common import public_floor as pf
from ossbot.common.floor_lock import LOCK_PATH, floor_lock
from ossbot.ctl import pubip, render

ADDRS = {"one": "9.9.9.9", "two": "1.0.0.1"}  # global addresses (never the owner's)
BASE = "8.8.4.4"


def _append(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


class Recorder:
    """iptables -C/-A/-D on OSSBOT-PUBFLOOR against a rule file shared by both processes (under the lock)."""

    def __init__(self, rules: Path) -> None:
        self.rules = rules

    def _load(self) -> list[list[str]]:
        try:
            data: list[list[str]] = json.loads(self.rules.read_text())
        except FileNotFoundError:
            return []
        return data

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        op, rule = args[2], list(args[4:])
        have = self._load()
        if op == "-C":
            return subprocess.CompletedProcess(list(args), 0 if rule in have else 1, "", "")
        if op == "-A":
            have.append(rule)
        elif op == "-D":
            have.remove(rule)
        self.rules.write_text(json.dumps(have))
        return subprocess.CompletedProcess(list(args), 0, "", "")


def _worker(name: str, floor: str, journal: str, rules: str, now: float) -> None:
    def hup() -> None:  # the critical section's end: a slow reload while holding the lock
        _append(Path(journal), f"{time.monotonic():.6f} enter {name}")
        time.sleep(0.5)
        _append(Path(journal), f"{time.monotonic():.6f} leave {name}")

    pubip.update(Path(floor), {BASE, ADDRS[name]}, set(), now, ipt=Recorder(Path(rules)), hup=hup, lock_timeout_s=20)


def race() -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="ossbot-floor-race-") as tmp:
        floor = Path(tmp) / "floor"
        floor.mkdir(mode=0o755)
        os.chmod(floor, 0o755)  # noqa: S103 - root:root, not group/other-writable: the lock's trust checks pass
        journal, rules = Path(tmp) / "journal", Path(tmp) / "rules.json"
        now = time.time()
        pubip.update(floor, {BASE}, set(), now, ipt=Recorder(rules), hup=lambda: None)
        procs = [multiprocessing.Process(target=_worker, args=(n, str(floor), str(journal), str(rules), now + 60 + i))
                 for i, n in enumerate(ADDRS)]  # fmt: skip
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        codes = [p.exitcode for p in procs]
        events = [line.split() for line in journal.read_text().splitlines()]
        inside, overlap = 0, False
        for _, what, _name in events:
            inside += 1 if what == "enter" else -1
            overlap |= inside > 1
        entries = pf.parse_public_text((floor / pf.PUBLIC_FILE).read_text())
        state = json.loads((floor / pf.STATE_FILE).read_text())
        recorded = [r[r.index("-d") + 1] for r in json.loads(rules.read_text())]
        chain = list(dict.fromkeys(recorded))
        want = {f"{a}/32" for a in (BASE, *ADDRS.values())}
        checks = [
            ("both updaters exited 0", codes == [0, 0]),
            ("the critical sections never overlapped (2 enters, 2 leaves, never two inside)",
             len(events) == 4 and not overlap),
            ("public.txt keeps every address (no lost update)", set(entries) == want),
            ("the recorded OSSBOT-PUBFLOOR rules match public.txt in order", chain == entries),
            ("each address has its full rule block once", len(recorded) == 6 * len(entries)),
            ("state.json agrees with public.txt", [f"{e['ip']}/32" for e in state["v4"]] == entries),
            ("generation counts three updates", state["generation"] == 3),
            ("code-web.txt is the pool (known)", (floor / pf.CODE_WEB_FILE).read_text() == pf.code_web_text(True)),
            ("the rule shapes are the renderer's", all(r[0:2] == ["-s", render.PROXY_IP + "/32"]
                                                       for r in json.loads(rules.read_text())[::6])),
        ]  # fmt: skip
        for desc, ok in checks:
            print(f"{'PASS' if ok else 'FAIL'} floor_lock_race: {desc}")
            failures += not ok
    return 1 if failures else 0


def hold_live() -> int:
    try:
        with floor_lock(False, 10.0, LOCK_PATH):
            pass
    except OSError as exc:
        print(f"FAIL floor_lock_race: root cannot take the live lock ({exc})")
        return 1
    print("PASS floor_lock_race: root takes the live lock (its trust checks pass on the real file)")
    return 0


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("floor_lock_race: run as root", file=sys.stderr)
        sys.exit(2)
    sys.exit(race() if sys.argv[1:] == ["race"] else hold_live() if sys.argv[1:] == ["hold-live"] else 2)
