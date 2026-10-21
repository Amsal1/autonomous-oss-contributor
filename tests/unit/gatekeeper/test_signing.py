"""SSH commit signing primitives (owner addition 2026-10-21, ADR-0006 addendum): key encoding, the
signed twin of a commit, verification, the scanner/redactor needles and the submit-time refusal."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ossbot.common.redact import REDACTED
from ossbot.gatekeeper import secret_scan, signing
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.gitops import Git
from tests.fakes.signing_key import KEYGEN, key_body, make_key, needs_keygen
from tests.githarness import tgit

pytestmark = [pytest.mark.unit, needs_keygen]
IDENT = {"GIT_AUTHOR_NAME": "Test Person", "GIT_AUTHOR_EMAIL": "test.person@example.org",
         "GIT_COMMITTER_NAME": "Test Person", "GIT_COMMITTER_EMAIL": "test.person@example.org",
         "GIT_AUTHOR_DATE": "2026-10-17T12:00:00Z", "GIT_COMMITTER_DATE": "2026-10-17T12:00:00Z"}  # fmt: skip


def repo_with_commit(tmp_path: Path) -> tuple[Git, Path, str]:
    work = tmp_path / "work"
    work.mkdir()
    tgit(work, "init", "-q")
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    tgit(work, "add", "-A")
    tgit(work, "commit", "-q", "-m", "base", env=IDENT)
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    tgit(work, "add", "-A")
    tgit(work, "commit", "-q", "-m", "fix: two\n\nSigned-off-by: Test Person <test.person@example.org>", env=IDENT)
    return Git(home=tmp_path / "githome"), work, tgit(work, "rev-parse", "HEAD")


def raw(repo: Path, sha: str) -> bytes:
    return subprocess.run(["/usr/bin/git", "cat-file", "commit", sha], cwd=repo, capture_output=True,
                          check=True).stdout  # fmt: skip


# ------------------------------------------------------------------------------ the key
def test_pem_body_round_trip_matches_ssh_keygen_layout(tmp_path: Path) -> None:
    pem = make_key(tmp_path).read_text(encoding="ascii")
    body = signing.body_from_pem(pem.replace("\n", "\r\n"))  # a key file copied from Windows
    assert "\n" not in body and signing.BODY_RE.match(body)
    assert signing.pem_from_body(body).decode("ascii") == pem


def test_fingerprint_equals_ssh_keygen(tmp_path: Path) -> None:
    path = make_key(tmp_path)
    signer = signing.CommitSigner(signing.body_from_pem(path.read_text(encoding="ascii")))
    listed = subprocess.run([KEYGEN, "-lf", f"{path}.pub"], capture_output=True, text=True, check=True).stdout
    assert listed.split()[1] == signer.fingerprint
    assert signing.same_key(signer.public_key, (tmp_path / "sign.pub").read_text(encoding="ascii"))


@pytest.mark.parametrize(("kind", "passphrase"), [("rsa", ""), ("ed25519", "a passphrase")])
def test_other_keys_are_refused_without_echoing_them(tmp_path: Path, kind: str, passphrase: str) -> None:
    pem = make_key(tmp_path, kind=kind, passphrase=passphrase).read_text(encoding="ascii")
    body = signing.body_from_pem(pem)
    with pytest.raises(signing.SigningError) as exc:
        signing.CommitSigner(body)
    assert not any(line in str(exc.value) for line in signing.key_needles(body))


@pytest.mark.parametrize("value", ["not a key", "QUJD" * 40, "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"])
def test_malformed_values_are_refused(value: str) -> None:
    with pytest.raises(signing.SigningError):
        signing.load({signing.SIGNING_ENV: value})
    assert signing.load({}) is None
    assert signing.load({signing.SIGNING_ENV: " "}) is None


# --------------------------------------------------------------------------- the commit
def test_signed_twin_keeps_tree_parents_identities_and_message(tmp_path: Path) -> None:
    git, repo, head = repo_with_commit(tmp_path)
    signer = signing.CommitSigner(key_body(tmp_path / "keys"))
    signed = signer.sign_commit(git, repo, head)
    assert signed != head
    signed_raw, unsigned_raw = raw(repo, signed), raw(repo, head)
    assert signing.is_signed(signed_raw) and not signing.is_signed(unsigned_raw)
    assert signing.strip_signature(signed_raw) == unsigned_raw  # tree, parent, author, committer, message
    header = signed_raw[: signing.header_end(signed_raw)].split(b"\n")
    sig_start = next(i for i, line in enumerate(header) if line.startswith(b"gpgsig "))
    assert header[sig_start] == b"gpgsig -----BEGIN SSH SIGNATURE-----"
    assert header[sig_start - 1].startswith(b"committer ")  # where git itself puts it
    assert signer.verify(git, repo, signed, "test.person@example.org")
    # git agrees: the signature is good and the payload is the unsigned commit
    assert signer.sign_commit(git, repo, head) == signed  # ed25519 is deterministic
    with pytest.raises(signing.SigningError, match="already"):
        signer.sign_commit(git, repo, signed)


def test_another_key_does_not_verify(tmp_path: Path) -> None:
    git, repo, head = repo_with_commit(tmp_path)
    mine = signing.CommitSigner(key_body(tmp_path / "keys", "mine"))
    other = signing.CommitSigner(key_body(tmp_path / "keys", "other"))
    signed = mine.sign_commit(git, repo, head)
    assert mine.verify(git, repo, signed, "p@example.org")
    assert not other.verify(git, repo, signed, "p@example.org")
    assert not mine.verify(git, repo, head, "p@example.org")  # unsigned


def test_selftest_and_the_key_never_lands_on_disk(tmp_path: Path) -> None:
    body = key_body(tmp_path / "keys")
    signer = signing.CommitSigner(body)
    assert signer.selftest(tmp_path / "scratch")
    needles = signing.key_needles(body)
    for path in (tmp_path / "scratch").rglob("*"):
        if path.is_file():
            data = path.read_bytes().decode("latin-1")
            assert not any(n in data for n in needles), path


# ------------------------------------------------------------------- scanner and redaction
def test_scanner_and_redactor_catch_the_key_wrapped_or_one_line(ctx: AppContext, tmp_path: Path) -> None:
    body = key_body(tmp_path)
    scanner = secret_scan.build(ctx.config, {signing.SIGNING_ENV: body})
    pem = signing.pem_from_body(body).decode("ascii")
    for leak in (pem, body, f"key: {body[70:140]}"):
        assert scanner.scan(leak) > 0
        shown, hits = scanner.redact(leak)
        assert hits > 0 and REDACTED in shown
        assert not any(n in shown for n in signing.key_needles(body))
    assert scanner.scan("the fingerprint " + signing.CommitSigner(body).fingerprint) == 0


def test_context_signer_follows_the_environment(ctx: AppContext, tmp_path: Path) -> None:
    assert ctx.signer is None
    body = key_body(tmp_path)
    ctx.env = {signing.SIGNING_ENV: body}
    signer = ctx.signer
    assert signer is not None and ctx.signer is signer  # cached
    ctx.env = {signing.SIGNING_ENV: "broken"}
    with pytest.raises(signing.SigningError):
        _ = ctx.signer


def test_the_key_is_gatekeeper_only() -> None:
    """Never the runner, sandboxd or Hermes: one module reads it, setup-secrets writes it to gatekeeper.env."""
    root = Path(__file__).resolve().parents[3]
    untrusted = [root / "src" / "ossbot" / d for d in ("runner", "sandboxd", "sandbox_tools", "dockerfilter")]
    untrusted += [root / "hermes", root / "sandbox"]
    leaks = sorted(p.relative_to(root).as_posix() for d in untrusted for p in d.rglob("*") if p.is_file()
                   and "SIGNING_KEY" in p.read_text(encoding="utf-8", errors="replace"))  # fmt: skip
    assert leaks == []
    script = (root / "scripts" / "setup-secrets.sh").read_text(encoding="utf-8")
    calls = re.findall(r"^\s*write_env .*?(?<!\\)$", script, flags=re.M | re.S)
    writers = [c.split()[1] for c in calls if "GITHUB_COMMIT_SIGNING_KEY=" in c]
    assert writers == ['"$gk"'], calls


# -------------------------------------------------------------------------- the refusal
def test_unsigned_refusal_only_for_signed_commit_repos() -> None:
    signed = {"rev": 2, "signature": {"format": "ssh", "fingerprint": "SHA256:abc"}}
    required = {"signed_commits_required": True}
    assert signing.unsigned_refusal("grafana/loki", required, {"rev": 2}) is not None
    assert "grafana/loki" in str(signing.unsigned_refusal("grafana/loki", required, {"rev": 2}))
    assert signing.unsigned_refusal("grafana/loki", required, signed) is None
    assert signing.unsigned_refusal("o/r", {}, {"rev": 1}) is None
    assert signing.card_text(None) == "⚠️ Commit not signed"
    assert signing.card_text(signing.from_payload(signed)) == "🔏 Commit signed (SSH, key SHA256:abc)"
