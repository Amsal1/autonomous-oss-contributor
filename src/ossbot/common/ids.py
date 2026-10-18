"""Identifiers and opaque tokens.

* Contribution / approval / action ids: prefix + 10 Crockford base32 chars (50 bits) derived
  from a ULID-style layout: 42 bits of Unix milliseconds (sortable until 2109) followed by 8
  random bits. Within one process the random part is bumped monotonically for the same
  millisecond. Collisions across processes are caught by the primary key; callers retry.
* UI tokens: 16 random bytes, base64url without padding (22 chars, DESIGN §12.7).
"""

from __future__ import annotations

import re
import secrets
import threading
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ID_RE = re.compile(r"^[a-z]_[0-9A-HJKMNP-TV-Z]{10}\Z")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}\Z")

_lock = threading.Lock()
_last: tuple[int, int] = (0, 0)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str, now_ms: int | None = None) -> str:
    """Return ``<prefix>_<10 chars>``; prefix is a single lowercase letter (``c``, ``a``, ``x``...)."""
    if not re.fullmatch(r"[a-z]", prefix):
        raise ValueError("prefix must be one lowercase letter")
    global _last  # noqa: PLW0603 - process-wide monotonic state, guarded by _lock
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    with _lock:
        last_ms, last_rand = _last
        if ms == last_ms:
            rand = (last_rand + 1) & 0xFF
        else:
            rand = secrets.randbelow(256)
        _last = (ms, rand)
    value = ((ms & ((1 << 42) - 1)) << 8) | rand
    return f"{prefix}_{_encode(value, 10)}"


def new_cid() -> str:
    return new_id("c")


def new_approval_id() -> str:
    return new_id("a")


def new_action_id() -> str:
    return new_id("x")


def new_token() -> str:
    token = secrets.token_urlsafe(16)
    assert len(token) == 22
    return token
