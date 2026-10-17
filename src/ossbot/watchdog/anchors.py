"""Root audit anchors (DESIGN §12.7, N7): append the gatekeeper's current audit head to the
append-only ``/srv/ossbot/logs/audit-anchors.log`` (``chattr +a``).

Written every 10th watchdog tick (5 min) and once per connection on ``/run/ossbot/anchor.sock``
(``ossbot-anchor@.service``, ``--serve-one``: the payload is ignored, the reply is ``OK``). The DB is
opened read-only; root computes everything itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path("/srv/ossbot/gatekeeper/ossbot.db")
ANCHOR_LOG = Path("/srv/ossbot/logs/audit-anchors.log")


def audit_head(db_path: Path = DB_PATH) -> tuple[int, str] | None:
    """(seq, row_hash) of the newest audit row, or None if the DB or the table is not there yet."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        row = conn.execute("SELECT seq, row_hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return (int(row[0]), str(row[1])) if row else None


def append_anchor(db_path: Path = DB_PATH, log_path: Path = ANCHOR_LOG) -> dict[str, object] | None:
    head = audit_head(db_path)
    if head is None:
        return None
    line = {"seq": head[0], "row_hash": head[1], "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    # O_APPEND is the only write mode chattr +a allows.
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot-anchors")
    parser.add_argument("--serve-one", action="store_true", help="socket-activated: anchor, reply OK")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--log", type=Path, default=ANCHOR_LOG)
    args = parser.parse_args(argv)
    if args.serve_one:
        os.read(0, 64)  # the payload is ignored
    try:
        line = append_anchor(args.db, args.log)
    except OSError as exc:
        print(f"anchor failed: {exc}", file=sys.stderr)
        if args.serve_one:
            sys.stdout.write("ERR\n")
            sys.stdout.flush()
        return 1
    if args.serve_one:
        sys.stdout.write("OK\n")
        sys.stdout.flush()
    else:
        print(json.dumps(line) if line else "no audit rows yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
