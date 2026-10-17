"""Secret redaction for logs and user-facing text (DESIGN §21).

The default patterns are the same regexes as ``policy.yaml`` ``secrets.extra_patterns``
(the Telegram token pattern is identical everywhere, N6).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

DEFAULT_SECRET_PATTERNS: tuple[str, ...] = (
    r"ksk_[A-Za-z0-9_\-]{16,}",
    r"gh[pousr]_[A-Za-z0-9]{36,}",
    r"github_pat_[A-Za-z0-9_]{60,}",
    r"\d{6,12}:[A-Za-z0-9_-]{30,}",
)
REDACTED = "[REDACTED]"


class Redactor:
    def __init__(self, patterns: Iterable[str] = DEFAULT_SECRET_PATTERNS, secrets: Iterable[str] = ()) -> None:
        self._patterns = [re.compile(p) for p in patterns]
        # Longest first so a secret containing another is fully replaced.
        self._secrets = sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True)

    def add_secret(self, value: str) -> None:
        if len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in self._patterns:
            text = pattern.sub(REDACTED, text)
        return text


def find_secret_patterns(text: str, patterns: Iterable[str] = DEFAULT_SECRET_PATTERNS) -> list[str]:
    """Return the patterns that match ``text`` (used by policy checks)."""
    return [p for p in patterns if re.search(p, text)]
