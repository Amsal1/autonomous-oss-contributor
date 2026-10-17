"""Stage-in tar building and output extraction (DESIGN §10.2 steps 1 and 4, M3).

The input tar is built by sandboxd itself from regular files only (opened ``O_NOFOLLOW``,
checked with ``fstat``), with flat names, mode 0444, owner root and a fixed mtime, so a hostile
earlier stage can never plant links or shadow files in ``/io/input``. Outputs come back as the
tar stream of ``GET /containers/{id}/archive?path=/io/output`` and are extracted with the same
paranoia: regular files and directories only, no absolute paths or ``..``, size caps.
"""

from __future__ import annotations

import io
import os
import re
import stat
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

NAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
MAX_INPUT_FILE = 64 * 1024 * 1024
MAX_INPUT_TOTAL = 128 * 1024 * 1024
MAX_OUTPUT_FILE = 64 * 1024 * 1024
MAX_OUTPUT_TOTAL = 256 * 1024 * 1024
MAX_OUTPUT_FILES = 5000


class StageError(ValueError):
    pass


def _check_root(path: Path, roots: Iterable[Path]) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise StageError(f"input path must be absolute without '..': {path}")
    if not any(path == r or r in path.parents for r in roots):
        raise StageError(f"input path outside the allowed roots: {path}")


def _read_regular(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise StageError(f"cannot open input {path.name}: {exc.strerror}") from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise StageError(f"input {path.name} is not a regular file")
    with os.fdopen(fd, "rb") as fh:
        if st.st_size > MAX_INPUT_FILE:
            raise StageError(f"input {path.name} too large")
        data = fh.read(MAX_INPUT_FILE + 1)
    if len(data) > MAX_INPUT_FILE:
        raise StageError(f"input {path.name} too large")
    return data


def build_stagein_tar(inputs: Mapping[str, Path], roots: Iterable[Path]) -> bytes:
    """A tar of regular files (0444, root:root, mtime 0) with flat, validated names."""
    roots = list(roots)
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(inputs):
            if NAME_RE.fullmatch(name) is None or name in {".", ".."}:
                raise StageError(f"bad input name {name!r}")
            path = inputs[name]
            _check_root(path, roots)
            data = _read_regular(path)
            total += len(data)
            if total > MAX_INPUT_TOTAL:
                raise StageError("inputs too large")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o444
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def extract_outputs(chunks: Iterable[bytes], dest: Path, top: str = "output") -> list[Path]:
    """Extract the ``/io/output`` archive into ``dest`` (created 0750); returns the files written.

    Docker names the top directory after the last path component (``output/...``).
    """
    data = b"".join(chunks)
    if len(data) > MAX_OUTPUT_TOTAL + 1024 * 1024:
        raise StageError("output archive too large")
    dest.mkdir(mode=0o750, parents=True, exist_ok=True)
    written: list[Path] = []
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")  # noqa: SIM115 - closed by the with below
    except tarfile.TarError as exc:
        raise StageError("output archive unreadable") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_OUTPUT_FILES:
            raise StageError("too many output files")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != top or PurePosixPath(member.name).is_absolute() or ".." in parts:
                raise StageError(f"unexpected archive member {member.name[:100]!r}")
            rel = parts[1:]
            if not rel:
                continue
            target = dest.joinpath(*rel)
            if member.isdir():
                target.mkdir(mode=0o750, parents=True, exist_ok=True)
                continue
            if not member.isreg():
                raise StageError(f"non-regular output member {member.name[:100]!r}")
            if member.size > MAX_OUTPUT_FILE:
                raise StageError(f"output file too large: {member.name[:100]!r}")
            total += member.size
            if total > MAX_OUTPUT_TOTAL:
                raise StageError("outputs too large")
            target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
            with os.fdopen(fd, "wb") as fh:
                fh.write(source.read())
            written.append(target)
    return written
