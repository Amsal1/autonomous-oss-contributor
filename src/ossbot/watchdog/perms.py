"""Permission invariants of DESIGN §7.3, checked by the watchdog every 30 s and by ``ossctl doctor``.

Any drift (owner, group, mode, a symlink where a file belongs, an unexpected file) is reported as
one line; the watchdog kills on any. ``root`` relocates every path for tests.

Implementation notes (FEAT-004): the control dir is ``gatekeeper:osscontrol 2750`` (setgid, so a
flag the gatekeeper writes inherits group ``osscontrol`` and stays readable by runner and sandboxd);
``/run/ossbot`` is ``root:root 1755`` plus ACL entries for the two services that bind sockets there
(``stat`` then shows mode 1775: the ACL mask occupies the group bits).
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

SECRET_FILES: dict[str, str] = {
    "gatekeeper.env": "gatekeeper",
    "runner.env": "ossrunner",
    "sandboxd.env": "ossbox",
    "hermes.env": "hermes",
}
# socket -> (owner, group); mode is always 0660
SOCKETS: dict[str, tuple[str, str]] = {
    "gatekeeper.sock": ("gatekeeper", "ossapi"),
    "sandboxd.sock": ("ossbox", "osssbx"),
    "docker-filter.sock": ("root", "ossdock"),
    "anchor.sock": ("root", "gatekeeper"),
}
CONTROL_FILE_OWNERS = frozenset({"gatekeeper", "root"})


@dataclass(frozen=True)
class Expect:
    path: str
    owner: str
    group: str
    mode: int
    kind: str = "dir"  # dir | file | sock


def _name_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _name_gid(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _check(root: Path, e: Expect, required: bool = True) -> list[str]:
    path = root / e.path.lstrip("/")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return [f"{e.path}: missing"] if required else []
    kind_ok = {
        "dir": stat.S_ISDIR(st.st_mode),
        "file": stat.S_ISREG(st.st_mode),
        "sock": stat.S_ISSOCK(st.st_mode),
    }[e.kind]
    if not kind_ok:
        return [f"{e.path}: not a {e.kind} (symlink or other type)"]
    have = (_name_uid(st.st_uid), _name_gid(st.st_gid), stat.S_IMODE(st.st_mode))
    want = (e.owner, e.group, e.mode)
    if have != want:
        return [f"{e.path}: {have[0]}:{have[1]} {have[2]:04o}, expected {want[0]}:{want[1]} {want[2]:04o}"]
    return []


def check_control(root: Path = Path("/")) -> list[str]:
    problems = _check(root, Expect("/var/lib/ossbot/control", "gatekeeper", "osscontrol", 0o2750))
    control = root / "var/lib/ossbot/control"
    try:
        entries = sorted(os.listdir(control))
    except OSError:
        return problems
    for name in entries:
        if name.startswith(".") and ".tmp-" in name:
            continue  # an atomic write in progress
        path = control / name
        st = os.lstat(path)
        rel = f"/var/lib/ossbot/control/{name}"
        if not stat.S_ISREG(st.st_mode):
            problems.append(f"{rel}: not a regular file")
            continue
        owner, group, mode = _name_uid(st.st_uid), _name_gid(st.st_gid), stat.S_IMODE(st.st_mode)
        if owner not in CONTROL_FILE_OWNERS or group != "osscontrol" or mode != 0o640:
            problems.append(f"{rel}: {owner}:{group} {mode:04o}, expected gatekeeper|root:osscontrol 0640")
    return problems


def check_secrets(root: Path = Path("/")) -> list[str]:
    problems = _check(root, Expect("/etc/ossbot/secrets", "root", "root", 0o751))
    directory = root / "etc/ossbot/secrets"
    for name, group in SECRET_FILES.items():
        problems += _check(root, Expect(f"/etc/ossbot/secrets/{name}", "root", group, 0o640, "file"))
    try:
        extra = sorted(set(os.listdir(directory)) - set(SECRET_FILES))
    except OSError:
        extra = []
    for name in extra:
        st = os.lstat(directory / name)
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o007:
            problems.append(f"/etc/ossbot/secrets/{name}: unexpected file not root-only")
    return problems


def check_sockets(root: Path = Path("/")) -> list[str]:
    # 1755 + ACL user entries: stat shows the ACL mask (rwx) in the group bits, i.e. 1775.
    problems = _check(root, Expect("/run/ossbot", "root", "root", 0o1775))
    for name, (owner, group) in SOCKETS.items():
        problems += _check(root, Expect(f"/run/ossbot/{name}", owner, group, 0o660, "sock"), required=False)
    problems += _check(root, Expect("/var/run/docker.sock", "root", "docker", 0o660, "sock"), required=False)
    try:
        members = grp.getgrnam("docker").gr_mem
    except KeyError:
        members = []
    if members:
        problems.append(f"group docker has members: {','.join(members)}")
    return problems


def check_anchor_log(root: Path = Path("/")) -> list[str]:
    rel = "/srv/ossbot/logs/audit-anchors.log"
    problems = _check(root, Expect(rel, "root", "root", 0o644, "file"))
    if problems or root != Path("/"):
        return problems
    out = subprocess.run(["/usr/bin/lsattr", "-d", rel], capture_output=True, text=True, check=False)
    flags = out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else ""
    if "a" not in flags:
        problems.append(f"{rel}: not append-only (chattr +a)")
    return problems


def check_all(root: Path = Path("/")) -> list[str]:
    return check_control(root) + check_secrets(root) + check_sockets(root) + check_anchor_log(root)
