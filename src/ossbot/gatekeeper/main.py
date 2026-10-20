"""Gatekeeper entrypoint (``ossbot-gatekeeper``; systemd unit in FEAT-004).

Start order (fail closed): migrate DB -> load and validate the config snapshot (refuses to start
on invalid config, missing profile or empty forbidden_logins) -> reconcile control flags and
resume.log -> bind ``/run/ossbot/gatekeeper.sock`` (0660, group ossapi) -> serve.
"""

from __future__ import annotations

import grp
import logging
import os
import socket
import stat
import sys
from pathlib import Path

import uvicorn

from ossbot.common.config import ConfigError
from ossbot.common.logging import setup_logging
from ossbot.common.paths import Paths
from ossbot.common.redact import Redactor
from ossbot.gatekeeper.app import Service, create_app
from ossbot.gatekeeper.authz import PeerCredH11Protocol, resolve_role_map
from ossbot.gatekeeper.context import AppContext, build_context
from ossbot.gatekeeper.github_read import GitHubReader, make_client
from ossbot.gatekeeper.policy import PolicyStartupError
from ossbot.gatekeeper.reconcile import Reconciler, ReconcileService
from ossbot.gatekeeper.review_mirror import ReviewMirror
from ossbot.gatekeeper.submit import make_submitter
from ossbot.gatekeeper.telegram.bot import TelegramFrontend, build_application, telegram_enabled
from ossbot.gatekeeper.telegram.handlers import Handlers
from ossbot.gatekeeper.telegraph import Telegraph, TelegraphWriter
from ossbot.gatekeeper.telegraph import make_client as make_telegraph_client

log = logging.getLogger("ossbot.gatekeeper")
SOCKET_GROUP = "ossapi"


def bind_socket(path: Path, group: str | None = SOCKET_GROUP) -> socket.socket:
    """Create the API socket with mode 0660 from the start (no world-accessible window)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            path.unlink()
        else:
            raise RuntimeError(f"{path} exists and is not a socket")
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o117)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    if group is not None:
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except KeyError:
            log.warning("group %s does not exist; socket keeps the process group", group)
    os.chmod(path, 0o660)
    sock.listen(64)
    return sock


def build_services(ctx: AppContext) -> list[Service]:
    """Restart reconciliation (reads only) first, then the Telegram front-end if configured."""
    write_token = os.environ.get("GITHUB_WRITE_TOKEN") or None
    if write_token is None:
        log.warning("no GITHUB_WRITE_TOKEN: every submit is refused (error_class=no_write_token)")
    client = make_client(write_token)
    submitter = make_submitter(ctx, client, write_token)
    reconciler = Reconciler(ctx, GitHubReader(client))
    services: list[Service] = [ReconcileService(reconciler)]
    mirror_token = os.environ.get("GITHUB_REVIEW_MIRROR_TOKEN") or None  # optional (ADR-0019 C, off by default)
    ctx.review_mirror = ReviewMirror(ctx, make_client(mirror_token) if mirror_token else None, mirror_token,
                                     classic=client)  # fmt: skip
    ctx.approvals.mirror_url = ctx.review_mirror.card_url
    # §14.12.8, off by default: the writer exists always; nothing is offered unless telegraph.enabled
    ctx.telegraph = Telegraph(ctx, TelegraphWriter(make_telegraph_client(), os.environ.get("TELEGRAPH_ACCESS_TOKEN")))
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    if not telegram_enabled(ctx, bot_token):
        log.warning("Telegram front-end disabled (awaiting TELEGRAM_BOT_TOKEN / profile.telegram.user_id)")
        return services
    assert bot_token is not None
    handlers = Handlers(ctx, submitter.submit, submitter.preflight)
    handlers.reader = GitHubReader(client)
    frontend = TelegramFrontend(ctx, build_application(bot_token, handlers))
    frontend.reader = handlers.reader  # digest stats and status cards (read-only GETs, §14.13.4, §14.11.11)
    submitter.retry_note = frontend.publisher.retry_note
    reconciler.retry_note = frontend.publisher.retry_note
    services.append(frontend)
    return services


def main() -> int:
    redactor = Redactor()
    for name in ("TELEGRAM_BOT_TOKEN", "GITHUB_WRITE_TOKEN", "OSSBOT_TOKEN_PEPPER", "HERMES_API_KEY",
                 "GITHUB_REVIEW_MIRROR_TOKEN", "TELEGRAPH_ACCESS_TOKEN"):  # fmt: skip
        value = os.environ.get(name)
        if value:
            redactor.add_secret(value)
    setup_logging("gatekeeper", redactor)
    paths = Paths.default()
    ctx = build_context(paths, resolve_role_map())
    try:
        ctx.snapshots.load_at_start()
    except (ConfigError, PolicyStartupError) as exc:
        log.error("refusing to start: %s", exc, extra={"event": "config_invalid"})
        return 2
    report = ctx.control.reconcile()
    log.info("control state %s (alerts: %s)", report.state, list(report.alerts), extra={"event": "started"})
    sock = bind_socket(paths.gatekeeper_sock)
    config = uvicorn.Config(
        create_app(ctx, services=build_services(ctx)), http=PeerCredH11Protocol, lifespan="on", log_config=None,
        access_log=False, proxy_headers=False, server_header=False,
    )  # fmt: skip
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
