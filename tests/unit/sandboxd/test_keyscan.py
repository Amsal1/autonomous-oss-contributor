"""Kiro key output scanning (DESIGN §10.6, §22.1): every encoding, files, logs, bundle blobs."""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from ossbot.sandboxd.keyscan import BundleFormatError, KeyScanner, needles

KEY = "ksk_live_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
SCANNER = KeyScanner([KEY])
RAW = KEY.encode()


def _encodings() -> dict[str, bytes]:
    out = {
        "literal": RAW,
        "reversed": RAW[::-1],
        "hex_lower": RAW.hex().encode(),
        "hex_upper": RAW.hex().upper().encode(),
        "url_encoded": quote(KEY, safe="").encode(),
    }
    for k in range(3):
        prefix = b"xyz"[:k]
        out[f"b64_std_align{k}"] = base64.b64encode(prefix + RAW)
        out[f"b64_url_align{k}"] = base64.urlsafe_b64encode(prefix + RAW)
        out[f"b64_url_align{k}_suffix"] = base64.urlsafe_b64encode(prefix + RAW + b"tail")
    return out


@pytest.mark.parametrize(("name", "blob"), sorted(_encodings().items()))
def test_every_encoding_detected_in_text(name: str, blob: bytes) -> None:
    assert SCANNER.hit(b"prefix junk " + blob + b" suffix junk"), name


@pytest.mark.parametrize(("name", "blob"), sorted(_encodings().items()))
def test_every_encoding_detected_in_file(name: str, blob: bytes, tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    path.write_bytes(os.urandom(3 * 1024 * 1024).hex().encode()[: 3 * 1024 * 1024 - 7] + blob + b"\n")
    assert SCANNER.scan_file(path), name


def test_hit_across_chunk_boundary() -> None:
    blob = b"a" * 100 + RAW + b"b" * 100
    chunks = [blob[i : i + 7] for i in range(0, len(blob), 7)]
    assert SCANNER.scan_stream(chunks)


def test_clean_outputs_pass(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    path.write_text('{"plan": "nothing secret here", "ksk": "not a key"}')
    assert not SCANNER.scan_file(path)
    assert not SCANNER.hit(b"ksk_live_ but not the rest")


def test_redaction_of_logs() -> None:
    log = b"start " + RAW + b" middle " + base64.b64encode(RAW) + b" end"
    redacted = SCANNER.redact(log)
    assert not SCANNER.hit(redacted)
    assert redacted.startswith(b"start [REDACTED] middle ")


def test_needles_are_long_enough() -> None:
    assert all(len(n) >= 8 for n in needles(KEY))
    with pytest.raises(ValueError):
        KeyScanner(["short"])


def test_symlink_output_refused(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_text("x")
    (tmp_path / "l").symlink_to(target)
    with pytest.raises(OSError):
        SCANNER.scan_file(tmp_path / "l")


# ------------------------------------------------------------------------ bundles
def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(
        ["/usr/bin/git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _thin_bundle(tmp_path: Path, content: bytes) -> Path:
    """A fix-round style bundle: one commit on top of a base the scanner does not have."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    big = "\n".join(f"line {i} of a reasonably long file that git will delta against" for i in range(400))
    (repo / "app.py").write_text(big + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_bytes(big.encode() + b"\n# " + content + b"\n")
    (repo / "notes.txt").write_bytes(b"new file\n")
    _git(repo, "checkout", "-q", "-b", "ossbot-change")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    bundle = tmp_path / "change.bundle"
    _git(repo, "bundle", "create", "-q", str(bundle), "refs/heads/ossbot-change", f"^{base}")
    return bundle


@pytest.mark.parametrize("encoding", ["literal", "b64_std_align1", "hex_lower", "reversed"])
def test_key_inside_bundle_blob_detected(encoding: str, tmp_path: Path) -> None:
    bundle = _thin_bundle(tmp_path, _encodings()[encoding])
    assert SCANNER.scan_bundle(bundle)


def test_thin_bundle_cannot_be_unbundled_without_base(tmp_path: Path) -> None:
    """Why the scanner inflates the pack itself instead of `git bundle unbundle` (§10.6 deviation)."""
    bundle = _thin_bundle(tmp_path, b"x")
    empty = tmp_path / "empty.git"
    _git(tmp_path, "init", "-q", "--bare", str(empty))
    proc = subprocess.run(
        ["/usr/bin/git", "-C", str(empty), "bundle", "unbundle", str(bundle)],
        capture_output=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode != 0


def test_clean_bundle_passes(tmp_path: Path) -> None:
    assert not SCANNER.scan_bundle(_thin_bundle(tmp_path, b"nothing to see"))


def test_key_in_commit_message_detected(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"oops {KEY}")
    bundle = tmp_path / "b.bundle"
    _git(repo, "bundle", "create", "-q", str(bundle), "refs/heads/main")
    assert SCANNER.scan_bundle(bundle)


def test_garbage_bundle_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "x.bundle"
    path.write_bytes(b"# v2 git bundle\n" + b"0" * 40 + b" refs/heads/x\n\nPACKgarbage")
    with pytest.raises(BundleFormatError):
        SCANNER.scan_bundle(path)
