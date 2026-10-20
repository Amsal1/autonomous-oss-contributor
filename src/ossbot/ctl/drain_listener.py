"""Root drain channel for the Windows shutdown blocker (CR-001 6a; ADR-0021 addendum post-v1).

Windows refuses to initialise a new process in a session that is ending (0xC0000142 /
0xC000026B), so the blocker cannot start ``down.ps1`` at WM_QUERYENDSESSION. Instead it starts
``wsl.exe -d ossbot -u root --exec ossctl drain-listen`` once at logon and keeps its pipes; at
shutdown it only writes one line.

The grammar is fixed and stop-only (I8/I9): no line can express a go action. One ASCII line of at
most 64 bytes at a time:

- ``ping`` -> ``pong <lifecycle-state>``
- ``down <timeout 0..120> <caller [A-Za-z0-9_-]{1,40}>`` -> ``ack``, then the same work as
  ``ossctl down --kind graceful --timeout N`` (scope distro), then ``drained exit=<n> elapsed=<s>``
- anything else -> ``error grammar`` (the line is ignored)

Once a ``down`` is accepted the listener ignores SIGHUP and SIGPIPE and finishes the drain even when
its stdin or stdout closes (the Windows side may be ended first). EOF on stdin otherwise ends it
quietly. It never reads or writes the kill/pause flags; ``run_down`` writes the lifecycle flag and
the shutdown marker exactly as ``ossctl down`` does.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

MAX_LINE = 64
MAX_TIMEOUT_S = 120
_BOM = b"\xef\xbb\xbf"
_PING = re.compile(r"ping")
_DOWN = re.compile(r"down (0|[1-9][0-9]{0,2}) ([A-Za-z0-9_-]{1,40})")

RunDown = Callable[[int, str, str], int]


@dataclass(frozen=True)
class Ping:
    pass


@dataclass(frozen=True)
class Down:
    timeout_s: int
    caller: str


Command = Ping | Down


def parse(line: bytes) -> Command | None:
    """One line (without its newline) -> a command, or None when it is outside the grammar."""
    if len(line) > MAX_LINE:
        return None
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return None
    if _PING.fullmatch(text):
        return Ping()
    m = _DOWN.fullmatch(text)
    if m is None:
        return None
    timeout = int(m.group(1))
    if timeout > MAX_TIMEOUT_S:
        return None
    return Down(timeout, m.group(2))


def read_line(inp: BinaryIO) -> bytes | None:
    """The next line without CR/LF; None at EOF. A line longer than MAX_LINE is consumed to its end
    and returned whole-length-marked (MAX_LINE + 1 bytes), so ``parse`` refuses it."""
    raw = inp.readline(MAX_LINE + 2)
    if not raw:
        return None
    if not raw.endswith(b"\n") and len(raw) > MAX_LINE:
        while True:  # drop the rest of the over-long line
            rest = inp.readline(4096)
            if not rest or rest.endswith(b"\n"):
                break
        return b"x" * (MAX_LINE + 1)
    return raw.rstrip(b"\n").rstrip(b"\r")


def _detach() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)


class _Out:
    """The channel's stdout; a closed pipe is remembered, never fatal."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.broken = False

    def send(self, text: str) -> None:
        if self.broken:
            return
        try:
            self.stream.write(text.encode("ascii") + b"\n")
            self.stream.flush()
        except (OSError, ValueError):
            self.broken = True


def serve(
    inp: BinaryIO,
    out: BinaryIO,
    run_down: RunDown,
    lifecycle_state: Callable[[], str],
    log: Callable[[str], None],
    detach: Callable[[], None] = _detach,
) -> int:
    """The listener loop. Returns 0 at EOF (or once the output is gone after a drain)."""
    channel = _Out(out)
    first = True
    while True:
        line = read_line(inp)
        if line is None:
            log("stdin closed; exiting")
            return 0
        if first:
            first = False
            line = line.removeprefix(_BOM)  # .NET's stdin writer may send a UTF-8 preamble
        cmd = parse(line)
        if cmd is None:
            log(f"refused a line outside the grammar ({len(line)} bytes)")
            channel.send("error grammar")
            continue
        if isinstance(cmd, Ping):
            try:
                state = lifecycle_state()
            except (OSError, ValueError) as exc:
                state = f"unknown-{type(exc).__name__}"
            channel.send(f"pong {state}")
            continue
        detach()  # the drain must survive the Windows side going away
        log(f"down timeout={cmd.timeout_s} caller={cmd.caller}: start")
        channel.send("ack")
        started = time.monotonic()
        try:
            code = run_down(cmd.timeout_s, "graceful", "distro")
        except Exception as exc:  # noqa: BLE001 - reported on the channel, never re-raised
            log(f"down failed: {type(exc).__name__}: {exc}")
            code = 1
        elapsed = time.monotonic() - started
        log(f"down caller={cmd.caller}: end exit={code} elapsed={elapsed:.1f}")
        channel.send(f"drained exit={code} elapsed={elapsed:.1f}")
        if channel.broken:
            return 0


def _log(text: str) -> None:
    try:
        sys.stderr.write(f"drain-listen: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def main() -> int:
    """Entry point of ``ossctl drain-listen`` (root only)."""
    if os.geteuid() != 0:
        _log("refused: runs as root only")
        return 2
    from ossbot.common.control_flags import lifecycle_name, read_lifecycle  # noqa: PLC0415
    from ossbot.common.paths import Paths  # noqa: PLC0415
    from ossbot.ctl.cli import run_down  # noqa: PLC0415

    # The channel keeps the original stdout; anything the down path prints goes to stderr.
    chan = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    control_dir = Paths.default().control_dir

    def down(timeout_s: int, kind: str, scope: str) -> int:
        code, _report = run_down(timeout_s, kind, scope)
        return code

    _log(f"listening (pid {os.getpid()})")
    return serve(sys.stdin.buffer, chan, down, lambda: lifecycle_name(read_lifecycle(control_dir)), _log)
