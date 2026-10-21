"""Throw-away SSH signing keys for the commit-signing tests (ADR-0006 addendum). Never a real key."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ossbot.gatekeeper import signing

KEYGEN = "/usr/bin/ssh-keygen"
needs_keygen = pytest.mark.skipif(shutil.which(KEYGEN) is None, reason="ssh-keygen not installed")


def make_key(directory: Path, name: str = "sign", *, kind: str = "ed25519", passphrase: str = "") -> Path:
    """A fresh private-key file (OpenSSH format) under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    args = [KEYGEN, "-q", "-t", kind, "-N", passphrase, "-C", "", "-f", str(path)]
    if kind == "rsa":
        args[4:4] = ["-b", "2048"]
    subprocess.run(args, check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return path


def key_body(directory: Path, name: str = "sign") -> str:
    """The one-line body ``setup-secrets.sh`` stores in ``gatekeeper.env``."""
    return signing.body_from_pem(make_key(directory, name).read_text(encoding="ascii"))
