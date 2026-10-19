"""Render Hermes' managed config for a capability tier (DESIGN §15.1, §15.5; ADR-0002 addendum).

Root, ``python -I -m ossbot.ctl.hermes_render [--tier T | --from-flag] [--check]``. The output is
Hermes v0.22.2's managed scope ``/etc/hermes/config.yaml`` (root:root 0644, documented to win over
``~/.hermes/config.yaml`` and the environment), so nothing hermes-writable carries the tool surface.
The tier comes from the control flag (missing = policy default, unparseable = ``locked``) unless given.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import sys
from pathlib import Path

import jinja2

from ossbot.common.config import ConfigError, parse_bundle
from ossbot.common.control_flags import HERMES_TIERS, HermesTier, read_hermes_tier
from ossbot.common.snapshot import read_current
from ossbot.gatekeeper.chat.hermes_surface import allowed_toolsets, disabled_toolsets

APP = Path("/opt/ossbot/app")
SNAPSHOT_ROOT = Path("/srv/ossbot/config-snapshot")
TEMPLATE = APP / "hermes" / "config" / "config.yaml.j2"
MANAGED_DIR = Path("/etc/hermes")
MANAGED_CONFIG = MANAGED_DIR / "config.yaml"
CONTROL_DIR = Path("/var/lib/ossbot/control")


def render(tier: HermesTier, template: Path = TEMPLATE) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False, keep_trailing_newline=True)  # noqa: S701 - YAML, not HTML
    text = env.from_string(template.read_text(encoding="utf-8")).render(
        tier=tier, toolsets=list(allowed_toolsets(tier)), disabled=list(disabled_toolsets(tier))
    )
    return text


def write_managed(text: str, path: Path = MANAGED_CONFIG) -> bool:
    """Atomic replace, refusing a symlinked directory or target; True when the content changed."""
    parent = path.parent
    if parent.exists():
        st = os.lstat(parent)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"{parent} is not a real directory")
    else:
        parent.mkdir(mode=0o755, parents=True)
    with contextlib.suppress(FileNotFoundError):
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"{path} is a symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            if fh.read() == text.encode():
                return False
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


def policy_default_tier(snapshot_root: Path = SNAPSHOT_ROOT) -> HermesTier:
    """``hermes.capability_tier_default`` of the current config snapshot; unreadable = ``locked``."""
    try:
        _, files = read_current(snapshot_root)
        return parse_bundle(files).policy.hermes.capability_tier_default
    except (OSError, ValueError, ConfigError):
        return "locked"


def tier_from_flag(control_dir: Path = CONTROL_DIR, snapshot_root: Path = SNAPSHOT_ROOT) -> HermesTier:
    return read_hermes_tier(control_dir, policy_default_tier(snapshot_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot.ctl.hermes_render", description=__doc__.split("\n")[0])
    parser.add_argument("--tier", choices=HERMES_TIERS)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--out", type=Path, default=MANAGED_CONFIG)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    parser.add_argument("--check", action="store_true", help="exit 1 when the file differs from the render")
    args = parser.parse_args(argv)
    tier: HermesTier = args.tier or tier_from_flag()
    text = render(tier, args.template)
    if args.stdout:
        sys.stdout.write(text)
        return 0
    if args.check:
        try:
            same = args.out.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        print(f"{args.out}: {'matches' if same else 'differs from'} the render for tier {tier}")
        return 0 if same else 1
    changed = write_managed(text, args.out)
    print(f"{args.out}: {'changed' if changed else 'unchanged'} (tier {tier})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
