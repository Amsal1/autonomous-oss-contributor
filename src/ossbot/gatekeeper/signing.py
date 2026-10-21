"""SSH commit signing in the gatekeeper's own mirror (owner addition 2026-10-21; ADR-0006 addendum, I6).

The optional ``GITHUB_COMMIT_SIGNING_KEY`` (an unencrypted OpenSSH ed25519 private key) lives in
``gatekeeper.env`` only, stored by ``setup-secrets.sh`` as the single-line base64 body of the key
block. It never reaches a sandbox, Hermes or the runner.

``/package`` re-creates the verified deterministic commit with the identical tree, parents, author,
committer and message plus an SSH ``gpgsig`` header (``sign_commit``): the raw commit object is
signed with ``ssh-keygen -Y sign -n git`` and the signature is inserted where git puts it, so the
signed payload is the unsigned object byte for byte. The signed SHA is the packaged SHA: the
preview, the approval binding (I1, I12) and the push all use it. The key reaches ``ssh-keygen`` only
through an inherited 0600 memfd (``/proc/self/fd/N``), never a file on disk, argv or the
environment, and is closed right after each call. ``verify`` runs ``git verify-commit`` against an
allowed-signers file built from the key's own public half.

Policy ``policy.commit.signing``: ``auto`` (default) signs whenever the key is set, ``required``
makes ``/package`` refuse without a key, ``off`` never signs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ossbot.gatekeeper.gitops import GIT_ARGS, SHA_RE, Git

SIGNING_ENV = "GITHUB_COMMIT_SIGNING_KEY"
KEYGEN = "/usr/bin/ssh-keygen"
NAMESPACE = "git"
PEM_HEAD = "-----BEGIN OPENSSH PRIVATE KEY-----"
PEM_TAIL = "-----END OPENSSH PRIVATE KEY-----"
PEM_WIDTH = 70  # ssh-keygen wraps the key body at 70 columns
BODY_RE = re.compile(r"^[A-Za-z0-9+/]{64,8000}={0,2}$")
KEY_MAGIC = b"openssh-key-v1\0"
KEY_TYPE = "ssh-ed25519"
SIG_HEADER = b"gpgsig"
MIN_NEEDLE = 16
KEYGEN_TIMEOUT_S = 30.0

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class SigningError(RuntimeError):
    """The key is malformed or ``ssh-keygen``/git refused; the message never carries key material."""


@dataclass(frozen=True)
class Signature:
    """What the cards show: ``🔏 Commit signed (SSH, key SHA256:…)``."""

    fingerprint: str
    format: str = "ssh"

    def as_dict(self) -> dict[str, str]:
        return {"format": self.format, "fingerprint": self.fingerprint}


# ------------------------------------------------------------------------- key encoding
def body_from_pem(text: str) -> str:
    """The single-line body of an OpenSSH private-key block (what ``gatekeeper.env`` stores)."""
    lines = [ln.strip() for ln in text.replace("\r", "").strip().split("\n")]
    if len(lines) < 3 or lines[0] != PEM_HEAD or lines[-1] != PEM_TAIL:
        raise SigningError("not an OpenSSH private-key block")
    body = "".join(lines[1:-1])
    check_body(body)
    return body


def check_body(body: str) -> bytes:
    """The decoded key; refuses anything that is not an OpenSSH private key."""
    if not BODY_RE.match(body):
        raise SigningError(f"{SIGNING_ENV} is not a single-line OpenSSH key body")
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SigningError(f"{SIGNING_ENV} is not valid base64") from exc
    if not raw.startswith(KEY_MAGIC):
        raise SigningError(f"{SIGNING_ENV} is not an OpenSSH private key")
    return raw


def wrapped(body: str) -> list[str]:
    return [body[i : i + PEM_WIDTH] for i in range(0, len(body), PEM_WIDTH)]


def pem_from_body(body: str) -> bytes:
    return ("\n".join([PEM_HEAD, *wrapped(body), PEM_TAIL]) + "\n").encode("ascii")


def key_needles(body: str | None) -> list[str]:
    """Scanner and log-redactor values for the key (U14): every 70-column line of the block but the
    first, which is the same for every unencrypted ed25519 key. A verbatim copy of the key, wrapped
    or as one line, contains all of them; each is short and whitespace-free (``secret_scan``)."""
    if not body:
        return []
    return [line for line in wrapped(body)[1:] if len(line) >= MIN_NEEDLE]


def fingerprint(public_key: str) -> str:
    """``SHA256:<base64>`` of the public key blob, as ``ssh-keygen -l`` and GitHub print it."""
    parts = public_key.split()
    if len(parts) < 2:
        raise SigningError("unparseable public key")
    blob = base64.b64decode(parts[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def same_key(a: str, b: str) -> bool:
    """Two ``<type> <base64> [comment]`` public keys name the same key (comments ignored)."""
    pa, pb = a.split(), b.split()
    return len(pa) >= 2 and len(pb) >= 2 and pa[:2] == pb[:2]


# ------------------------------------------------------------------------ commit objects
def header_end(raw: bytes) -> int:
    """Index just after the last header line (where git inserts ``gpgsig``)."""
    end = raw.find(b"\n\n")
    return len(raw) if end < 0 else end + 1


def is_signed(raw: bytes) -> bool:
    return any(line.startswith(SIG_HEADER + b" ") for line in raw[: header_end(raw)].split(b"\n"))


def insert_signature(raw: bytes, signature: bytes) -> bytes:
    """git's ``add_header_signature``: ``gpgsig`` + every signature line prefixed by one space."""
    if not signature.endswith(b"\n"):
        signature += b"\n"
    pos = header_end(raw)
    lines = signature.splitlines(keepends=True)
    return raw[:pos] + SIG_HEADER + b"".join(b" " + line for line in lines) + raw[pos:]


def strip_signature(raw: bytes) -> bytes:
    """The signed payload of a signed commit object (the unsigned object, byte for byte)."""
    pos = header_end(raw)
    head = raw[:pos].split(b"\n")
    out: list[bytes] = []
    skipping = False
    for line in head:
        if line.startswith(SIG_HEADER + b" "):
            skipping = True
            continue
        if skipping and line.startswith(b" "):
            continue
        skipping = False
        out.append(line)
    return b"\n".join(out) + raw[pos:]


# ----------------------------------------------------------------------------- signer
class CommitSigner:
    def __init__(self, body: str, *, keygen: str = KEYGEN, runner: Runner = subprocess.run) -> None:
        check_body(body)
        self._pem = pem_from_body(body)
        self.keygen = keygen
        self.runner = runner
        proc = self._with_key(["-y", "-f"], None)
        public = proc.stdout.decode("ascii", "replace").strip()
        if proc.returncode != 0 or not public:
            raise SigningError(f"ssh-keygen could not read the key (exit {proc.returncode}; a passphrase?)")
        if public.split()[0] != KEY_TYPE:
            raise SigningError(f"the signing key must be {KEY_TYPE}, not {public.split()[0][:30]}")
        self.public_key = " ".join(public.split()[:2])
        self.fingerprint = fingerprint(self.public_key)

    @property
    def signature(self) -> Signature:
        return Signature(self.fingerprint)

    def _with_key(self, args: Sequence[str], stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
        """Run ``ssh-keygen <args> /proc/self/fd/N [...]`` with the key on an inherited 0600 memfd."""
        fd = os.memfd_create("ossbot-signing-key", os.MFD_CLOEXEC)
        try:
            os.write(fd, self._pem)
            os.fchmod(fd, 0o600)
            os.set_inheritable(fd, True)
            argv = [self.keygen, *args, f"/proc/self/fd/{fd}"]  # every call ends with ``-f <key>``
            try:
                return self.runner(
                    argv, input=stdin, capture_output=True, check=False, timeout=KEYGEN_TIMEOUT_S, pass_fds=(fd,),
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}, cwd="/",
                )  # fmt: skip
            except (OSError, subprocess.SubprocessError) as exc:
                raise SigningError(f"ssh-keygen unavailable: {type(exc).__name__}") from exc
        finally:
            os.close(fd)

    def sign(self, payload: bytes) -> bytes:
        """The armored SSH signature (namespace ``git``) of ``payload``."""
        proc = self._with_key(["-Y", "sign", "-n", NAMESPACE, "-f"], payload)
        sig = proc.stdout
        if proc.returncode != 0 or not sig.startswith(b"-----BEGIN SSH SIGNATURE-----"):
            raise SigningError(f"ssh-keygen -Y sign failed (exit {proc.returncode})")
        return sig

    def sign_commit(self, git: Git, repo: Path, sha: str) -> str:
        """Write the signed twin of commit ``sha`` into ``repo``; returns its SHA (``sha`` is untouched)."""
        raw = git.run(["cat-file", "commit", sha], cwd=repo).stdout
        if is_signed(raw):
            raise SigningError("the commit already carries a signature")
        signed = insert_signature(raw, self.sign(raw))
        if strip_signature(signed) != raw:  # the payload must stay the unsigned object byte for byte
            raise SigningError("signature insertion changed the commit")
        new = git.run(["hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, stdin=signed)
        out = new.stdout.decode("ascii", "replace").strip()
        if not SHA_RE.match(out):
            raise SigningError("git hash-object returned no SHA")
        return out

    def allowed_signers(self, principal: str) -> str:
        return f'{principal} namespaces="{NAMESPACE}" {self.public_key}\n'

    def verify(self, git: Git, repo: Path, sha: str, principal: str) -> bool:
        """``git verify-commit`` with an allowed-signers file of this key's public half (public data)."""
        home = git.home
        home.mkdir(parents=True, exist_ok=True)
        path = home / "allowed_signers"
        tmp = home / f".allowed_signers.tmp-{os.getpid()}"
        tmp.write_text(self.allowed_signers(principal), encoding="utf-8")
        os.replace(tmp, path)
        args = [*GIT_ARGS, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={path}",
                "-c", f"gpg.ssh.program={self.keygen}"]  # fmt: skip
        return git.run(["verify-commit", sha], cwd=repo, git_args=args, check=False).returncode == 0

    def selftest(self, root: Path, principal: str = "selftest@ossbot.invalid") -> bool:
        """Sign and verify a throw-away commit in a scratch bare repo under ``root`` (doctor)."""
        git = Git(home=root / "home")
        repo = root / "selftest.git"
        root.mkdir(parents=True, exist_ok=True)
        git.run(["init", "-q", "--bare", "--object-format=sha1", str(repo)], cwd=root)
        tree = git.run(["hash-object", "-t", "tree", "-w", "--stdin"], cwd=repo, stdin=b"").stdout.decode().strip()
        ident = {"GIT_AUTHOR_NAME": "ossbot selftest", "GIT_AUTHOR_EMAIL": principal,
                 "GIT_COMMITTER_NAME": "ossbot selftest", "GIT_COMMITTER_EMAIL": principal}  # fmt: skip
        sha = git.run(["commit-tree", tree, "-m", "ossbot signing selftest"], cwd=repo, extra_env=ident)
        signed = self.sign_commit(git, repo, sha.stdout.decode().strip())
        return self.verify(git, repo, signed, principal)


def load(env: Mapping[str, str], **kw: Any) -> CommitSigner | None:
    """The signer of ``gatekeeper.env``, None when the key is unset; ``SigningError`` when it is broken."""
    body = (env.get(SIGNING_ENV) or "").strip()
    return CommitSigner(body, **kw) if body else None


# ------------------------------------------------------------------------- the cards
def from_payload(payload: Mapping[str, Any]) -> Signature | None:
    sig = payload.get("signature")
    if isinstance(sig, dict) and isinstance(sig.get("fingerprint"), str):
        return Signature(str(sig["fingerprint"])[:80], str(sig.get("format") or "ssh")[:10])
    return None


def from_meta(meta: Mapping[str, Any]) -> Signature | None:
    return from_payload(meta)


def card_text(sig: Signature | None) -> str:
    if sig is None:
        return "⚠️ Commit not signed"
    return f"🔏 Commit signed ({sig.format.upper()}, key {sig.fingerprint})"


# --------------------------------------------------------------- submit-time refusal
UNSIGNED_REFUSAL = (
    "🔏 {repo} requires signed commits and r{rev} is not signed: nothing was published. Set"
    " GITHUB_COMMIT_SIGNING_KEY (docs/SETUP.md), then tap Request changes so a signed rev is packaged."
)


def unsigned_refusal(upstream: str, vetting: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    """The refusal text when a repo vet flagged ``signed_commits_required`` gets an unsigned rev."""
    if not vetting.get("signed_commits_required") or from_payload(payload) is not None:
        return None
    return UNSIGNED_REFUSAL.format(repo=upstream, rev=payload.get("rev", "?"))


def refusal_for_approval(conn: sqlite3.Connection, approval_id: str) -> str | None:
    row = conn.execute(
        "SELECT o.kind, o.payload_json, c.upstream, c.vetting_json FROM approvals a JOIN outbound_actions o"
        " ON o.id = a.action_id JOIN contributions c ON c.id = o.cid WHERE a.id = ?",
        (approval_id,),
    ).fetchone()
    if row is None or row["kind"] not in ("open_pr", "push_update"):
        return None
    try:
        vetting = json.loads(row["vetting_json"] or "{}")
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        return None
    if not isinstance(vetting, dict) or not isinstance(payload, dict):
        return None
    return unsigned_refusal(str(row["upstream"]), vetting, payload)
