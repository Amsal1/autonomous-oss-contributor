"""Child process for test_drain_listener: serve() with a fake run_down that takes ~1 s and records
its arguments in a file (argv[1]). Run by path, never imported by the suite."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ossbot.ctl import drain_listener


def main() -> int:
    record = Path(sys.argv[1])

    def run_down(timeout_s: int, kind: str, scope: str) -> int:
        time.sleep(1.0)
        record.write_text(f"{timeout_s} {kind} {scope}\n", encoding="ascii")
        return 0

    out = sys.stdout.buffer
    return drain_listener.serve(sys.stdin.buffer, out, run_down, lambda: "up", lambda _t: None)


if __name__ == "__main__":
    raise SystemExit(main())
