"""Sanitizers for untrusted text (DESIGN §14.17.3, U2, U8) and the GitHub link parser (§14.20.6).

* ``defuse_commands`` (display only) and ``undefuse`` (text re-entering the gatekeeper: a quote copied
  into a comment, an echoed follow-up, a ``Use as comment`` source): every U+2060 that directly follows
  ``/`` is ours and is deleted before ``make_visible``; any other U+2060 is still shown.
* ``label_from_untrusted``: button labels from untrusted or model text are rejected when they hide
  characters, have whitespace collapsed, may not start with ``/`` and are dropped when too long.
* ``github_urls`` / ``parse_github_url``: the ``/work`` and link-card URL rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from ossbot.gatekeeper.telegram.escape import WORD_JOINER, defuse_commands, make_visible
from ossbot.gatekeeper.telegram.ui.limits import LABEL_SINGLE_MAX, label_len

__all__ = ["GithubRef", "defuse_commands", "domain_of", "github_urls", "label_from_untrusted", "parse_github_url",
           "undefuse"]  # fmt: skip

_OUR_MARKER = re.compile("/" + WORD_JOINER)
_WS = re.compile(r"\s+")
APPROVAL_WORDS = re.compile(r"\b(approve|approved|confirm|submit|publish|merge)\b", re.IGNORECASE)
GITHUB_URL_RE = re.compile(
    r"^(?:https://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})"
    r"(?:/(?P<kind>issues|pull)/(?P<n>[1-9][0-9]{0,9}))?/?\Z"
)
_URL_FINDER = re.compile(r"(?<![\w/])(?:https?://)?(?:www\.)?github\.com/[^\s<>()\"'`]+", re.IGNORECASE)
MAX_LINK_CARDS = 3


def undefuse(text: str) -> str:
    """Remove the U8 display marker (U+2060 right after ``/``) before the text re-enters the gatekeeper."""
    return _OUR_MARKER.sub("/", text)


def label_from_untrusted(text: str, limit: int = LABEL_SINGLE_MAX) -> str | None:
    """A button label from untrusted or model text, or None (dropped, never cut): hidden characters,
    a leading ``/``, an approval word or more than ``limit`` code points after collapsing whitespace."""
    _, hidden = make_visible(text)
    if hidden:
        return None
    label = _WS.sub(" ", text).strip()
    if not label or label.startswith("/") or APPROVAL_WORDS.search(label) or label_len(label) > limit:
        return None
    return label


def domain_of(url: str) -> str | None:
    """The host of an http(s) URL, else None (links are only ever http(s), §14.17.3)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.hostname.lower()


@dataclass(frozen=True)
class GithubRef:
    owner: str
    repo: str
    ref: Literal["issue", "pull", "repo"]
    number: int | None

    @property
    def upstream(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        if self.ref == "repo":
            return f"https://github.com/{self.upstream}"
        path = "issues" if self.ref == "issue" else "pull"
        return f"https://github.com/{self.upstream}/{path}/{self.number}"

    @property
    def label(self) -> str:
        return self.upstream if self.number is None else f"{self.upstream}#{self.number}"


def parse_github_url(text: str) -> GithubRef | None:
    """``/work`` URL rule (§14.20.6): ``?query`` and ``#fragment`` stripped, the scheme optional but never
    ``http://``, host ``github.com`` (``www.`` allowed), an issue, PR or repo path."""
    candidate = text.strip().split("#", 1)[0].split("?", 1)[0]
    if candidate.lower().startswith("http://"):
        return None
    match = GITHUB_URL_RE.match(candidate)
    if match is None or match.group("repo") in {".", ".."}:
        return None
    kind = match.group("kind")
    ref: Literal["issue", "pull", "repo"] = "repo" if kind is None else ("issue" if kind == "issues" else "pull")
    number = int(match.group("n")) if match.group("n") else None
    return GithubRef(match.group("owner"), match.group("repo"), ref, number)


def github_urls(text: str, extra: list[str] | None = None) -> list[GithubRef]:
    """GitHub issue, PR and repo links found anywhere in ``text`` plus ``extra`` (entity URLs), in
    order, de-duplicated, at most ``MAX_LINK_CARDS`` (routing rule 6). Trailing punctuation is dropped."""
    found: list[GithubRef] = []
    seen: set[str] = set()
    candidates = [m.group(0) for m in _URL_FINDER.finditer(text or "")] + list(extra or [])
    for raw in candidates:
        ref = parse_github_url(raw.rstrip(".,;:!?)]}>"))
        if ref is None or ref.url.lower() in seen:
            continue
        seen.add(ref.url.lower())
        found.append(ref)
        if len(found) == MAX_LINK_CARDS:
            break
    return found


def strip_urls(text: str) -> str:
    """The message text minus every GitHub link (link-card notes, §14.11.19)."""
    return _WS.sub(" ", _URL_FINDER.sub(" ", text or "")).strip()
