"""Deterministic policy checks (DESIGN §12.6, §7.9 floors, I10).

* ``FORBIDDEN_PATHS_FLOOR`` is hard-coded (not personal). Personal floors (forbidden logins,
  confidentiality strings) are monotonic: every value ever loaded is stored in ``policy_floor``;
  the effective sets are ``profile union floor``. Removal needs an explicit operator + Telegram path.
* A fresh DB whose effective ``forbidden_logins`` is empty refuses to start (fail closed).
* Every check returns a list of ``Violation``; an empty list means pass.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from ossbot.common.clock import Clock, to_iso
from ossbot.common.config import ConfigBundle, PolicyConfig, ProfileConfig, ResolvedTarget
from ossbot.common.hashing import sha256_text
from ossbot.common.redact import DEFAULT_SECRET_PATTERNS

FORBIDDEN_PATHS_FLOOR: tuple[str, ...] = (".github/workflows/**",)
DISCLOSURE_MARKER = "<!-- ossbot:cid="
DISCLOSURE_PHRASE = "AI assistance disclosure"
FloorKind = Literal["forbidden_login", "confidentiality"]
TextKind = Literal["title", "body", "comment", "commit_message"]
# Candidate strings for the Kiro key hash check: anything token-shaped.
TOKENISH_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")


class PolicyStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str = ""


@dataclass(frozen=True)
class DiffEntry:
    """One changed path: numstat (``added``/``deleted`` None for binary) and raw modes."""

    path: str
    added: int | None
    deleted: int | None
    old_mode: str = "100644"
    new_mode: str = "100644"


# ------------------------------------------------------------------------- floors
def sync_floor(conn: sqlite3.Connection, clock: Clock, profile: ProfileConfig) -> list[tuple[str, str]]:
    """Persist every profile floor value (monotonic). Caller holds the transaction."""
    added: list[tuple[str, str]] = []
    now = to_iso(clock.now())
    values: list[tuple[FloorKind, str]] = [("forbidden_login", v.lower()) for v in profile.forbidden_logins]
    values += [("confidentiality", v.lower()) for v in profile.confidentiality_floor]
    for kind, value in values:
        cur = conn.execute(
            "INSERT INTO policy_floor (kind, value, added_at) VALUES (?, ?, ?) ON CONFLICT(kind, value) DO NOTHING",
            (kind, value, now),
        )
        if cur.rowcount:
            added.append((kind, value))
    return added


def floor_values(conn: sqlite3.Connection, kind: FloorKind) -> set[str]:
    rows = conn.execute("SELECT value FROM policy_floor WHERE kind = ? AND removed_at IS NULL", (kind,))
    return {str(r["value"]) for r in rows}


def remove_floor(conn: sqlite3.Connection, clock: Clock, kind: FloorKind, value: str, by: str) -> bool:
    """Operator (TTY) + Telegram confirm path only; the caller enforces both."""
    cur = conn.execute(
        "UPDATE policy_floor SET removed_at = ?, removed_by = ? WHERE kind = ? AND value = ? AND removed_at IS NULL",
        (to_iso(clock.now()), by, kind, value.lower()),
    )
    return bool(cur.rowcount)


def effective_forbidden_logins(conn: sqlite3.Connection, profile: ProfileConfig) -> set[str]:
    return {v.lower() for v in profile.forbidden_logins} | floor_values(conn, "forbidden_login")


def effective_confidentiality(conn: sqlite3.Connection, bundle: ConfigBundle) -> set[str]:
    return (
        {v.lower() for v in bundle.profile.confidentiality_floor}
        | {v.lower() for v in bundle.policy.text.confidentiality_denylist}
        | floor_values(conn, "confidentiality")
    )


def check_startup(conn: sqlite3.Connection, profile: ProfileConfig) -> None:
    if not effective_forbidden_logins(conn, profile):
        raise PolicyStartupError("forbidden_logins is empty (profile and policy floor); refusing to start")


def effective_forbidden_paths(policy: PolicyConfig) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*FORBIDDEN_PATHS_FLOOR, *policy.diff.forbidden_paths]))


# --------------------------------------------------------------------- path globs
@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """gitignore-style: a pattern without '/' matches the basename at any depth; '**' spans dirs."""
    anchored = "/" in pattern.rstrip("/")
    pat = pattern.lstrip("/")
    out = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    body = "".join(out)
    return re.compile(f"^{body}$" if anchored else f"^(?:.*/)?{body}$")


def path_matches(path: str, patterns: Iterable[str]) -> str | None:
    norm = path.lstrip("/")
    for pattern in patterns:
        if _glob_regex(pattern).match(norm):
            return pattern
    return None


# ----------------------------------------------------------------------- checks
def check_diff(
    entries: Sequence[DiffEntry], policy: PolicyConfig, target: ResolvedTarget | None = None
) -> list[Violation]:
    out: list[Violation] = []
    rules = policy.diff
    if len(entries) > rules.max_files:
        out.append(Violation("too_many_files", f"{len(entries)} > {rules.max_files}"))
    max_lines = min(rules.max_changed_lines, target.max_changed_lines) if target else rules.max_changed_lines
    changed = sum((e.added or 0) + (e.deleted or 0) for e in entries)
    if changed > max_lines:
        out.append(Violation("too_many_lines", f"{changed} > {max_lines}"))
    forbidden = effective_forbidden_paths(policy)
    for e in entries:
        hit = path_matches(e.path, forbidden)
        if hit:
            out.append(Violation("forbidden_path", f"{e.path} ({hit})"))
        if rules.forbid_binary and (e.added is None or e.deleted is None):
            out.append(Violation("binary", e.path))
        modes = {e.old_mode, e.new_mode}
        if rules.forbid_symlinks and "120000" in modes:
            out.append(Violation("symlink", e.path))
        if rules.forbid_submodule_changes and "160000" in modes:
            out.append(Violation("submodule", e.path))
        for mode in sorted(modes & set(rules.forbid_file_modes) - {"120000", "160000"}):
            out.append(Violation("file_mode", f"{e.path} {mode}"))
    return out


def kiro_key_hashes(value: str | None) -> frozenset[str]:
    """`KIRO_KEY_SHA256` is a comma list (sandbox + hermes key, DESIGN §18.3)."""
    return frozenset(h.strip().lower() for h in (value or "").split(",") if h.strip())


def check_text(
    kind: TextKind,
    text: str,
    bundle: ConfigBundle,
    confidentiality: set[str],
    kiro_key_sha256: str | None = None,
) -> list[Violation]:
    out: list[Violation] = []
    rules = bundle.policy.text
    limits = {
        "title": rules.title_max,
        "body": rules.body_max,
        "comment": rules.comment_max,
        "commit_message": rules.commit_message_max,
    }
    size = len(text.encode("utf-8")) if kind == "commit_message" else len(text)
    if size > limits[kind]:
        out.append(Violation("too_long", f"{kind} {size} > {limits[kind]}"))
    if kind == "title" and (not text.strip() or "\n" in text):
        out.append(Violation("bad_title"))
    if kind == "commit_message":
        subject = text.split("\n", 1)[0]
        if len(subject) > rules.commit_subject_max:
            out.append(Violation("subject_too_long", f"{len(subject)} > {rules.commit_subject_max}"))
    patterns = tuple(dict.fromkeys([*DEFAULT_SECRET_PATTERNS, *bundle.policy.secrets.extra_patterns]))
    for pattern in patterns:
        if re.search(pattern, text):
            out.append(Violation("secret_pattern", pattern))
    if bundle.policy.secrets.kiro_key_hash_check and kiro_key_sha256:
        for candidate in TOKENISH_RE.findall(text):
            if sha256_text(candidate) in kiro_key_hashes(kiro_key_sha256):
                out.append(Violation("kiro_key", "token matches KIRO_KEY_SHA256"))
                break
    lowered = text.lower()
    for value in sorted(confidentiality):
        if value and value in lowered:
            out.append(Violation("confidential", value))
    if kind == "body" and rules.require_disclosure and (DISCLOSURE_MARKER not in text or DISCLOSURE_PHRASE not in text):
        out.append(Violation("missing_disclosure"))
    return out


@dataclass(frozen=True)
class RepoState:
    """Facts relevant to a repo-level decision (vetting result + DB overrides)."""

    ai_policy: Literal["allowed", "unclear", "banned", "unknown"] = "unknown"
    ai_override_allowed: bool = False
    cla_required: bool = False
    db_enabled: bool | None = None  # repo_overrides.enabled


def check_repo(
    repo: str, target: ResolvedTarget | None, state: RepoState, forbidden_logins: set[str]
) -> list[Violation]:
    out: list[Violation] = []
    owner = repo.split("/", 1)[0].lower()
    if owner in forbidden_logins:
        out.append(Violation("forbidden_owner", owner))
    if target is None:
        out.append(Violation("not_a_target", repo))
        return out
    enabled = target.enabled if state.db_enabled is None else state.db_enabled
    if not enabled:
        out.append(Violation("repo_disabled", repo))
    allowed_by_override = state.ai_override_allowed or target.ai_policy_override == "allowed"
    if state.ai_policy == "banned":
        out.append(Violation("ai_policy_banned", repo))
    elif state.ai_policy in {"unclear", "unknown"} and not allowed_by_override:
        out.append(Violation("ai_policy_unclear", repo))
    if state.cla_required and not target.cla_signed:
        out.append(Violation("cla_required", repo))
    return out
