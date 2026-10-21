"""The gatekeeper's one secret scanner (DESIGN §14.9 U14, §14.18.1, BA-16, BA-21).

``SecretScanner`` holds the policy token patterns (``DEFAULT_SECRET_PATTERNS`` +
``policy.secrets.extra_patterns``), the gatekeeper's own secret values that are set (each >= 8
characters) and the Kiro-key hash check. It is built once per config snapshot from the snapshot and
``gatekeeper.env`` (``AppContext.secret_scanner``) and rebuilt on a confirmed ``/reload``.

* ``scan`` counts hits and never alters its input: publish bytes (PR title/body, commit message,
  comment, patch) are only scanned; packaging and the edit paths refuse them on a hit
  (``policy.check_text(..., secret_values=)`` -> ``Violation("gatekeeper_secret", kind)``).
* ``redact`` replaces every hit with ``common.redact.REDACTED`` for display text (U14).
* Load refusals keep the draft hold-back sound (``telegram/ui/redact.draft_visible``): a secret
  value with whitespace or of ``HOLD_BACK`` characters or more, a pattern whose minimum match is that
  wide, or an ``extra_patterns`` entry that can match whitespace. Multi-line secrets on publish bytes
  are gitleaks' job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ossbot.common.config import ConfigBundle
from ossbot.common.hashing import sha256_text
from ossbot.common.redact import DEFAULT_SECRET_PATTERNS, REDACTED
from ossbot.gatekeeper.signing import SIGNING_ENV, key_needles

HOLD_BACK = 256
MIN_VALUE_LEN = 8
WHITESPACE = " \t\n\r\f\v\u00a0\u2028\u2029"
# U14: the gatekeeper-held values (gatekeeper.env), loaded when set.
SECRET_ENV_KEYS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN", "GITHUB_WRITE_TOKEN", "HERMES_API_KEY", "OSSBOT_TOKEN_PEPPER",
    "GITHUB_REVIEW_MIRROR_TOKEN", "TELEGRAPH_ACCESS_TOKEN", "REVERSE_IMAGE_SEARCH_KEY",
)  # fmt: skip
TOKENISH_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")  # same candidates as policy.TOKENISH_RE


class SecretScanError(ValueError):
    """A load refusal: the scanner (and so the start or the /reload) is refused."""


# ------------------------------------------------------------------- pattern analysis
_parser: Any = getattr(re, "_parser")  # noqa: B009 - Python 3.12's sre parser (BA-16 walks its tree)
_constants: Any = getattr(re, "_constants")  # noqa: B009


def _items_match_whitespace(items: Iterable[Any]) -> bool:
    c = _constants
    for op, arg in items:
        if op in (c.ANY, c.NOT_LITERAL):
            return True
        if op == c.LITERAL and chr(arg) in WHITESPACE:
            return True
        if op == c.IN:
            members = list(arg)
            if members and members[0][0] == c.NEGATE:
                return True  # a negated class always includes some whitespace character
            if _class_matches_whitespace(members):
                return True
        elif op == c.SUBPATTERN:
            if _items_match_whitespace(arg[-1]):
                return True
        elif op in (c.MAX_REPEAT, c.MIN_REPEAT, getattr(c, "POSSESSIVE_REPEAT", object())):
            if _items_match_whitespace(arg[2]):
                return True
        elif op == c.BRANCH:
            if any(_items_match_whitespace(branch) for branch in arg[1]):
                return True
        elif op in (c.ASSERT, c.ASSERT_NOT):
            continue  # zero-width
        elif op == c.CATEGORY and arg in (c.CATEGORY_SPACE, c.CATEGORY_NOT_WORD, c.CATEGORY_NOT_DIGIT):
            return True
    return False


def _class_matches_whitespace(members: list[Any]) -> bool:
    c = _constants
    for op, arg in members:
        if op == c.LITERAL and chr(arg) in WHITESPACE:
            return True
        if op == c.RANGE and any(arg[0] <= ord(ch) <= arg[1] for ch in WHITESPACE):
            return True
        if op == c.CATEGORY and arg in (c.CATEGORY_SPACE, c.CATEGORY_NOT_WORD, c.CATEGORY_NOT_DIGIT):
            return True  # \s, \W, \D inside a class (\S, \w, \d never match whitespace)
    return False


def can_match_whitespace(pattern: str) -> bool:
    """True when ``pattern`` can match any character of ``WHITESPACE`` (BA-16, R5 finding 3)."""
    return _items_match_whitespace(_parser.parse(pattern))


def min_width(pattern: str) -> int:
    return int(_parser.parse(pattern).getwidth()[0])


# -------------------------------------------------------------------------- scanner
@dataclass(frozen=True)
class SecretScanner:
    values: frozenset[str] = frozenset()
    patterns: tuple[str, ...] = DEFAULT_SECRET_PATTERNS
    kiro_hashes: frozenset[str] = frozenset()
    _compiled: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", tuple(re.compile(p) for p in self.patterns))

    @staticmethod
    def _text(text: str | bytes) -> str:
        return text.decode("utf-8", "replace") if isinstance(text, bytes) else text

    def _kiro_hits(self, text: str) -> list[str]:
        if not self.kiro_hashes:
            return []
        return [t for t in TOKENISH_RE.findall(text) if sha256_text(t) in self.kiro_hashes]

    def value_hits(self, text: str | bytes) -> int:
        """Occurrences of the gatekeeper's own secret values (``gatekeeper_secret``)."""
        s = self._text(text)
        return sum(s.count(v) for v in self.values)

    def scan(self, text: str | bytes) -> int:
        """Number of hits (values, patterns, Kiro key); never alters ``text``."""
        s = self._text(text)
        hits = self.value_hits(s) + len(self._kiro_hits(s))
        return hits + sum(len(p.findall(s)) for p in self._compiled)

    def redact(self, text: str) -> tuple[str, int]:
        hits = 0
        for value in sorted(self.values, key=len, reverse=True):  # a value containing another first
            n = text.count(value)
            if n:
                hits += n
                text = text.replace(value, REDACTED)
        for candidate in self._kiro_hits(text):
            hits += text.count(candidate)
            text = text.replace(candidate, REDACTED)
        for pattern in self._compiled:
            text, n = pattern.subn(REDACTED, text)
            hits += n
        return text, hits


def build(bundle: ConfigBundle, env: Mapping[str, str]) -> SecretScanner:
    """The scanner for one snapshot; raises ``SecretScanError`` on any load refusal (BA-16)."""
    values = []
    for key in SECRET_ENV_KEYS:
        value = env.get(key) or ""
        if not value:
            continue
        if any(ch in WHITESPACE for ch in value) or len(value) >= HOLD_BACK:
            raise SecretScanError(f"{key}: a secret value must be free of whitespace and shorter than {HOLD_BACK}")
        if len(value) >= MIN_VALUE_LEN:
            values.append(value)
    # the SSH signing key is one long line: its 70-column lines are the values (``signing.key_needles``)
    values += key_needles((env.get(SIGNING_ENV) or "").strip())
    extra = list(bundle.policy.secrets.extra_patterns)
    for pattern in extra:
        if can_match_whitespace(pattern):
            raise SecretScanError(
                f"secrets.extra_patterns entry {pattern!r} can match whitespace; patterns must be whitespace-free"
                " (multi-line secrets on publish bytes are covered by gitleaks)"
            )
    patterns = tuple(dict.fromkeys([*DEFAULT_SECRET_PATTERNS, *extra]))
    for pattern in patterns:
        if min_width(pattern) >= HOLD_BACK:
            raise SecretScanError(f"secret pattern {pattern!r} has a minimum match of {HOLD_BACK}+ characters")
    hashes: frozenset[str] = frozenset()
    if bundle.policy.secrets.kiro_key_hash_check:
        raw = env.get("KIRO_KEY_SHA256") or ""
        hashes = frozenset(h.strip().lower() for h in raw.split(",") if h.strip())
    return SecretScanner(values=frozenset(values), patterns=patterns, kiro_hashes=hashes)
