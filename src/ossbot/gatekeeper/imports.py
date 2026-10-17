"""Operator import (DESIGN §9.13, M3): bundle bytes in the request, never a path.

Validation happens before any row exists; every failure is 422 ``import_invalid:<rule>`` and
creates nothing. The validated bytes are stored gatekeeper-only under ``imports/<cid>/`` and the
contribution starts in ``IMPORTED``. The runner later downloads them (``import-bundle``) for the
``unbundle`` job (mode ``import``).
"""

from __future__ import annotations

import base64
import binascii
import re
import shutil
import tempfile
from pathlib import Path

from ossbot.common.clock import to_iso
from ossbot.common.config import resolve_target
from ossbot.common.hashing import sha256_hex
from ossbot.common.ids import new_cid
from ossbot.common.schemas import ImportRequest, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import normalize_text
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.contributions import ContributionRefused
from ossbot.gatekeeper.gitops import MAX_BUNDLE_BYTES, BundleCopy, GitError, GitOps, bundle_prerequisites

IMPORT_HEAD_RE = re.compile(r"^refs/heads/([A-Za-z0-9._/-]{1,100})$")
DOWNLOAD_STATES = (State.IMPORTED.value, State.FETCHING.value, State.FETCHED.value)


class ImportInvalid(RuntimeError):
    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        super().__init__(f"import_invalid:{rule}{': ' + detail if detail else ''}")


class Importer:
    def __init__(self, ctx: AppContext, ops: GitOps) -> None:
        self.ctx = ctx
        self.ops = ops

    def _validate(self, req: ImportRequest, data: bytes, work: Path) -> str:
        """Returns the import head ref. ``work`` is a private temp dir, deleted by the caller."""
        bundle = self.ops.store_bundle_bytes(work / "import.bundle", data)
        try:
            prereqs = bundle_prerequisites(data)
        except ValueError as exc:
            raise ImportInvalid("not_a_bundle") from exc
        tip = self._fetch_upstream(req)
        mirror = work / "mirror.git"
        self.ops.init_bare(mirror)
        (mirror / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (mirror / "objects" / "info" / "alternates").write_text(
            f"{self.ops.cache_dir(req.repo) / 'objects'}\n", encoding="utf-8"
        )
        self.ops.git.run(["update-ref", f"refs/upstream/{req.base_ref}", tip], cwd=mirror)
        try:
            heads = self.ops.list_heads(mirror, bundle.path)
        except (GitError, ValueError) as exc:
            raise ImportInvalid("heads", str(exc)) from exc
        if len(heads) != 1 or IMPORT_HEAD_RE.match(heads[0][1]) is None:
            raise ImportInvalid("heads", f"{len(heads)} head(s)")
        head_sha, head_ref = heads[0]
        if prereqs != {req.base_sha}:
            raise ImportInvalid("prerequisites")
        if not self.ops.commit_exists(mirror, req.base_sha) or not self.ops.is_ancestor(
            mirror, req.base_sha, f"refs/upstream/{req.base_ref}"
        ):
            raise ImportInvalid("base_not_upstream")
        if not self.ops.verify_bundle(mirror, bundle.path):
            raise ImportInvalid("verify")
        try:
            self.ops.fetch_bundle(mirror, bundle.path, head_ref, "refs/ossbot/import")
        except GitError as exc:
            raise ImportInvalid("fetch", exc.stderr[-300:]) from exc
        if self.ops.rev_parse(mirror, "refs/ossbot/import") != head_sha:
            raise ImportInvalid("heads", "fetched head differs")
        if not self.ops.is_ancestor(mirror, req.base_sha, head_sha) or head_sha == req.base_sha:
            raise ImportInvalid("head_not_descendant")
        return head_ref

    def _fetch_upstream(self, req: ImportRequest) -> str:
        try:
            return self.ops.fetch_upstream(req.repo, req.base_ref)
        except (GitError, ValueError) as exc:
            raise ImportInvalid("upstream_fetch", str(exc)) from exc

    def import_bundle(self, req: ImportRequest, actor: str) -> str:
        ctx = self.ctx
        target = resolve_target(ctx.config.targets, req.repo)
        if target is None:
            raise ImportInvalid("not_a_target")
        if not target.enabled:
            raise ImportInvalid("repo_not_enabled")
        if len(req.bundle_b64) > (MAX_BUNDLE_BYTES * 4) // 3 + 8:
            raise ImportInvalid("size")
        try:
            data = base64.b64decode(req.bundle_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImportInvalid("base64") from exc
        if len(data) > MAX_BUNDLE_BYTES:
            raise ImportInvalid("size")
        digest = sha256_hex(data)
        if digest != req.bundle_sha256:
            raise ImportInvalid("sha256")
        ctx.control.require_running("import")
        if req.issue_number is not None:
            decision = ctx.contributions.decide(target.repo, req.issue_number)
            if not decision.allowed:
                raise ContributionRefused(409, decision.http_code)
        work_root = self.ops.root / "work"
        work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="import-", dir=work_root))
        try:
            head_ref = self._validate(req, data, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        cid = new_cid()
        stored: BundleCopy = self.ops.store_bundle_bytes(self.ops.import_dir(cid) / "change.bundle", data)
        if stored.sha256 != digest:
            raise ImportInvalid("sha256", "stored copy differs")
        now = to_iso(ctx.clock.now())
        issue_url = f"https://github.com/{target.repo}/issues/{req.issue_number}" if req.issue_number else None
        try:
            with db.transaction(ctx.conn):
                ctx.conn.execute(
                    "INSERT INTO contributions (id, upstream, issue_number, issue_url, source, category, priority,"
                    " state, base_ref, base_sha, import_head_ref, import_bundle_sha256, pr_title, pr_body,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid, target.repo, req.issue_number, issue_url, "operator", target.category, target.priority,
                        State.IMPORTED.value, req.base_ref, req.base_sha, head_ref, digest,
                        normalize_text(req.title, title=True), normalize_text(req.body), now, now,
                    ),
                )  # fmt: skip
                ctx.audit.append(
                    actor,
                    "contribution_imported",
                    {"repo": target.repo, "base_sha": req.base_sha, "head_ref": head_ref, "bundle_sha256": digest},
                    cid,
                )
        except Exception:
            shutil.rmtree(self.ops.import_dir(cid), ignore_errors=True)
            raise
        return cid

    def download(self, cid: str) -> bytes:
        row = db.one(
            self.ctx.conn, "SELECT source, state, import_bundle_sha256 FROM contributions WHERE id = ?", (cid,)
        )
        if row is None:
            raise ContributionRefused(404, "not_found")
        if row["source"] != "operator" or row["state"] not in DOWNLOAD_STATES:
            raise ContributionRefused(409, "import_bundle_unavailable")
        data = (self.ops.import_dir(cid) / "change.bundle").read_bytes()
        if sha256_hex(data) != row["import_bundle_sha256"]:
            raise ContributionRefused(409, "import_bundle_corrupt")
        return data
