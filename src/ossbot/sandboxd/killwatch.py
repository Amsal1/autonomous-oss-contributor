"""Kill watch (DESIGN §13): poll the control flags; on ``killed`` kill every running job
container immediately. New jobs are refused while paused or killed by ``JobService.admit``;
running jobs finish under ``paused``. The root enforcer (``ossbot-kill``) also kills every
``ossbot.sandbox=1`` container through the real socket; this is the fast in-process path."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ossbot.common.control_flags import read_flags
from ossbot.sandboxd.jobs import JobService

log = logging.getLogger(__name__)
POLL_S = 1.0


class KillWatch:
    def __init__(self, jobs: JobService, control_dir: Path, poll_s: float = POLL_S) -> None:
        self.jobs = jobs
        self.control_dir = control_dir
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        flags = read_flags(self.control_dir)
        if flags.killed is not None:
            return self.jobs.kill_all(f"killed: {flags.killed.reason}")
        return 0

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception:
                log.exception("kill watch tick failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="killwatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
