"""``ossbot-runner``: the pipeline worker service (DESIGN §7.5, §9; unit ``ossbot-runner.service``).

Runs as ``ossrunner``: worker role on the gatekeeper socket, sandboxd client, GitHub reads through
proxy 3140 with ``GITHUB_READ_TOKEN`` (``runner.env``). It holds no write token and no Kiro key, and
it never imports the gatekeeper. ``config/models.yaml`` is read live from the repo mount (the one
live config file, §16.1); everything else comes from the gatekeeper's config snapshot.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Any

from ossbot.common.config import ConfigBundle
from ossbot.common.logging import setup_logging
from ossbot.common.models import ModelsFile
from ossbot.common.paths import Paths
from ossbot.common.redact import Redactor
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.gk_client import Gatekeeper, GkError
from ossbot.runner.pipeline import Gate, StageContext
from ossbot.runner.sbx_client import Sandbox
from ossbot.runner.scheduler import Scheduler

log = logging.getLogger("ossbot.runner")
TICK_S = 10.0


class ConfigCache:
    """The gatekeeper's current snapshot; the last good one stays while the gatekeeper is away."""

    def __init__(self, gk: Gatekeeper) -> None:
        self.gk = gk
        self.lock = threading.Lock()
        self.bundle: ConfigBundle | None = None

    def refresh(self) -> None:
        try:
            _, bundle = self.gk.config()
        except (GkError, ValueError) as exc:
            log.warning("config snapshot unavailable: %s", exc, extra={"event": "config_unavailable"})
            return
        with self.lock:
            self.bundle = bundle

    def __call__(self) -> ConfigBundle:
        with self.lock:
            if self.bundle is None:
                raise RuntimeError("no config snapshot yet")
            return self.bundle


def build(paths: Paths, **overrides: Any) -> tuple[StageContext, ConfigCache]:
    gk: Gatekeeper = overrides.get("gk") or Gatekeeper(sock=paths.gatekeeper_sock)
    cache = ConfigCache(gk)
    runner_dir = paths.srv / "runner"

    def alert(reason: str) -> None:
        try:
            gk.notify(f"⚠️ config/models.yaml: {reason}")
        except GkError:
            log.warning("models.yaml alert not delivered", extra={"event": "notify_failed"})

    sc = StageContext(
        gk=gk,
        sbx=overrides.get("sbx") or Sandbox(),
        gh=overrides.get("gh") or GitHubRead(),
        models=overrides.get("models")
        or ModelsFile(
            paths.config_dir / "models.yaml", last_good_path=runner_dir / "models.last-good.yaml", on_broken=alert
        ),
        gate=Gate(paths.control_dir),
        inputs_root=runner_dir / "inputs",
        config=cache,
        state_dir=runner_dir / "state",
    )
    return sc, cache


def main() -> int:
    redactor = Redactor()
    token = os.environ.get("GITHUB_READ_TOKEN")
    if token:
        redactor.add_secret(token)  # never logged, even inside an exception text
    setup_logging("runner", redactor)
    paths = Paths.default()
    sc, cache = build(paths)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set() and not sc.gk.health():
        stop.wait(5.0)
    cache.refresh()
    scheduler = Scheduler(sc, workers=int(os.environ.get("OSSBOT_RUNNER_WORKERS", "4")))
    try:
        log.info("runner started; %d interrupted stage(s) re-queued", scheduler.recover_interrupted(),
                 extra={"event": "runner_started"})  # fmt: skip
    except GkError as exc:
        log.warning("interrupted-stage recovery failed: %s", exc.code, extra={"event": "recover_failed"})
    while not stop.is_set():
        cache.refresh()
        try:
            scheduler.tick()
        except (GkError, RuntimeError) as exc:
            log.warning("tick failed: %s", exc, extra={"event": "tick_failed"})
        stop.wait(TICK_S)
    scheduler.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
