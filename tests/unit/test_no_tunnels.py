"""No exposure in v1 (DESIGN FR-22, AC 42; ADR-0013): no Mini App, tunnel, webhook or inbound port.

Every tracked-style file outside ``docs/`` (code, scripts, units, Dockerfiles, compose, provisioning,
config) is scanned for tunnel tools and webhook configuration. The Telegram bot uses long polling
only; ``tests/system/test_no_exposure.sh`` checks the live listeners.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", "docs", ".ossbot-local", ".wsl", ".venv", "node_modules", "__pycache__", ".hypothesis",
             ".import_linter_cache", ".mypy_cache", ".ruff_cache", ".pytest_cache", "backups", "tools"}  # fmt: skip
SELF = Path(__file__).resolve()
# guards that name the forbidden tools on purpose (they check that none is installed or configured)
GUARDS = {SELF, (ROOT / "tests/system/test_no_exposure.sh").resolve()}
FORBIDDEN = {
    "cloudflared tunnel": re.compile(r"(?i)\bcloudflared\b|trycloudflare"),
    "ngrok": re.compile(r"(?i)\bngrok\b"),
    "tailscale funnel/serve": re.compile(r"(?i)\btailscale\b\s+(funnel|serve)\b|\btailscale\b"),
    "other tunnel": re.compile(r"(?i)\blocaltunnel\b|\bserveo\b|\bpagekite\b|\bbore\.pub\b|\bzrok\b"),
    "telegram webhook": re.compile(r"(?i)set_?webhook|webhook_url|WEBHOOK"),
    "web app / mini app": re.compile(r"(?i)\bweb_app\b|WebAppInfo|mini ?app"),
    "published docker port": re.compile(r"(?:\s|^)(?:-p|--publish)\s+\d|\bports:\s*$|\"PortBindings\"\s*:\s*\{\s*\""),
}


SKIP_SUFFIXES = {".png", ".svg", ".lock", ".db", ".pyc", ".bundle", ".tar", ".gz", ".vhdx"}


@functools.cache
def _files() -> tuple[Path, ...]:
    """One pruned walk per session: skipped directories (.wsl holds the distro disk, .git, caches) are
    never entered, which keeps the scan of the /mnt/d drvfs mount to a few seconds."""
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            path = Path(dirpath) / name
            # ROOT is resolved, so the joined path compares directly: a ``resolve()`` per file costs
            # an lstat per path component on drvfs and made this walk take minutes
            if path.suffix in SKIP_SUFFIXES or path in GUARDS:
                continue
            out.append(path)
    return tuple(out)


@functools.cache
def _texts() -> tuple[tuple[Path, str], ...]:
    out = []
    for path in _files():
        try:
            out.append((path, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return tuple(out)


def test_scan_covers_code_scripts_units_and_images() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _files()}
    for required in ("src/ossbot/gatekeeper/telegram/bot.py", "scripts/provision.sh", "systemd/ossbot-proxy.service",
                     "sandbox/images/proxy/Dockerfile", "config/egress.yaml"):  # fmt: skip
        assert required in names


@pytest.mark.parametrize("what", sorted(FORBIDDEN))
def test_no_tunnel_or_webhook(what: str) -> None:
    pattern = FORBIDDEN[what]
    hits = []
    for path, text in _texts():
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                # the dockerfilter rejects published ports: its deny corpus/rules may name them
                if what == "published docker port" and "dockerfilter" in path.as_posix():
                    continue
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{n}: {line.strip()[:120]}")
    assert hits == []


def test_bot_uses_long_polling_only() -> None:
    bot = (ROOT / "src/ossbot/gatekeeper/telegram/bot.py").read_text(encoding="utf-8")
    assert "start_polling(" in bot and "start_webhook" not in bot and "run_webhook" not in bot
