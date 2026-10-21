"""Review-artifact derivation from the per-rev mirror (DESIGN §12.5 steps 5-6, M7, R4-N6, I6).

The gatekeeper derives numstat (``gitcmd.NUMSTAT``), raw modes, the patch (``gitcmd.FORMAT_PATCH``),
the commit message and identities itself; only these are ever shown or bound. The sandbox's
``change.patch``/``diffstat.json`` are compared by ``git patch-id --stable`` and the numstat set,
so a git-version difference in non-content bytes is not an anomaly.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ossbot.common.gitcmd import FORMAT_PATCH_ARGV, NUMSTAT_ARGV, NumstatEntry, parse_numstat
from ossbot.common.hashing import sha256_hex
from ossbot.common.schemas import OutboundKind
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError, ArtifactHashes
from ossbot.gatekeeper.gitops import DIFF_SAFETY, GitOps
from ossbot.gatekeeper.policy import DiffEntry

Comparison = Literal["identical", "equivalent", "mismatch", "unreadable"]
SIGNOFF_KEY = "signed-off-by"


@dataclass(frozen=True)
class Identity:
    author: str
    committer: str
    signoff: str | None


@dataclass(frozen=True)
class Derived:
    round_base: str
    head: str
    numstat_text: str
    numstat: tuple[NumstatEntry, ...]
    entries: tuple[DiffEntry, ...]
    patch: bytes
    commit_message: bytes
    identity: Identity

    @property
    def patch_sha256(self) -> str:
        return sha256_hex(self.patch)

    @property
    def commit_message_sha256(self) -> str:
        return sha256_hex(self.commit_message)

    def diffstat(self) -> list[dict[str, int | str | None]]:
        return [{"added": e.added, "deleted": e.deleted, "path": e.path} for e in self.numstat]


def _split_z(data: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in data.split(b"\0")]


def diff_entries(ops: GitOps, mirror: Path, base: str, head: str) -> list[DiffEntry]:
    """Raw modes + numstat per path (``-z``, unquoted paths) for the policy checks."""
    flags = ["-c", "diff.renames=false", "-c", "core.quotePath=true"]
    raw = ops.git.run([*flags, "diff", "--raw", "--no-abbrev", "-z", *DIFF_SAFETY, base, head], cwd=mirror).stdout
    num = ops.git.run([*flags, "diff", "--numstat", "-z", *DIFF_SAFETY, base, head], cwd=mirror).stdout
    modes: dict[str, tuple[str, str]] = {}
    items = _split_z(raw)
    index = 0
    while index + 1 < len(items):
        meta, path = items[index], items[index + 1]
        index += 2
        if not meta.startswith(":"):
            raise ValueError("unexpected raw diff output")
        fields = meta[1:].split(" ")
        modes[path] = (fields[0], fields[1])
    counts: dict[str, tuple[int | None, int | None]] = {}
    for item in _split_z(num):
        if not item:
            continue
        added, deleted, path = item.split("\t", 2)
        counts[path] = (None if added == "-" else int(added), None if deleted == "-" else int(deleted))
    out: list[DiffEntry] = []
    for path in sorted(set(modes) | set(counts)):
        old_mode, new_mode = modes.get(path, ("100644", "100644"))
        added_n, deleted_n = counts.get(path, (0, 0))
        out.append(DiffEntry(path, added_n, deleted_n, old_mode, new_mode))
    return out


def last_signoff(ops: GitOps, mirror: Path, message: bytes) -> str | None:
    out = ops.git.run(["interpret-trailers", "--parse"], cwd=mirror, stdin=message).stdout.decode("utf-8", "replace")
    value = None
    for line in out.splitlines():
        key, sep, rest = line.partition(":")
        if sep and key.strip().lower() == SIGNOFF_KEY:
            value = rest.strip()
    return value


def derive(ops: GitOps, mirror: Path, base: str, head: str) -> Derived:
    numstat_text = ops.git.run([*NUMSTAT_ARGV, base, head], cwd=mirror).stdout.decode("utf-8", "surrogateescape")
    patch = ops.git.run([*FORMAT_PATCH_ARGV, f"{base}..{head}"], cwd=mirror).stdout
    message = ops.git.run(["log", "-1", "--format=%B", head], cwd=mirror).stdout
    who = ops.git.out(["log", "-1", "--format=%an <%ae>%x00%cn <%ce>", head], cwd=mirror)
    author, _, committer = who.partition("\0")
    return Derived(
        round_base=base,
        head=head,
        numstat_text=numstat_text,
        numstat=tuple(parse_numstat(numstat_text)),
        entries=tuple(diff_entries(ops, mirror, base, head)),
        patch=patch,
        commit_message=message,
        identity=Identity(author, committer, last_signoff(ops, mirror, message)),
    )


def store(
    ops: GitOps, cid: str, rev: int, derived: Derived, cumulative: str | None = None,
    signature: dict[str, str] | None = None,
) -> Path:  # fmt: skip
    """``derived/<cid>/<rev>/`` (gatekeeper-only); display reads these, the binding re-derives.
    ``signature`` (``signing.Signature.as_dict``) is null for an unsigned head."""
    out = ops.derived_dir(cid, rev)
    out.mkdir(parents=True, exist_ok=True)
    (out / "change.patch").write_bytes(derived.patch)
    (out / "commit_message.txt").write_bytes(derived.commit_message)
    (out / "numstat.txt").write_text(derived.numstat_text, encoding="utf-8", errors="surrogateescape")
    meta = {
        "round_base": derived.round_base,
        "head": derived.head,
        "author": derived.identity.author,
        "committer": derived.identity.committer,
        "signoff": derived.identity.signoff,
        "cumulative_numstat": cumulative,
        "signature": signature,
    }
    (out / "meta.json").write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    return out


# --------------------------------------------------------------------- comparison
def patch_ids(ops: GitOps, patch: bytes) -> set[str]:
    """``git patch-id --stable`` from stdin only, outside any repository."""
    ops.git.home.mkdir(parents=True, exist_ok=True)
    out = ops.git.run(["patch-id", "--stable"], cwd=ops.git.home, stdin=patch).stdout.decode("ascii", "replace")
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def parse_diffstat_json(data: bytes) -> set[NumstatEntry]:
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, list):
        raise ValueError("diffstat.json must be a list")
    out: set[NumstatEntry] = set()
    for item in doc:
        if not isinstance(item, dict) or set(item) != {"added", "deleted", "path"}:
            raise ValueError("bad diffstat entry")
        added, deleted, path = item["added"], item["deleted"], item["path"]
        for count in (added, deleted):
            if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
                raise ValueError("bad diffstat count")
        if not isinstance(path, str):
            raise ValueError("bad diffstat path")
        out.add(NumstatEntry(added, deleted, path))
    return out


def compare(ops: GitOps, derived: Derived, sandbox_patch: bytes | None, sandbox_diffstat: bytes | None) -> Comparison:
    if sandbox_patch is None or sandbox_diffstat is None:
        return "unreadable"
    try:
        theirs_stat = parse_diffstat_json(sandbox_diffstat)
    except (ValueError, UnicodeDecodeError):
        return "unreadable"
    theirs_ids = patch_ids(ops, sandbox_patch)
    if not theirs_ids:
        return "unreadable"
    if theirs_ids != patch_ids(ops, derived.patch) or theirs_stat != set(derived.numstat):
        return "mismatch"
    return "identical" if sandbox_patch == derived.patch else "equivalent"


# --------------------------------------------------------------- ArtifactSource
class GitArtifacts:
    """``approvals.ArtifactSource``: re-hash the private bundle copy and re-derive patch and commit
    message from the per-rev mirror at Approve, Confirm and before the first write (§12.4)."""

    def __init__(self, ops: GitOps, conn: sqlite3.Connection) -> None:
        self.ops = ops
        self.conn = conn

    def hashes(self, cid: str, rev: int, kind: OutboundKind) -> ArtifactHashes:
        contrib = db.one(self.conn, "SELECT upstream, base_sha FROM contributions WHERE id = ?", (cid,))
        if contrib is None:
            raise ApprovalError("unknown_contribution", cid)
        rows = self.conn.execute(
            "SELECT payload_json FROM outbound_actions WHERE cid = ? AND kind = ? ORDER BY created_at DESC", (cid, kind)
        ).fetchall()
        payload = next((p for p in (json.loads(r["payload_json"]) for r in rows) if p.get("rev") == rev), None)
        if payload is None:
            raise ApprovalError("artifacts_unavailable", f"{cid} r{rev}: no action")
        base = payload["old_head_sha"] if kind == "push_update" else contrib["base_sha"]
        mirror = self.ops.mirror_dir(contrib["upstream"], cid, rev)
        bundle = self.ops.work_bundle(cid, rev)
        if not mirror.is_dir() or not bundle.is_file():
            raise ApprovalError("artifacts_unavailable", f"{cid} r{rev}: mirror or bundle missing")
        head = self.ops.rev_parse(mirror, f"refs/ossbot/r{rev}")
        if head is None or head != payload["head_sha"]:
            raise ApprovalError("artifacts_unavailable", f"{cid} r{rev}: head changed")
        patch = self.ops.git.run([*FORMAT_PATCH_ARGV, f"{base}..{head}"], cwd=mirror).stdout
        message = self.ops.git.run(["log", "-1", "--format=%B", head], cwd=mirror).stdout
        return ArtifactHashes(
            bundle_sha256=sha256_hex(bundle.read_bytes()),
            patch_sha256=sha256_hex(patch),
            commit_message_sha256=sha256_hex(message),
        )
