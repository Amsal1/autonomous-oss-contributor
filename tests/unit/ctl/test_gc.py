"""``ossctl gc`` (DESIGN §18.10, plan D1-D6): root-side retention. It deletes only below its roots, never
follows a symlink, never removes a keep-set image, runs fstrim last (not in a dry run) and stops with
exit 3 as soon as a kill or pause flag appears."""

from __future__ import annotations

import json
import os
import socket
import stat
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ossbot.common.config import RetentionPolicy
from ossbot.common.control_flags import FlagState, KillInfo, PauseInfo
from ossbot.common.schemas import RetentionReport
from ossbot.ctl import cli, gc

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "gc" / "dry_run.txt"
SNAP = "0123456789ab" + "c" * 52
DAY = 86400
NOW = time.time()
RUNNING = FlagState(killed=None, paused=None)
PAUSED = FlagState(killed=None, paused=PauseInfo(reason="test", actor="test", ts="t"))
KILLED = FlagState(killed=KillInfo(level="soft", reason="test", actor="test", ts="t"), paused=None)
PIN = "sha256:" + "a" * 64
BASE = "sha256:" + "b" * 64
TAGGED = "sha256:" + "c" * 64
DANGLING = "sha256:" + "d" * 64
IN_USE = "sha256:" + "e" * 64


def _age(path: Path, days: float) -> None:
    t = NOW - days * DAY
    os.utime(path, (t, t), follow_symlinks=False)


def _roots(tmp_path: Path) -> gc.Roots:
    roots = gc.Roots(
        tmp=tmp_path / "tmp", artifacts=tmp_path / "artifacts", run_logs=tmp_path / "runs",
        proxy_logs=tmp_path / "proxy", hermes_sessions=tmp_path / "hermes" / "sessions",
        state_file=tmp_path / "var" / "gc" / "last.json", lock=tmp_path / "versions.lock.yaml",
    )  # fmt: skip
    for d in (roots.tmp, roots.artifacts, roots.run_logs, roots.proxy_logs, roots.hermes_sessions):
        d.mkdir(parents=True)
    roots.lock.write_text(
        f'base_images:\n  ubuntu_24_04: "{BASE}"\nimages:\n  proxy:\n    digest: "{PIN}"\n', encoding="utf-8"
    )
    return roots


class FakeDocker:
    """The docker and fstrim argv the executor runs, answered from a tiny image table."""

    def __init__(self, dangling: dict[str, int] | None = None, in_use: frozenset[str] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.dangling = dict(dangling or {})
        self.in_use = in_use
        self.refs = {"ossbot/proxy:in-1": PIN, f"ossbot/proxy@{PIN}": PIN, "ossbot/sandbox-full:in-2": TAGGED,
                     f"ubuntu@{BASE}": BASE}  # fmt: skip
        self.break_on_rm: str | None = None  # a ref that stops resolving once anything is removed

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        a = list(argv)
        self.calls.append(a)
        if a[:3] == [gc.DOCKER, "image", "ls"]:
            if "--filter" in a:
                return 0, "\n".join([*self.dangling, BASE, PIN]) + "\n"
            return 0, "ossbot/proxy:in-1\nossbot/sandbox-full:in-2\nubuntu:<none>\n<none>:<none>\n"
        if a[:3] == [gc.DOCKER, "image", "inspect"]:
            fmt, ref = a[4], a[5]
            if fmt == "{{.Size}}":
                return 0, f"{self.dangling.get(ref, 0)}\n"
            image = self.refs.get(ref)
            return (0, f"{image}\n") if image else (1, "")
        if a[:3] == [gc.DOCKER, "image", "rm"]:
            if a[3] in self.in_use:
                return 1, ""
            self.dangling.pop(a[3], None)
            if self.break_on_rm:
                self.refs.pop(self.break_on_rm, None)
            return 0, ""
        if a[:3] == [gc.DOCKER, "builder", "prune"]:
            return 0, "ID\tRECLAIMABLE\tSIZE\nabcdefghijklmnopqrstuvwxy\ttrue\t1.5GB\nTotal:\t1.5GB\n"
        if a == gc.FSTRIM_ARGV:
            return 0, "/: 1 GiB (1073741824 bytes) trimmed on /dev/sdd\n"
        raise AssertionError(f"unexpected argv {a}")


def _gk(eligible: Sequence[str] = (), calls: list[bool] | None = None) -> gc.GkCall:
    def call(dry_run: bool) -> RetentionReport:
        if calls is not None:
            calls.append(dry_run)
        return RetentionReport(
            dry_run=dry_run, snapshot_hash="f" * 64,
            counts={"contributions": 1, "tg_outbox": 30, "tg_views": 6, "ui_tokens": 3, "mirrors": 1, "derived": 1,
                    "imports": 0, "work": 0, "cache": 0},
            bytes={"mirrors": 1000, "derived": 24, "imports": 0, "work": 0, "cache": 0},
            eligible_cids=list(eligible),
        )  # fmt: skip

    return call


def _unreachable(dry_run: bool) -> RetentionReport:
    raise gc.GatekeeperUnreachable("no socket")


def _run(roots: gc.Roots, stores: Sequence[str] = gc.DEFAULT_STORES, *, dry_run: bool = False,
         docker: FakeDocker | None = None, gk: gc.GkCall | None = None,
         flags: Callable[[], FlagState] | None = None, hours: int | None = None) -> gc.Report:  # fmt: skip
    reader = flags or (lambda: RUNNING)
    return gc.run_gc(
        stores, dry_run, RetentionPolicy(), SNAP, gk_call=gk or _gk(), runner=docker or FakeDocker(),
        flags_reader=reader, clock=lambda: NOW, roots=roots, build_cache_hours=hours,
    )  # fmt: skip


def _store(report: gc.Report, name: str) -> gc.StoreResult:
    return next(s for s in report.stores if s.name == name)


# ---------------------------------------------------------------------------------- /tmp
def test_tmp_selection_skips_private_recent_sockets_and_symlinks(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    t = roots.tmp
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    (t / "old.txt").write_text("old")
    (t / "olddir").mkdir()
    (t / "olddir" / "f").write_text("12345")
    (t / "mixed").mkdir()
    (t / "mixed" / "fresh").write_text("new")
    (t / "mixed" / "stale").write_text("old")
    (t / "new.txt").write_text("new")
    (t / "systemd-private-abc-ossbot-gatekeeper.service-x").mkdir()
    (t / ".X11-unix").mkdir()
    os.mkfifo(t / "fifo")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(t / "sock"))
    (t / "link").symlink_to(outside)
    for p in (t / "olddir" / "f", t / "olddir", t / "old.txt", t / "mixed" / "stale", t / "mixed", t / "fifo",
              t / "sock", t / "systemd-private-abc-ossbot-gatekeeper.service-x", t / ".X11-unix", t / "link",
              outside):  # fmt: skip
        _age(p, 10)
    try:
        assert [e.name for e in gc.select_tmp(t, 7, NOW)] == ["old.txt", "olddir"]
        assert {e.name: e.bytes for e in gc.select_tmp(t, 7, NOW)}["old.txt"] == 3
        report = _run(roots, ["tmp"])
    finally:
        sock.close()
    assert _store(report, "tmp").status == "DELETED" and _store(report, "tmp").count == 2
    assert not (t / "old.txt").exists() and not (t / "olddir").exists()
    left = sorted(p.name for p in t.iterdir())
    assert left == [".X11-unix", "fifo", "link", "mixed", "new.txt", "sock",
                    "systemd-private-abc-ossbot-gatekeeper.service-x"]  # fmt: skip
    assert (outside / "keep.txt").exists()  # the symlink's target is never touched


def test_symlinked_store_root_fails_and_follows_nothing(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "request_dump_x.json").write_text("{}")
    _age(victim / "request_dump_x.json", 400)
    roots.hermes_sessions.rmdir()
    roots.hermes_sessions.symlink_to(victim)
    report = _run(roots, ["hermes-sessions"])
    assert _store(report, "hermes-sessions").status == "FAIL"
    assert (victim / "request_dump_x.json").exists()


# --------------------------------------------------------------------------------- files
def test_run_logs_by_mtime(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    r = roots.run_logs
    for name in ("old.log", "new.log", "old.txt"):
        (r / name).write_text("log")
    (r / "dir.log").mkdir()
    for name in ("old.log", "old.txt", "dir.log"):
        _age(r / name, 31)
    _age(r / "new.log", 29)
    assert [e.name for e in gc.select_run_logs(r, 30, NOW)] == ["old.log"]
    report = _run(roots, ["run-logs"])
    assert _store(report, "run-logs").count == 1
    assert sorted(p.name for p in r.iterdir()) == ["dir.log", "new.log", "old.txt"]


def test_run_logs_include_old_egress_summaries(tmp_path: Path) -> None:
    """Open-discovery §13.2 (review-2 finding 7): old ``*.egress.json`` next to old ``*.log``, nothing else."""
    r = _roots(tmp_path).run_logs
    for name in ("r1.log", "r1.egress.json", "r2.egress.json", "r3.egress.jsonl", "r4.json", "egress.json.bak"):
        (r / name).write_text("{}")
        _age(r / name, 31)
    _age(r / "r2.egress.json", 29)
    assert [e.name for e in gc.select_run_logs(r, 30, NOW)] == ["r1.egress.json", "r1.log"]


def test_proxy_logs_keep_recent_rotations_and_never_the_live_file(tmp_path: Path) -> None:
    """Open-discovery §13.5: rotated ``access.log.<stamp>`` older than ``proxy_logs_days`` (default 30)."""
    roots = _roots(tmp_path)
    p = roots.proxy_logs
    names = ("access.log", "access.log.20260901T001000Z", "access.log.20261020T001000Z", "cache.log",
             "cache.log.20260901T001000Z")  # fmt: skip
    for name in names:
        (p / name).write_text("line\n")
        _age(p / name, 400)
    _age(p / "access.log.20261020T001000Z", 29)
    (p / "access.log.dir").mkdir()
    _age(p / "access.log.dir", 400)
    assert RetentionPolicy().proxy_logs_days == 30
    assert [e.name for e in gc.select_proxy_logs(p, 30, NOW)] == ["access.log.20260901T001000Z"]
    report = _run(roots, ["proxy-logs"])
    assert _store(report, "proxy-logs").count == 1
    assert sorted(x.name for x in p.iterdir()) == ["access.log", "access.log.20261020T001000Z", "access.log.dir",
                                                   "cache.log", "cache.log.20260901T001000Z"]  # fmt: skip


def test_hermes_sessions_only_regular_files_in_sessions_dir(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    s = roots.hermes_sessions
    state_db = s.parent / "state.db"
    state_db.write_text("db")
    for name in ("request_dump_s1_1.json", "session_s1.json", "sessions.json", "s2.jsonl", "fresh.json"):
        (s / name).write_text("{}")
    (s / "sub").mkdir()
    (s / "sub" / "old.json").write_text("{}")
    (s / "link.json").symlink_to(state_db)
    for p in (s / "request_dump_s1_1.json", s / "session_s1.json", s / "sessions.json", s / "s2.jsonl",
              s / "sub" / "old.json", s / "sub", s / "link.json", state_db):  # fmt: skip
        _age(p, 91)
    _age(s / "fresh.json", 30)
    assert [e.name for e in gc.select_hermes_sessions(s, 90, NOW)] == ["request_dump_s1_1.json", "session_s1.json"]
    report = _run(roots, ["hermes-sessions"])
    hermes = _store(report, "hermes-sessions")
    assert hermes.status == "DELETED" and hermes.count == 2 and hermes.items == []  # names never reported
    assert sorted(p.name for p in s.iterdir()) == ["fresh.json", "link.json", "s2.jsonl", "sessions.json", "sub"]
    assert state_db.exists() and (s / "sub" / "old.json").exists()


def test_artifacts_only_eligible_cids_and_contained_dirs(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    a = roots.artifacts
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x").write_text("x")
    for cid in ("c_AAAAAAAAAA", "c_BBBBBBBBBB"):
        (a / cid / "1").mkdir(parents=True)
        (a / cid / "1" / "bundle").write_text("12345678")
    (a / "c_CCCCCCCCCC").symlink_to(outside)
    (a / "c_DDDDDDDDDD").write_text("a file")
    eligible = ["c_AAAAAAAAAA", "c_CCCCCCCCCC", "c_DDDDDDDDDD"]
    selected = gc.select_artifacts(a, [*eligible, "../elsewhere", "c_aaaaaaaaaa", "sys-chat", "c_AAAAAAAAAA/1"])
    assert [e.name for e in selected] == ["c_AAAAAAAAAA"]
    calls: list[bool] = []
    bad = _run(roots, ["gatekeeper", "artifacts"], gk=_gk(["../elsewhere"], calls))  # outside the schema
    assert _store(bad, "gatekeeper").status == "FAIL" and _store(bad, "artifacts").status == "SKIP"
    assert calls == [False]  # asked once, never again for the artifacts
    report = _run(roots, ["gatekeeper", "artifacts"], gk=_gk(eligible))
    assert _store(report, "artifacts").count == 1
    assert sorted(p.name for p in a.iterdir()) == ["c_BBBBBBBBBB", "c_CCCCCCCCCC", "c_DDDDDDDDDD"]
    assert (outside / "x").exists()


def test_artifacts_alone_ask_the_gatekeeper_in_dry_run_mode(tmp_path: Path) -> None:
    calls: list[bool] = []
    _run(_roots(tmp_path), ["artifacts"], gk=_gk(calls=calls))
    assert calls == [True]  # eligibility only: no gatekeeper deletion without --only gatekeeper


# -------------------------------------------------------------------------------- docker
def test_dangling_images_respect_keep_set(tmp_path: Path) -> None:
    docker = FakeDocker(dangling={DANGLING: 5000, IN_USE: 7})
    _run(_roots(tmp_path), ["images"], docker=docker)
    removed = [c[3] for c in docker.calls if c[:3] == [gc.DOCKER, "image", "rm"]]
    assert removed == [DANGLING, IN_USE]
    assert BASE not in removed and PIN not in removed  # dangling-looking base and pinned images are kept
    docker2 = FakeDocker(dangling={DANGLING: 5000, IN_USE: 7}, in_use=frozenset({IN_USE}))
    report = _run(_roots(tmp_path / "2"), ["images"], docker=docker2)
    images = _store(report, "images")
    assert images.status == "DELETED" and images.count == 1 and images.bytes == 5000
    assert "1 refused (in use)" in images.detail and "keep set 4 refs intact" in images.detail
    assert not any("--force" in c or "prune" in c for c in docker2.calls if c[:2] == [gc.DOCKER, "image"])


def test_keep_set_postcheck_fails_loudly(tmp_path: Path) -> None:
    docker = FakeDocker(dangling={DANGLING: 1})
    docker.break_on_rm = "ossbot/sandbox-full:in-2"
    report = _run(_roots(tmp_path), ["images"], docker=docker)
    images = _store(report, "images")
    assert images.status == "FAIL" and "ossbot/sandbox-full:in-2" in images.detail
    assert report.exit_code == gc.EXIT_FAIL


def test_builder_prune_argv() -> None:
    assert gc.builder_prune_argv(168) == [
        "/usr/bin/docker", "builder", "prune", "--all", "--force", "--filter", "until=168h"
    ]  # fmt: skip


def test_builder_prune_one_off_has_no_until(tmp_path: Path) -> None:
    assert gc.builder_prune_argv(0) == ["/usr/bin/docker", "builder", "prune", "--all", "--force"]
    docker = FakeDocker()
    report = _run(_roots(tmp_path), ["build-cache"], docker=docker, hours=0)
    assert docker.calls == [gc.builder_prune_argv(0)]
    cache = _store(report, "build-cache")
    assert cache.status == "DELETED" and cache.count == 1 and cache.bytes == 1_500_000_000


# --------------------------------------------------------------------------- run control
def test_fstrim_last_and_skipped_in_dry_run(tmp_path: Path) -> None:
    docker = FakeDocker()
    report = _run(_roots(tmp_path), gc.STORES, docker=docker)
    assert docker.calls[-1] == gc.FSTRIM_ARGV and _store(report, "fstrim").status == "DONE"
    assert [s.name for s in report.stores][-1] == "fstrim"
    dry = FakeDocker()
    report = _run(_roots(tmp_path / "dry"), gc.STORES, docker=dry, dry_run=True)
    assert gc.FSTRIM_ARGV not in dry.calls and _store(report, "fstrim").status == "SKIP"
    assert not any(c[:3] in ([gc.DOCKER, "image", "rm"], [gc.DOCKER, "builder", "prune"]) for c in dry.calls)


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    (roots.tmp / "old").write_text("x")
    (roots.run_logs / "a.log").write_text("x")
    (roots.artifacts / "c_AAAAAAAAAA").mkdir()
    for p in (roots.tmp / "old", roots.run_logs / "a.log"):
        _age(p, 400)
    calls: list[bool] = []
    report = _run(roots, gc.STORES, dry_run=True, gk=_gk(["c_AAAAAAAAAA"], calls))
    assert calls == [True]
    assert (roots.tmp / "old").exists() and (roots.run_logs / "a.log").exists()
    assert (roots.artifacts / "c_AAAAAAAAAA").exists()
    assert {s.name: s.status for s in report.deleting()} == {
        "tmp": "WOULD DELETE", "gatekeeper": "WOULD DELETE", "artifacts": "WOULD DELETE", "run-logs": "WOULD DELETE",
        "proxy-logs": "WOULD DELETE", "hermes-sessions": "WOULD DELETE", "images": "WOULD DELETE",
    }  # fmt: skip


@pytest.mark.parametrize("flag", [PAUSED, KILLED], ids=["paused", "killed"])
def test_stops_when_flag_appears(tmp_path: Path, flag: FlagState) -> None:
    roots = _roots(tmp_path)
    (roots.run_logs / "a.log").write_text("x")
    _age(roots.run_logs / "a.log", 400)
    reads = iter([RUNNING, RUNNING])
    docker = FakeDocker()
    report = _run(roots, docker=docker, flags=lambda: next(reads, flag))
    assert report.stopped == flag.state and report.exit_code == gc.EXIT_STOPPED
    assert [s.name for s in report.stores] == ["tmp", "gatekeeper"]  # nothing after the flag appeared
    assert (roots.run_logs / "a.log").exists() and docker.calls == []
    assert "STOPPED" in gc.render_text(report)
    assert json.loads(roots.state_file.read_text())["stopped"] == flag.state
    report = _run(_roots(tmp_path / "2"), ["tmp"], flags=lambda: flag)
    assert report.exit_code == gc.EXIT_STOPPED and report.stores == []


def test_non_root_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_gc must not run")

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(gc, "run_gc", boom)
    res = CliRunner().invoke(cli.app, ["gc", "--dry-run"])
    assert res.exit_code == 2 and "runs as root" in res.output


def test_cli_rejects_unknown_store_and_missing_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert CliRunner().invoke(cli.app, ["gc", "--only", "audit"]).exit_code == 2
    with pytest.raises(gc.GcConfigError):
        gc.load_policy(tmp_path / "no-snapshot")


def test_gatekeeper_unreachable_skips_gatekeeper_and_artifacts(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    (roots.tmp / "old").write_text("x")
    _age(roots.tmp / "old", 30)
    report = _run(roots, gk=_unreachable)
    assert _store(report, "gatekeeper").status == "SKIP" and "unreachable" in _store(report, "gatekeeper").detail
    assert _store(report, "artifacts").status == "SKIP"
    assert _store(report, "tmp").status == "DELETED" and _store(report, "images").status == "DELETED"
    assert report.exit_code == gc.EXIT_OK


# -------------------------------------------------------------------------------- output
def _example() -> gc.Report:
    mib = 1024 * 1024
    return gc.Report(dry_run=True, snapshot=SNAP, ts="2026-10-21T04:30:00Z", stores=[
        gc.StoreResult("tmp", "WOULD DELETE", 3, round(812.4 * mib), "older than 7 d"),
        gc.StoreResult("gatekeeper", "WOULD DELETE", 41, 12 * mib, "outbox 30, views 6, tokens 3, dirs 2"),
        gc.StoreResult("artifacts", "WOULD DELETE", 2, round(40.1 * mib), "terminal >= 30 d"),
        gc.StoreResult("run-logs", "WOULD DELETE", 5, round(1.2 * mib), "older than 30 d"),
        gc.StoreResult("proxy-logs", "WOULD DELETE", 1, round(3.5 * mib), "rotated, older than 30 d"),
        gc.StoreResult("hermes-sessions", "WOULD DELETE", 0, 0, "idle >= 90 d"),
        gc.StoreResult("images", "WOULD DELETE", 1, 310 * mib, "dangling, keep set 6 refs intact"),
        gc.StoreResult("build-cache", "SKIP", detail=gc.WEEKLY),
        gc.StoreResult("fstrim", "SKIP", detail="dry run"),
    ])  # fmt: skip


def test_dry_run_report_text() -> None:
    assert gc.render_text(_example()) == FIXTURE.read_text(encoding="utf-8")


def test_json_report_shape() -> None:
    doc = json.loads(gc.render_json(_example()))
    assert set(doc) == {"dry_run", "snapshot", "ts", "stopped", "exit_code", "stores", "total"}
    assert list(doc["stores"]) == sorted(gc.STORES)
    assert set(doc["stores"]["tmp"]) == {"status", "count", "bytes", "detail", "items"}
    assert doc["total"]["count"] == 53 and doc["exit_code"] == 0 and doc["stopped"] is None


def test_last_json_written(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _run(roots, ["tmp"], dry_run=True)
    doc = json.loads(roots.state_file.read_text(encoding="utf-8"))
    assert doc["dry_run"] is True and doc["stores"]["tmp"] == {"count": 0, "bytes": 0, "status": "WOULD DELETE"}
    assert stat.S_IMODE(roots.state_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(roots.state_file.parent.stat().st_mode) == 0o755
    assert not [p for p in roots.state_file.parent.iterdir() if p.name != "last.json"]  # no temp left
