"""The public-floor lock (build-env design §4.7, carried criterion C-M1): one root-only ``flock`` that
serialises every writer and reader of the floor.

Every user of the floor runs as root and takes this lock: ``ossbot-pubip`` (exclusive, around its
read-modify-write of the kernel chain ``OSSBOT-PUBFLOOR`` and the floor files), ``ossbot-firewall --apply``
for every part that touches ``OSSBOT-FWD``/``OSSBOT-PUBFLOOR`` (exclusive), ``ossbot-firewall --check``
including the squid drift check (shared, 10 s timeout; busy = a non-zero exit, i.e. fail closed in every
``ExecStartPre``) and ``render squid``'s creation of the floor files (exclusive).

The lock is ``/etc/ossbot/proxy/floor/.lock`` in a directory only root can write (``root:root 0755``), never
in ``/run/ossbot`` (writable by the gatekeeper and sandboxd users, design review 3 M1). It is created by root
``0600 root:root`` and opened with ``O_NOFOLLOW`` relative to the directory's own descriptor. A lock file or
directory that isn't exactly that (another owner, a group or other bit, a symlink, a non-regular file, a hard
link, a writable or non-root directory) is refused with ERROR ``floor_lock_untrusted``: an untrusted file
could be held by a non-root process, which would wedge the root floor updater. Nothing here fixes the file;
the operator removes it (``ossctl doctor`` names it).

Stdlib-only (``ossbot.common`` depends on no component). The path and the expected owner are overridable
only by argument (tests), never by the environment.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_PATH = Path("/etc/ossbot/proxy/floor/.lock")
CHECK_TIMEOUT_S = 10.0  # --check and squid_drift (shared)
WRITE_TIMEOUT_S = 30.0  # pubip, --apply, render squid's floor files (exclusive)
_POLL_S = 0.05


class FloorLockError(OSError):
    """Base: the lock could not be taken (``code`` names why; the text starts with it for the watchdog)."""

    code = "floor_lock"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")
        self.detail = detail


class FloorLockUntrusted(FloorLockError):
    """The lock file or its directory isn't root-only (another owner, mode, symlink, file type, hard link)."""

    code = "floor_lock_untrusted"


class FloorLockBusy(FloorLockError):
    """Another holder kept the lock past the timeout."""

    code = "floor_lock_busy"


def _untrusted(path: Path, why: str) -> FloorLockUntrusted:
    err = FloorLockUntrusted(f"{path}: {why}")
    log.error("refusing the public-floor lock %s: %s", path, why, extra={"event": "floor_lock_untrusted"})
    return err


def _open_dir(directory: Path, owner: int, group: int) -> int:
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        # root's parent (/etc/ossbot/proxy) is root-only writable; squid (uid 13) and ossbox read the files
        os.mkdir(directory, 0o755)
        os.chmod(directory, 0o755)  # noqa: S103 - the directory is world-readable by design, root-only writable
        st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise _untrusted(directory, "not a real directory")
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(dfd)
        if st.st_uid != owner or st.st_gid != group:
            raise _untrusted(directory, f"directory owned by {st.st_uid}:{st.st_gid}, expected {owner}:{group}")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _untrusted(directory, f"directory mode {stat.S_IMODE(st.st_mode):04o} is group/other-writable")
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def _open_lock(path: Path, owner: int, group: int) -> int:
    dfd = _open_dir(path.parent, owner, group)
    try:
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _untrusted(path, "a symlink") from exc
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EISDIR, errno.ENXIO):
                raise _untrusted(path, f"cannot open it as root's lock file ({exc.strerror})") from exc
            raise
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        why = None
        if not stat.S_ISREG(st.st_mode):
            why = "not a regular file"
        elif st.st_uid != owner or st.st_gid != group:
            why = f"owned by {st.st_uid}:{st.st_gid}, expected {owner}:{group}"
        elif stat.S_IMODE(st.st_mode) != 0o600:
            why = f"mode {stat.S_IMODE(st.st_mode):04o}, expected 0600"
        elif st.st_nlink != 1:
            why = f"{st.st_nlink} hard links"
        if why is not None:
            raise _untrusted(path, why)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def floor_lock(
    exclusive: bool,
    timeout_s: float,
    path: Path = LOCK_PATH,
    *,
    owner: int = 0,
    group: int = 0,
) -> Iterator[None]:
    """Hold the public-floor lock (``exclusive`` or shared) for the ``with`` body.

    Raises ``FloorLockUntrusted`` (ERROR ``floor_lock_untrusted``) for a file or directory that isn't
    root-only, ``FloorLockBusy`` when another holder keeps it past ``timeout_s``. ``owner``/``group`` are the
    expected owner of the file and its directory: root, overridden only by tests on a tmp path."""
    fd = _open_lock(path, owner, group)
    try:
        op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fcntl.flock(fd, op)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise FloorLockBusy(f"{path} held by another process for more than {timeout_s:g} s") from None
                time.sleep(_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
