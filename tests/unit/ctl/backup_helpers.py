"""Shared set-up for the backup/restore unit tests: a relocated layout with Hermes state, a fake age."""

from __future__ import annotations

import io
import json
import os
import pwd
import sqlite3
import sys
import tarfile
from pathlib import Path

from ossbot.common.paths import Paths
from ossbot.ctl.backup import Layout
from tests.fakes import fake_age

RECIPIENT = "age1" + "q" * 58
IDENTITY = f"FAKE-IDENTITY:{RECIPIENT}".encode()
CID_ACTIVE = "c_ACTIVE0001"
CID_DONE = "c_DONE000001"


def fake_age_bin(tmp: Path) -> Path:
    script = tmp / "bin" / "age"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f'#!/bin/sh\nexec {sys.executable} {Path(fake_age.__file__)} "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    return script


def sqlite_wal(path: Path, rows: int) -> sqlite3.Connection:
    """A WAL-mode database; while the returned connection stays open, its rows are only in the -wal file
    (no checkpoint), like a live Hermes or gatekeeper DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("CREATE TABLE IF NOT EXISTS t (n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


def populate(paths: Paths, tmp: Path) -> Layout:
    """Hermes home, skill gate state, anchors, bundles of one active and one terminal item, secrets."""
    # the test user stands in for the hermes user (the Hermes home must be a directory of that user)
    layout = Layout(paths, hermes_home=tmp / "home" / "hermes" / ".hermes",
                    hermes_user=pwd.getpwuid(os.getuid()).pw_name)  # fmt: skip
    h = layout.hermes_home
    for rel, text in {
        "SOUL.md": "soul", "config.yaml": "model: x\n", "memories/MEMORY.md": "the owner likes tea\n",
        "sessions/s1.json": "{}", "skills/git-helper/SKILL.md": "# skill\n", "pending/skills/new/SKILL.md": "# new\n",
        "hermes-agent/run_agent.py": "code", "tools/x": "tool", "cache/c": "cache", "logs/agent.log": "log",
        ".env": "API_SERVER_KEY=test-only-not-secret\n", "gateway.lock": "", "kanban.db.init.lock": "",
    }.items():  # fmt: skip
        f = h / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    sqlite_wal(h / "state.db", 5).close()
    sqlite_wal(h / "ossbot-features" / "nutrition.db", 3).close()
    (layout.skills_root / "staged").mkdir(parents=True, exist_ok=True)
    (layout.skills_root / "decisions.json").write_text('{"git-helper": "sha"}', encoding="utf-8")
    paths.audit_anchors_log.parent.mkdir(parents=True, exist_ok=True)
    paths.audit_anchors_log.touch()
    for cid in (CID_ACTIVE, CID_DONE):
        bundle = paths.artifacts_dir / cid / "1" / "change.bundle"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_bytes(b"bundle " + cid.encode())
    (layout.imports_dir / CID_ACTIVE).mkdir(parents=True, exist_ok=True)
    (layout.imports_dir / CID_ACTIVE / "change.bundle").write_bytes(b"import")
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "gatekeeper.env").write_text("TEST_ONLY=1\n", encoding="utf-8")
    (layout.attachments_dir / "a_0000000000000001").mkdir(parents=True, exist_ok=True)
    return layout


def decrypt_tar(archive: Path) -> tarfile.TarFile:
    data = archive.read_bytes()
    head = fake_age.HEADER
    assert data.startswith(head)
    body = data[len(head) :].partition(b"\n")[2]
    return tarfile.open(fileobj=io.BytesIO(fake_age._xor(body)), mode="r:")


def manifest_of(archive: Path) -> dict:  # type: ignore[type-arg]
    with decrypt_tar(archive) as tar:
        fh = tar.extractfile("MANIFEST.json")
        assert fh is not None
        return json.loads(fh.read())  # type: ignore[no-any-return]


def reencrypt(tar_bytes: bytes, out: Path) -> None:
    out.write_bytes(fake_age.HEADER + json.dumps([RECIPIENT]).encode() + b"\n" + fake_age._xor(tar_bytes))
