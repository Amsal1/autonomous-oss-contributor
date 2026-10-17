"""Kiro key output scanning (DESIGN §10.6).

sandboxd is the only distro process that knows the sandbox key's value. Every file collected
from ``/io/output`` and every container log is searched for the literal key and its encodings
(base64 standard and URL-safe in all three alignments, hex lower/upper, URL-encoded, reversed)
before anything is written to artifacts or logs.

Bundles: a package bundle is a *thin* pack whose prerequisite (the round base) sandboxd does not
have, so ``git bundle unbundle`` into an empty bare repo fails for every fix round. The scanner
therefore inflates every pack entry itself: whole objects are scanned in full, deltas by their
literal insert data (the only place new bytes can live in a delta). That covers every blob the
design's ``cat-file --batch-all-objects`` stream would show, without needing the base.
"""

from __future__ import annotations

import base64
import os
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import quote

REDACTED = b"[REDACTED]"
MIN_KEY_LEN = 8
CHUNK = 1 << 20
MAX_BUNDLE = 64 * 1024 * 1024
MAX_OBJECT = 256 * 1024 * 1024
OBJ_OFS_DELTA, OBJ_REF_DELTA = 6, 7


class BundleFormatError(ValueError):
    """Unparseable bundle: it never leaves the sandbox (the stage fails closed)."""


def _b64_fragments(key: bytes) -> list[bytes]:
    """Encoded substrings fully determined by the key bytes, for prefix offsets 0, 1 and 2."""
    out: list[bytes] = []
    n = len(key)
    for k in range(3):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(b"\0" * k + key).rstrip(b"=")
            # char j covers bits [6j, 6j + 6); keep the chars entirely inside the key's bits
            first = -(-8 * k // 6)
            last = (8 * (k + n)) // 6
            fragment = encoded[first:last]
            if len(fragment) >= MIN_KEY_LEN:
                out.append(fragment)
    return out


def needles(key: str) -> list[bytes]:
    raw = key.encode("utf-8")
    forms = {
        raw,
        raw[::-1],
        raw.hex().encode(),
        raw.hex().upper().encode(),
        quote(key, safe="").encode(),
        *_b64_fragments(raw),
    }
    return sorted((f for f in forms if len(f) >= MIN_KEY_LEN), key=len, reverse=True)


# ----------------------------------------------------------------------------- packs
def _varint_type_size(data: bytes, pos: int) -> tuple[int, int, int]:
    byte = data[pos]
    obj_type = (byte >> 4) & 7
    size = byte & 0x0F
    shift = 4
    pos += 1
    while byte & 0x80:
        byte = data[pos]
        size |= (byte & 0x7F) << shift
        shift += 7
        pos += 1
    return obj_type, size, pos


def _delta_inserts(delta: bytes) -> bytes:
    """Concatenated literal insert payloads of a git delta (copy instructions skipped)."""
    pos = 0
    for _ in range(2):  # source size, target size
        while delta[pos] & 0x80:
            pos += 1
        pos += 1
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            pos += bin(op & 0x7F).count("1")
        elif op:
            out += delta[pos : pos + op]
            pos += op
        else:
            raise BundleFormatError("delta opcode 0")
    return bytes(out)


def pack_payloads(pack: bytes) -> Iterator[bytes]:
    """Yield the inflated content of every entry (delta entries: their insert data)."""
    if len(pack) < 32 or pack[:4] != b"PACK" or int.from_bytes(pack[4:8], "big") not in (2, 3):
        raise BundleFormatError("not a v2/v3 pack")
    count = int.from_bytes(pack[8:12], "big")
    pos = 12
    try:
        for _ in range(count):
            obj_type, _size, pos = _varint_type_size(pack, pos)
            if obj_type == OBJ_OFS_DELTA:
                while pack[pos] & 0x80:
                    pos += 1
                pos += 1
            elif obj_type == OBJ_REF_DELTA:
                pos += 20
            elif obj_type not in (1, 2, 3, 4):
                raise BundleFormatError(f"object type {obj_type}")
            inflater = zlib.decompressobj()
            data = inflater.decompress(pack[pos:], MAX_OBJECT)
            if not inflater.eof:
                raise BundleFormatError("truncated or oversized object")
            pos = len(pack) - len(inflater.unused_data)
            yield _delta_inserts(data) if obj_type in (OBJ_OFS_DELTA, OBJ_REF_DELTA) else data
    except (IndexError, zlib.error) as exc:
        raise BundleFormatError("corrupt pack") from exc


def split_bundle(data: bytes) -> tuple[bytes, bytes]:
    """``(header, pack)`` of a v2/v3 bundle (header ends at the first empty line)."""
    if not (data.startswith(b"# v2 git bundle\n") or data.startswith(b"# v3 git bundle\n")):
        raise BundleFormatError("not a git bundle")
    end = data.find(b"\n\n")
    if end < 0:
        raise BundleFormatError("bundle header unterminated")
    return data[: end + 1], data[end + 2 :]


class KeyScanner:
    def __init__(self, keys: Iterable[str]) -> None:
        found: set[bytes] = set()
        for key in keys:
            if len(key) < MIN_KEY_LEN:
                raise ValueError("refusing to scan for a key shorter than 8 characters")
            found.update(needles(key))
        self._needles = sorted(found, key=len, reverse=True)
        self._overlap = max((len(n) for n in self._needles), default=1) - 1

    @property
    def enabled(self) -> bool:
        return bool(self._needles)

    def hit(self, data: bytes) -> bool:
        return any(n in data for n in self._needles)

    def redact(self, data: bytes) -> bytes:
        for needle in self._needles:
            data = data.replace(needle, REDACTED)
        return data

    def scan_stream(self, chunks: Iterable[bytes]) -> bool:
        tail = b""
        for chunk in chunks:
            window = tail + chunk
            if self.hit(window):
                return True
            tail = window[-self._overlap :] if self._overlap else b""
        return False

    def scan_file(self, path: Path) -> bool:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            return self.scan_stream(iter(lambda: fh.read(CHUNK), b""))

    def scan_bundle(self, bundle: Path) -> bool:
        """Raw bytes, the header (ref names) and every inflated pack entry."""
        fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_BUNDLE + 1)
        if len(data) > MAX_BUNDLE:
            raise BundleFormatError("bundle too large")
        if self.hit(data):
            return True
        header, pack = split_bundle(data)
        if self.hit(header):
            return True
        return any(self.hit(payload) for payload in pack_payloads(pack))
