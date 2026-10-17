"""Config snapshot hash (N2) and on-disk snapshot layout (DESIGN §7.9).

hash = sha256(b"".join(name + b"\\0" + str(len(data)) + b"\\0" + data for name, data in sorted(files)))

Snapshots live in ``<root>/<sha256>/`` (dir 0755, files 0444); ``<root>/current`` is a symlink
repointed atomically. Readers (runner, sandboxd, root renderers) use ``read_current`` only.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from ossbot.common.hashing import is_sha256_hex


def snapshot_hash(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def write_snapshot(root: Path, files: Mapping[str, bytes]) -> str:
    """Write the exact bytes to ``root/<hash>/`` and repoint ``root/current``. Returns the hash."""
    digest = snapshot_hash(files)
    target = root / digest
    if not target.is_dir():
        staging = root / f".{digest}.tmp-{os.getpid()}"
        staging.mkdir(mode=0o755, parents=True)
        for name, data in files.items():
            if "/" in name or name.startswith("."):
                raise ValueError(f"bad snapshot file name {name!r}")
            path = staging / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            path.chmod(0o444)
        staging.chmod(0o755)
        os.replace(staging, target)
    link_tmp = root / f".current.tmp-{os.getpid()}"
    if link_tmp.is_symlink() or link_tmp.exists():
        link_tmp.unlink()
    os.symlink(digest, link_tmp)
    os.replace(link_tmp, root / "current")
    return digest


def read_snapshot(root: Path, digest: str) -> dict[str, bytes]:
    if not is_sha256_hex(digest):
        raise ValueError("bad snapshot hash")
    directory = root / digest
    files = {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file() and not p.is_symlink()}
    if snapshot_hash(files) != digest:
        raise ValueError(f"snapshot {digest} does not match its content")
    return files


def read_current(root: Path) -> tuple[str, dict[str, bytes]]:
    digest = os.readlink(root / "current")
    return digest, read_snapshot(root, digest)
