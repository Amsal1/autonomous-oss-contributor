"""Filesystem layout (DESIGN §7.3). ``Paths.under(tmp)`` relocates everything for tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = Path("/mnt/d/Work/autonomous-oss-contributor")


@dataclass(frozen=True)
class Paths:
    repo: Path
    srv: Path
    var_lib: Path
    run: Path
    etc: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(
            repo=Path(os.environ.get("OSSBOT_REPO", str(DEFAULT_REPO))),
            srv=Path("/srv/ossbot"),
            var_lib=Path("/var/lib/ossbot"),
            run=Path("/run/ossbot"),
            etc=Path("/etc/ossbot"),
        )

    @classmethod
    def under(cls, root: Path, repo: Path | None = None) -> Paths:
        return cls(
            repo=repo if repo is not None else root / "repo",
            srv=root / "srv",
            var_lib=root / "var_lib",
            run=root / "run",
            etc=root / "etc",
        )

    # repo (drvfs)
    @property
    def config_dir(self) -> Path:
        return self.repo / "config"

    @property
    def kill_marker(self) -> Path:
        return self.repo / ".ossbot-local" / "KILL"

    # gatekeeper
    @property
    def gatekeeper_dir(self) -> Path:
        return self.srv / "gatekeeper"

    @property
    def db_path(self) -> Path:
        return self.gatekeeper_dir / "ossbot.db"

    @property
    def snapshot_root(self) -> Path:
        return self.srv / "config-snapshot"

    @property
    def snapshot_current(self) -> Path:
        return self.snapshot_root / "current"

    @property
    def artifacts_dir(self) -> Path:
        return self.srv / "artifacts"

    # logs
    @property
    def logs_dir(self) -> Path:
        return self.srv / "logs"

    @property
    def resume_log(self) -> Path:
        return self.logs_dir / "resume.log"

    @property
    def audit_anchors_log(self) -> Path:
        return self.logs_dir / "audit-anchors.log"

    # control dir (gatekeeper:osscontrol 0750, files 0640)
    @property
    def control_dir(self) -> Path:
        return self.var_lib / "control"

    @property
    def killed_flag(self) -> Path:
        return self.control_dir / "killed"

    @property
    def paused_flag(self) -> Path:
        return self.control_dir / "paused"

    @property
    def resume_request(self) -> Path:
        return self.control_dir / "resume-request"

    @property
    def lifecycle_flag(self) -> Path:
        return self.control_dir / "lifecycle"

    @property
    def lifecycle_request(self) -> Path:
        return self.control_dir / "lifecycle-request"

    @property
    def shutdown_marker(self) -> Path:
        return self.control_dir / "shutdown-marker"

    @property
    def last_shutdown(self) -> Path:
        return self.control_dir / "last-shutdown"

    # sockets
    @property
    def gatekeeper_sock(self) -> Path:
        return self.run / "gatekeeper.sock"

    @property
    def anchor_sock(self) -> Path:
        return self.run / "anchor.sock"

    @property
    def secrets_dir(self) -> Path:
        return self.etc / "secrets"
