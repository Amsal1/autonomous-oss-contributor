"""The opt-in private GitHub review mirror (ADR-0019 option C, DESIGN §14.11.14; U4, U5, U9; AC 24, 25).

Off by default. It is active only while all of these hold: ``telegram.yaml review_mirror.enabled: true``
(a human-confirmed ``/reload``), ``profile.yaml review_mirror.repo`` set, ``GITHUB_REVIEW_MIRROR_TOKEN``
present, and a passing ``ossctl review-mirror enable`` probe recorded in the gatekeeper's state file
(``review-mirror.json``) for that same repo. A failed repo check (public, or Actions enabled) turns it
off again with an alert (P0 when the repo is public).

What it does, for each pending ``open_pr``/``push_update`` card while the system is running:

* builds two display commits in the per-rev bare mirror with plumbing only and a temporary index:
  ``B'`` (a root commit with the base versions of exactly the touched paths) and ``H'`` (the head
  versions plus ``.ossbot-review/{PR_BODY.md,COMMIT_MSG.txt,REVIEW.md}``, parent ``B'``), fixed identity
  ``ossbot review mirror <noreply@invalid>`` and the package time;
* checks integrity: ``diff --raw B' H'`` outside ``.ossbot-review/`` must equal ``diff --raw base head``
  (paths, modes, blob ids); a mismatch or a candidate touching ``.ossbot-review/`` is never pushed;
* force-pushes ``review/<cid>`` and ``review-base/<cid>`` and opens or updates the mirror PR through
  ``review_mirror_writer`` (the only module that writes, bound to the mirror repo);
* adds card row 4 ``[🐙 Review on GitHub ↗]`` with ``editMessageReplyMarkup`` re-using the card's own
  tokens (nothing minted or rotated; an armed card gets it when it next renders S1);
* polls the mirror PR (``poll_pending_s`` while the card is pending, ``poll_other_s`` otherwise): a
  submitted review by the owner becomes one change request, conversation comments join the draft
  review (source ``mirror``), a commit made on the mirror branch becomes a change request with its
  compare patch as guidance, and an Approve review never approves anything;
* closes the PR and deletes both branches once the card's approval has ended.

Display artifacts only (U4): nothing here is bound. Mirror text never contains ``@``, ``#N`` or a bare
URL (no cross-references or notifications). Every GitHub-authored string is untrusted.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.github_read import GitHubError, GitHubReader
from ossbot.gatekeeper.gitops import SHA_RE, Git, GitError
from ossbot.gatekeeper.review_mirror_writer import MirrorWriter, MirrorWriteRefused

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
STATE_FILE = "review-mirror.json"
REVIEW_DIR = ".ossbot-review"
IDENTITY = ("ossbot review mirror", "noreply@invalid")
ZERO_SHA = "0" * 40
REPO_CHECK_S = 600
EXPIRY_WARN = timedelta(days=14)
COMMENT_MAX = 4000
GUIDANCE_MAX = 20_000
_URL = re.compile(r"https?://[^\s`<>]+")
_ISSUE_REF = re.compile(r"(?<![\w`])#(\d+)")


class MirrorRefused(RuntimeError):
    """This candidate can't be mirrored (touches .ossbot-review/, integrity mismatch, no artifacts)."""


# ------------------------------------------------------------------------------- state
def _state_path(ctx: AppContext) -> Path:
    return ctx.gitops.root / STATE_FILE


def read_state(ctx: AppContext) -> dict[str, Any]:
    try:
        doc = json.loads(_state_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state(ctx: AppContext, doc: dict[str, Any]) -> None:
    path = _state_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def configured_repo(ctx: AppContext) -> str | None:
    return ctx.config.profile.review_mirror.repo


def active_repo(ctx: AppContext) -> str | None:
    """The mirror repo while the mirror is on (config + confirmed /reload + passing enable probe)."""
    repo = configured_repo(ctx)
    if repo is None or not ctx.config.telegram.review_mirror.enabled:
        return None
    state = read_state(ctx)
    return repo if state.get("enabled") is True and str(state.get("repo", "")).lower() == repo.lower() else None


# ------------------------------------------------------------------------------- text
def defang(text: str) -> str:
    """No ``@``, ``#N`` or bare URL in mirror text: URLs go into code spans, mentions and refs are broken."""
    out = _URL.sub(lambda m: f"`{m.group(0)}`", text.replace("`", "'"))
    out = out.replace("@", "(at)")
    return _ISSUE_REF.sub(lambda m: f"no. {m.group(1)}", out)


def review_files(contrib: Any, rev: int) -> dict[str, bytes]:
    title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else contrib["pr_title"]
    body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else contrib["pr_body"]
    review = (f"# {defang(str(contrib['id']))} r{rev}\n\nUpstream: `{contrib['upstream']}`\n"
              f"Issue: no. {contrib['issue_number'] or '-'}\nTitle: {defang(str(title or ''))}\n\n"
              "Approve only in Telegram. Reviews and comments here become change requests there.\n")  # fmt: skip
    return {"PR_BODY.md": defang(str(body or "")).encode("utf-8"),
            "COMMIT_MSG.txt": defang(str(contrib["commit_message"] or "")).encode("utf-8"),
            "REVIEW.md": review.encode("utf-8")}  # fmt: skip


# ------------------------------------------------------------------------------- plumbing
@dataclass(frozen=True)
class RawEntry:
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    status: str
    path: str


def raw_diff(git: Git, repo: Path, a: str, b: str) -> list[RawEntry]:
    """``diff --raw -z --no-renames --no-abbrev a b`` (paths, modes, blob ids)."""
    out = git.run(["diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff", "--no-textconv", a, b],
                  cwd=repo).stdout.decode("utf-8", "surrogateescape")  # fmt: skip
    fields = out.split("\0")
    entries: list[RawEntry] = []
    i = 0
    while i + 1 < len(fields) and fields[i].startswith(":"):
        old_mode, new_mode, old_blob, new_blob, status = fields[i][1:].split(" ")
        entries.append(RawEntry(old_mode, new_mode, old_blob, new_blob, status, fields[i + 1]))
        i += 2
    return entries


@dataclass(frozen=True)
class Snapshot:
    base: str  # B'
    head: str  # H'
    entries: tuple[RawEntry, ...]


def build_snapshot(git: Git, repo: Path, work: Path, base: str, head: str, files: dict[str, bytes],
                   ts: int) -> Snapshot:  # fmt: skip
    """``B'`` and ``H'`` in ``repo`` with a temporary ``GIT_INDEX_FILE`` under ``work`` (plumbing only)."""
    entries = raw_diff(git, repo, base, head)
    if any(e.path == REVIEW_DIR or e.path.startswith(f"{REVIEW_DIR}/") for e in entries):
        raise MirrorRefused("the candidate touches .ossbot-review/")
    work.mkdir(parents=True, exist_ok=True)
    index = work / f"mirror-{secrets.token_hex(6)}.index"
    stamp = f"@{ts} +0000"
    env = {"GIT_INDEX_FILE": str(index), "GIT_AUTHOR_NAME": IDENTITY[0], "GIT_AUTHOR_EMAIL": IDENTITY[1],
           "GIT_COMMITTER_NAME": IDENTITY[0], "GIT_COMMITTER_EMAIL": IDENTITY[1], "GIT_AUTHOR_DATE": stamp,
           "GIT_COMMITTER_DATE": stamp}  # fmt: skip

    def run(args: list[str], stdin: bytes | None = None) -> str:
        return git.run(args, cwd=repo, stdin=stdin, extra_env=env).stdout.decode("ascii", "replace").strip()

    def index_info(items: list[tuple[str, str, str]]) -> None:  # (mode, blob, path), NUL-separated
        run(["read-tree", "--empty"])
        if items:
            stdin = b"".join(f"{mode} {blob}\t".encode() + path.encode("utf-8", "surrogateescape") + b"\0"
                             for mode, blob, path in items)  # fmt: skip
            run(["update-index", "-z", "--index-info"], stdin)

    try:
        index_info([(e.old_mode, e.old_blob, e.path) for e in entries if e.old_mode != "000000"])
        b_commit = run(["commit-tree", run(["write-tree"]), "-m", "review base (touched files at base)"])
        extra = [("100644", run(["hash-object", "-w", "--stdin"], data), f"{REVIEW_DIR}/{name}")
                 for name, data in sorted(files.items())]  # fmt: skip
        index_info([(e.new_mode, e.new_blob, e.path) for e in entries if e.new_mode != "000000"] + extra)
        h_commit = run(["commit-tree", run(["write-tree"]), "-p", b_commit, "-m", "review head (display copy)"])
    finally:
        index.unlink(missing_ok=True)
    if not SHA_RE.match(b_commit) or not SHA_RE.match(h_commit):
        raise MirrorRefused("plumbing produced no commit")
    return Snapshot(b_commit, h_commit, tuple(entries))


def integrity_ok(git: Git, repo: Path, base: str, head: str, snap: Snapshot) -> bool:
    """``diff --raw B' H'`` outside ``.ossbot-review/`` equals ``diff --raw base head``."""
    mirrored = [e for e in raw_diff(git, repo, snap.base, snap.head)
                if not (e.path == REVIEW_DIR or e.path.startswith(f"{REVIEW_DIR}/"))]  # fmt: skip
    return sorted(mirrored, key=lambda e: e.path) == sorted(raw_diff(git, repo, base, head), key=lambda e: e.path)


# ------------------------------------------------------------------------------- read-side helpers
def current_row(conn: Any, cid: str, rev: int) -> Any:
    return db.one(conn, "SELECT * FROM review_mirror WHERE cid = ? AND rev = ?", (cid, rev))


def explorer_line(conn: Any, cid: str, rev: int) -> str | None:
    """Explorer section 1: ``🐙 Mirror PR #12 · tree-equal ✅`` or ``🐙 Mirror unavailable: <reason>``."""
    row = current_row(conn, cid, rev)
    if row is None or row["closed_at"] is not None:
        return None
    if row["last_error"]:
        return f"🐙 Mirror unavailable: {str(row['last_error'])[:80]}"
    if row["pr_number"] is None:
        return None
    return f"🐙 Mirror PR #{row['pr_number']} · tree-equal {'✅' if row['tree_equal'] else '❌'}"


class ReviewMirror:
    """The gatekeeper's mirror service (``ctx.review_mirror``); ticks from the Telegram front-end."""

    def __init__(self, ctx: AppContext, client: httpx.AsyncClient | None, token: str | None,
                 classic: httpx.AsyncClient | None = None) -> None:  # fmt: skip
        self.ctx = ctx
        self.client = client
        self.token = token
        self.classic = classic  # the classic write token's client (enable probe: the mirror must be 404)
        self._polled: dict[tuple[str, int], datetime] = {}

    # --------------------------------------------------------------------- wiring
    def writer(self) -> MirrorWriter | None:
        repo = active_repo(self.ctx)
        if repo is None or self.client is None or not self.token:
            return None
        return MirrorWriter(self.client, self.ctx.gitops.git, self.token, self.ctx.config.profile.github_login, repo)

    def card_url(self, action_id: str) -> str | None:
        """Card row 4: the mirror PR of the action's rev, once its push succeeded (and while on)."""
        repo = active_repo(self.ctx)
        if repo is None:
            return None
        action = db.one(self.ctx.conn, "SELECT cid, kind, payload_json FROM outbound_actions WHERE id = ?",
                        (action_id,))  # fmt: skip
        if action is None or action["kind"] not in {"open_pr", "push_update"}:
            return None
        rev = json.loads(action["payload_json"]).get("rev")
        row = current_row(self.ctx.conn, str(action["cid"]), int(rev)) if isinstance(rev, int) else None
        if row is None or row["pr_number"] is None or row["closed_at"] is not None or row["last_error"]:
            return None
        return f"https://github.com/{repo}/pull/{int(row['pr_number'])}"

    # --------------------------------------------------------------------- enable / disable
    def disable(self, actor: str, reason: str) -> None:
        state = read_state(self.ctx)
        state.update({"enabled": False, "disabled_reason": reason, "disabled_at": to_iso(self.ctx.clock.now())})
        write_state(self.ctx, state)
        with db.transaction(self.ctx.conn):
            self.ctx.audit.append(actor, "review_mirror_disabled", {"reason": reason})

    def status(self) -> dict[str, Any]:
        state = read_state(self.ctx)
        return {"configured_repo": configured_repo(self.ctx), "config_enabled": self.ctx.config.telegram.review_mirror
                .enabled, "token": bool(self.token), "active": active_repo(self.ctx) is not None,
                "state": {k: state.get(k) for k in ("enabled", "repo", "enabled_at", "disabled_reason",
                                                    "token_expires", "checked_at")},
                "open_prs": int(self.ctx.conn.execute("SELECT COUNT(*) FROM review_mirror WHERE closed_at IS NULL"
                                                      " AND pr_number IS NOT NULL").fetchone()[0])}  # fmt: skip

    async def check_repo(self, repo: str) -> str | None:
        """``private`` and ``visibility == private``, and Actions disabled; the failure code or None."""
        assert self.client is not None
        reader = GitHubReader(self.client)
        doc = await reader.repo(repo)
        if doc is None:
            return "repo_not_found"
        if doc.get("private") is not True or doc.get("visibility") != "private":
            return "repo_public"
        perms = await reader.json_or_none(f"/repos/{repo}/actions/permissions")
        if not isinstance(perms, dict) or perms.get("enabled") is not False:
            return "actions_enabled"
        return None

    async def enable(self, actor: str) -> dict[str, Any]:
        """``ossctl review-mirror enable`` (human-initiated only): every probe must pass (ADR-0019 C)."""
        repo = configured_repo(self.ctx)
        login = self.ctx.config.profile.github_login
        if repo is None:
            return {"ok": False, "error": "no_repo", "detail": "set profile.yaml review_mirror.repo"}
        if self.client is None or not self.token:
            return {"ok": False, "error": "no_token", "detail": "GITHUB_REVIEW_MIRROR_TOKEN is not set"}
        try:
            resp = await self.client.get("/user")
            if resp.status_code != 200 or str(resp.json().get("login", "")).lower() != login.lower():
                return {"ok": False, "error": "wrong_identity"}
            expires = resp.headers.get("github-authentication-token-expiration")
            failed = await self.check_repo(repo)
            if failed is not None:
                return {"ok": False, "error": failed}
            probe = await self._foreign_write_probe(repo, login)
            if probe == 422:
                self.ctx.notify("🚨 The review mirror token can write outside the mirror repo: revoke it now."
                                " The mirror stays off.", priority=0)  # fmt: skip
                return {"ok": False, "error": "token_writes_elsewhere"}
            if probe not in {403, 404}:
                return {"ok": False, "error": f"foreign_write_probe_{probe}"}
            if self.classic is not None:
                seen = await self.classic.get(f"/repos/{repo}")
                if seen.status_code != 404:
                    return {"ok": False, "error": "classic_token_sees_mirror"}
        except (httpx.HTTPError, GitHubError, ValueError) as exc:
            return {"ok": False, "error": "probe_failed", "detail": type(exc).__name__}
        now = to_iso(self.ctx.clock.now())
        write_state(self.ctx, {"enabled": True, "repo": repo, "enabled_at": now, "enabled_by": actor,
                               "token_expires": expires, "checked_at": now})  # fmt: skip
        with db.transaction(self.ctx.conn):
            self.ctx.audit.append(actor, "review_mirror_enabled", {"repo": repo, "token_expires": expires})
        return {"ok": True, "repo": repo, "active": active_repo(self.ctx) is not None, "token_expires": expires}

    async def _foreign_write_probe(self, mirror: str, login: str) -> int:
        """``POST /repos/<public repo>/git/refs`` with an all-zero SHA on one of the owner's public repos:
        it can't create anything; 403/404 pass, 422 means the token can write there."""
        assert self.client is not None
        repos = await GitHubReader(self.client).json_or_none(f"/users/{login}/repos",
                                                              {"type": "owner", "per_page": "100"})  # fmt: skip
        public = [str(r["full_name"]) for r in repos or [] if isinstance(r, dict) and not r.get("private")
                  and str(r.get("full_name", "")).lower() != mirror.lower()]  # fmt: skip
        if not public:
            return 0
        ref = f"refs/heads/ossbot-probe-{secrets.token_hex(4)}"
        resp = await self.client.post(f"/repos/{public[0]}/git/refs", json={"ref": ref, "sha": ZERO_SHA})
        return resp.status_code

    async def purge(self, actor: str) -> dict[str, Any]:
        """``ossctl review-mirror purge``: close every open mirror PR, delete its branches, forget the rows.
        GitHub keeps ``refs/pull/<n>/head``: delete and recreate the repo to remove those."""
        writer = self.writer() or self._writer_for_purge()
        closed = 0
        for row in self.ctx.conn.execute("SELECT * FROM review_mirror WHERE closed_at IS NULL").fetchall():
            if writer is not None and row["pr_number"] is not None:
                with contextlib.suppress(GitHubError, MirrorWriteRefused, httpx.HTTPError):
                    await writer.close(int(row["pr_number"]), str(row["cid"]))
                    closed += 1
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute("UPDATE review_mirror SET closed_at = ? WHERE closed_at IS NULL",
                                  (to_iso(self.ctx.clock.now()),))  # fmt: skip
            self.ctx.audit.append(actor, "review_mirror_purged", {"closed": closed})
        return {"closed": closed, "note": "GitHub keeps refs/pull/<n>/head: delete and recreate the repo"}

    def _writer_for_purge(self) -> MirrorWriter | None:
        repo = configured_repo(self.ctx)
        if repo is None or self.client is None or not self.token:
            return None
        return MirrorWriter(self.client, self.ctx.gitops.git, self.token, self.ctx.config.profile.github_login, repo)

    # --------------------------------------------------------------------- push
    async def push(self, cid: str, rev: int, action_id: str) -> bool:
        """Build, check and push one rev; record it; add row 4 to the card. True when pushed."""
        writer = self.writer()
        contrib = db.one(self.ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
        if writer is None or contrib is None:
            return False
        ops = self.ctx.gitops
        meta = _meta(ops.derived_dir(cid, rev))
        repo_dir = ops.mirror_dir(str(contrib["upstream"]), cid, rev)
        branch = f"review/{cid}"
        try:
            base, head = str(meta["round_base"]), str(meta["head"])
            if not (SHA_RE.match(base) and SHA_RE.match(head)) or not repo_dir.exists():
                raise MirrorRefused("artifacts unavailable")
            ts = int(from_iso(json.loads(contrib["package_ts_json"] or "{}").get(str(rev))
                              or to_iso(self.ctx.clock.now())).timestamp())  # fmt: skip
            snap = build_snapshot(ops.git, repo_dir, ops.root / "work", base, head, review_files(contrib, rev), ts)
            if not integrity_ok(ops.git, repo_dir, base, head, snap):
                log.error("review mirror integrity mismatch for %s r%d", cid, rev,
                          extra={"event": "review_mirror_integrity"})  # fmt: skip
                raise MirrorRefused("integrity check failed")
            failed = await self.check_repo(writer.repo)
            if failed is not None:
                await self._repo_failed(failed)
                return False
            writer.push(repo_dir, snap.head, snap.base, cid)
            title = f"[{cid} r{rev}] " + defang(str(contrib["pr_title_override"] or contrib["pr_title"] or ""))
            pr = await writer.open_or_update(cid, title[:250], _pr_body(contrib, rev))
        except (MirrorRefused, GitHubError, GitError, MirrorWriteRefused, KeyError, ValueError,
                httpx.HTTPError) as exc:  # fmt: skip
            self._record(cid, rev, branch, None, ZERO_SHA, False, error=str(exc)[:200] or type(exc).__name__)
            return False
        self._record(cid, rev, branch, pr.number, snap.head, True, error=None)
        self._add_row4(action_id)
        return True

    def _record(self, cid: str, rev: int, branch: str, number: int | None, head: str, equal: bool, *,
                error: str | None) -> None:  # fmt: skip
        now = to_iso(self.ctx.clock.now())
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute(
                "INSERT INTO review_mirror (cid, rev, branch, pr_number, mirror_head, tree_equal, pushed_at,"
                " last_error) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(cid, rev) DO UPDATE SET pr_number ="
                " excluded.pr_number,"
                " mirror_head = excluded.mirror_head, tree_equal = excluded.tree_equal, pushed_at ="
                " excluded.pushed_at, last_error = excluded.last_error",
                (cid, rev, branch, number, head, int(equal), now if error is None else None, error),
            )  # fmt: skip
            if error is None:  # an older rev of the same cid shares the branch and the PR: it is done
                self.ctx.conn.execute("UPDATE review_mirror SET closed_at = ? WHERE cid = ? AND rev < ? AND"
                                      " closed_at IS NULL", (now, cid, rev))  # fmt: skip
            self.ctx.audit.append("system", "review_mirror_pushed" if error is None else "review_mirror_failed",
                                  {"rev": rev, "pr": number, "error": error}, cid)  # fmt: skip

    def _add_row4(self, action_id: str) -> None:
        """Late push: ``editMessageReplyMarkup`` with the card's own unused tokens (none minted or rotated)."""
        from ossbot.gatekeeper.telegram import keyboards  # noqa: PLC0415 - telegram imports the context

        now = to_iso(self.ctx.clock.now())
        for a in self.ctx.conn.execute("SELECT * FROM approvals WHERE action_id = ? AND status = 'pending' AND"
                                       " tg_message_id IS NOT NULL", (action_id,)).fetchall():  # fmt: skip
            tokens = {str(r["purpose"]): str(r["token"]) for r in self.ctx.conn.execute(
                "SELECT purpose, token FROM ui_tokens WHERE approval_id = ? AND tg_message_id = ? AND expires_at > ?"
                " AND (single_use = 0 OR used_at IS NULL)", (a["id"], a["tg_message_id"], now))}  # fmt: skip
            try:
                rows = self.ctx.approvals.card_rows(str(a["id"]), tokens)
            except KeyError:  # the card isn't armable yet (parts still delivering): row 4 comes with the arm edit
                continue
            self.ctx.outbox.enqueue("editMessageReplyMarkup", {"chat_id": a["tg_chat_id"], "message_id":
                                    a["tg_message_id"], "reply_markup": keyboards.to_json(rows)},
                                    dedupe_key=f"mirror_row4:{a['id']}", thread_key="reviews")  # fmt: skip

    async def _repo_failed(self, code: str) -> None:
        self.disable("system", code)
        if code == "repo_public":
            self.ctx.notify("🚨 The review mirror repo is PUBLIC: the mirror is off. Make it private, then run"
                            " ossctl review-mirror enable.", priority=0)  # fmt: skip
        else:
            self.ctx.notify(f"⚠️ Review mirror off: {code}. Fix it, then run ossctl review-mirror enable.",
                            priority=1)  # fmt: skip

    # --------------------------------------------------------------------- read back
    async def poll(self, cid: str, rev: int, action_id: str) -> int:
        """Map the owner's mirror review activity into Telegram; returns the number of items handled."""
        from ossbot.gatekeeper.telegram.review import comments  # noqa: PLC0415 - telegram imports the context

        row = current_row(self.ctx.conn, cid, rev)
        repo = active_repo(self.ctx)
        if row is None or row["pr_number"] is None or row["closed_at"] is not None or repo is None:
            return 0
        assert self.client is not None
        reader = GitHubReader(self.client)
        number, login = int(row["pr_number"]), self.ctx.config.profile.github_login.lower()
        paths = _touched(self.ctx, cid, rev)
        mine = [r for r in await reader._list(f"/repos/{repo}/pulls/{number}/reviews")
                if str((r.get("user") or {}).get("login", "")).lower() == login]  # fmt: skip
        line_comments = [c for c in await reader._list(f"/repos/{repo}/pulls/{number}/comments")
                         if str((c.get("user") or {}).get("login", "")).lower() == login]  # fmt: skip
        handled = 0
        for c in await reader._list(f"/repos/{repo}/issues/{number}/comments"):
            if str((c.get("user") or {}).get("login", "")).lower() == login:
                handled += self._save(cid, rev, action_id, f"gi{c.get('id')}", None, None, str(c.get("body") or ""))
        for review in mine:
            state, rid = str(review.get("state") or ""), review.get("id")
            if state == "APPROVED":
                self._approve_note(cid, action_id, rid)
                continue
            if state not in {"COMMENTED", "CHANGES_REQUESTED"} or self._seen(cid, f"review:{rid}"):
                continue
            if self._armed(action_id):  # a pending Confirm can't take a change request: next poll
                break
            items = [c for c in line_comments if c.get("pull_request_review_id") == rid]
            for c in items:
                path = str(c.get("path") or "")
                outdated = str(c.get("commit_id") or "") != str(row["mirror_head"])
                text = ("(outdated) " if outdated else "") + str(c.get("body") or "")
                self._save(cid, rev, action_id, f"gr{c.get('id')}", path if path in paths else None,
                           c, text)  # fmt: skip
            self._mark(cid, f"review:{rid}")
            result = comments.send(self.ctx, cid, rev, action_id, str(review.get("body") or ""), actor="mirror")
            self._note(cid, action_id, f"🐙 GitHub review · {len(items)} comments → change request ({result[:80]})",
                       f"mirror:review:{rid}")  # fmt: skip
            handled += 1
        pr = await reader.pull(repo, number) or {}
        moved = str((pr.get("head") or {}).get("sha") or "")
        if SHA_RE.match(moved) and moved != str(row["mirror_head"]) and not self._seen(cid, f"commit:{moved}") \
                and not self._armed(action_id):  # fmt: skip
            handled += await self._mirror_commit(reader, repo, cid, rev, action_id, str(row["mirror_head"]), moved)
        return handled

    def _armed(self, action_id: str) -> bool:
        return db.one(self.ctx.conn, "SELECT 1 FROM approvals WHERE action_id = ? AND status = 'armed'",
                      (action_id,)) is not None  # fmt: skip

    def _seen(self, cid: str, key: str) -> bool:
        return key in read_state(self.ctx).get("seen", {}).get(cid, [])

    def _mark(self, cid: str, key: str) -> None:
        state = read_state(self.ctx)
        seen = state.setdefault("seen", {})
        seen[cid] = [*seen.get(cid, []), key][-200:]
        write_state(self.ctx, state)

    def _save(self, cid: str, rev: int, action_id: str, gid: str, path: str | None, c: dict[str, Any] | None,
              text: str) -> int:  # fmt: skip
        """One draft comment with source ``mirror`` (id from the GitHub id, so a poll never doubles it)."""
        body = text.strip()[:COMMENT_MAX]
        if not body:
            return 0
        line = (c or {}).get("line") if path else None
        start = (c or {}).get("start_line") or line
        side = "old" if (c or {}).get("side") == "LEFT" else "new"
        with db.transaction(self.ctx.conn):
            cur = self.ctx.conn.execute(
                "INSERT OR IGNORE INTO review_comments (id, cid, rev, approval_id, source, path, side, line_start,"
                " line_end, quote, text, created_at) VALUES (?,?,?,NULL,'mirror',?,?,?,?,NULL,?,?)",
                (f"m{gid}"[:40], cid, rev, path, side if path and line else None, start if path else None,
                 line if path else None, body, to_iso(self.ctx.clock.now())),
            )  # fmt: skip
        if cur.rowcount:
            from ossbot.gatekeeper.telegram.review import comments  # noqa: PLC0415 - telegram imports the context

            comments.refresh_draft(self.ctx, cid, rev, action_id)
        return int(cur.rowcount)

    def _approve_note(self, cid: str, action_id: str, rid: Any) -> None:
        """An Approve on GitHub never approves anything (ADR-0019 C)."""
        self._note(cid, action_id, "🐙 You approved on GitHub — approvals only count here", f"mirror:approve:{rid}",
                   show_card=True)  # fmt: skip

    def _note(self, cid: str, action_id: str, text: str, dedupe: str, *, show_card: bool = False) -> None:
        from ossbot.gatekeeper.telegram.review.actions import card_message  # noqa: PLC0415
        from ossbot.gatekeeper.telegram.ui import kit as k  # noqa: PLC0415
        from ossbot.gatekeeper.telegram.ui import send as ui_send  # noqa: PLC0415

        rows = [[k.button("⬇️ Show card", "show_card", {"cid": cid, "action_id": action_id})]] if show_card else []
        view = k.plain("fyi", k.line(text), rows=rows)
        ui_send.deliver(self.ctx, view, thread="reviews", reply_to=card_message(self.ctx, action_id), silent=True,
                        dedupe_key=dedupe)  # fmt: skip

    async def _mirror_commit(self, reader: GitHubReader, repo: str, cid: str, rev: int, action_id: str, base: str,
                             moved: str) -> int:  # fmt: skip
        """A commit made on the mirror branch: a change request with its compare patch as guidance."""
        from ossbot.gatekeeper.telegram.review import comments  # noqa: PLC0415 - telegram imports the context

        doc = await reader.json_or_none(f"/repos/{repo}/compare/{base}...{moved}") or {}
        patches = [f"--- {f.get('filename')}\n{f.get('patch') or ''}" for f in doc.get("files", [])
                   if isinstance(f, dict) and not str(f.get("filename", "")).startswith(f"{REVIEW_DIR}/")]  # fmt: skip
        self._mark(cid, f"commit:{moved}")
        if not patches:
            return 0
        guidance = "Edited on the GitHub review mirror; apply the same change:\n\n```diff\n" + \
            "\n".join(patches)[:GUIDANCE_MAX] + "\n```"  # fmt: skip
        result = comments.send(self.ctx, cid, rev, action_id, guidance, actor="mirror")
        self._note(cid, action_id, f"🐙 GitHub edit → change request ({result[:80]})", f"mirror:commit:{moved}")
        return 1

    async def close(self, cid: str, rev: int) -> None:
        writer = self.writer() or self._writer_for_purge()
        row = current_row(self.ctx.conn, cid, rev)
        if row is None or row["closed_at"] is not None:
            return
        if writer is not None and row["pr_number"] is not None:
            try:
                await writer.close(int(row["pr_number"]), cid)
            except (GitHubError, MirrorWriteRefused, httpx.HTTPError) as exc:
                log.warning("review mirror close failed: %s", type(exc).__name__,
                            extra={"event": "review_mirror_close_failed"})  # fmt: skip
                return
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute("UPDATE review_mirror SET closed_at = ? WHERE cid = ? AND rev = ?",
                                  (to_iso(self.ctx.clock.now()), cid, rev))  # fmt: skip
            self.ctx.audit.append("system", "review_mirror_closed", {"rev": rev}, cid)

    # --------------------------------------------------------------------- tick
    async def tick(self) -> int:
        """One pass: repo check (10 min), token expiry reminder, retention, pushes, polls."""
        repo = active_repo(self.ctx)
        if repo is None or self.client is None or self.ctx.control.view().state != "running":
            return 0
        now = self.ctx.clock.now()
        state = read_state(self.ctx)
        if state.get("checked_at") is None or now - from_iso(str(state["checked_at"])) >= timedelta(
                seconds=REPO_CHECK_S):  # fmt: skip
            failed = await self.check_repo(repo)
            state = read_state(self.ctx)
            state["checked_at"] = to_iso(now)
            write_state(self.ctx, state)
            if failed is not None:
                await self._repo_failed(failed)
                return 0
        self._expiry_reminder(state, now)
        done = 0
        for row in self.ctx.conn.execute("SELECT * FROM review_mirror WHERE closed_at IS NULL").fetchall():
            if not self._card_open(str(row["cid"]), int(row["rev"])):
                await self.close(str(row["cid"]), int(row["rev"]))
                done += 1
        cfg = self.ctx.config.telegram.review_mirror
        for a in self._open_cards():
            cid, rev, action_id = str(a["cid"]), int(a["rev"]), str(a["action_id"])
            row = current_row(self.ctx.conn, cid, rev)
            if row is None:
                done += int(await self.push(cid, rev, action_id))
                continue
            interval = cfg.poll_pending_s if a["status"] == "pending" else cfg.poll_other_s
            last = self._polled.get((cid, rev))
            if row["pr_number"] is not None and (last is None or now - last >= timedelta(seconds=interval)):
                self._polled[(cid, rev)] = now
                done += await self.poll(cid, rev, action_id)
        return done

    def _open_cards(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.ctx.conn.execute(
            "SELECT a.status, o.id AS action_id, o.cid, o.payload_json FROM approvals a JOIN outbound_actions o"
            " ON o.id = a.action_id JOIN contributions c ON c.id = o.cid WHERE a.status IN ('pending','armed')"
            " AND o.kind IN ('open_pr','push_update') AND c.state = ?", (State.AWAITING_APPROVAL.value,),
        ):  # fmt: skip
            rev = json.loads(r["payload_json"]).get("rev")
            if isinstance(rev, int):
                out.append({"status": r["status"], "action_id": r["action_id"], "cid": r["cid"], "rev": rev})
        return out

    def _card_open(self, cid: str, rev: int) -> bool:
        return any(c["cid"] == cid and c["rev"] == rev for c in self._open_cards())

    def _expiry_reminder(self, state: dict[str, Any], now: datetime) -> None:
        expires = state.get("token_expires")
        if not expires or state.get("expiry_reminded"):
            return
        try:
            at = datetime.strptime(str(expires)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
        except ValueError:
            return
        if at - now <= EXPIRY_WARN:
            self.ctx.notify(f"⏰ The review mirror token expires {at:%Y-%m-%d}: renew it and run setup-secrets.",
                            priority=1)  # fmt: skip
            state["expiry_reminded"] = True
            write_state(self.ctx, state)


def _meta(derived: Path) -> dict[str, Any]:
    try:
        doc = json.loads((derived / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _touched(ctx: AppContext, cid: str, rev: int) -> set[str]:
    """This rev's paths (a mirror comment on any other path is a general comment)."""
    from ossbot.gatekeeper.telegram.review import patchview  # noqa: PLC0415 - telegram imports the context

    return {f.path for f in patchview.load(ctx.gitops.derived_dir(cid, rev))}


def _pr_body(contrib: Any, rev: int) -> str:
    return ("Display copy for review only: this PR is never merged and never goes upstream.\n\n"
            f"- Contribution: `{contrib['id']}` r{rev}\n- Upstream: `{contrib['upstream']}`\n\n"
            "Comment or Request changes here: your review becomes one change request in Telegram. "
            "An Approve here approves nothing; approve on the Telegram card.\n")  # fmt: skip
