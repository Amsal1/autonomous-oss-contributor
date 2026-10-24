"""``ossctl gc``: root-side retention and housekeeping (DESIGN §18.10, ADR-0016 addendum post-v1).

Who deletes what (plan D1): the gatekeeper deletes its own DB rows and repos itself, triggered here
through the operator-only ``POST /v1/retention/run``. Root deletes the root-reachable stores: ``/tmp``
leftovers, the artifacts of the cids the gatekeeper reports eligible, sandbox run logs and egress summaries,
rotated squid access logs (``retention.proxy_logs_days``, never the live ``access.log``), idle files in
Hermes' ``sessions/`` dir, dangling Docker images outside the keep set, unused build cache (weekly) and
``fstrim`` last. Never touched: the audit log and its anchors, the control dir, secrets, the gatekeeper's
own dirs, Hermes' ``state.db``/memories/skills, anything open or awaiting approval.

Safety: every store root is opened component by component with ``O_NOFOLLOW`` (a symlink anywhere in
the path fails the store), entries are one name below that dir fd, lstat'ed and deleted relative to it
(``shutil.rmtree(dir_fd=...)`` is the fd-based implementation that never follows a link), so the owner
of a store (hermes, ossbox, any ``/tmp`` user) cannot steer a deletion outside it. Kill and pause are
re-read before every store and every deletion (exit 3). Docker runs as argv lists on the root socket
(like ``scripts/lib/images.sh``); images go one by one with ``docker image rm`` (no ``--force``) and every
keep-set ref must still resolve afterwards. Logs carry store names, counts and bytes only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ossbot.common.config import PolicyConfig, RetentionPolicy
from ossbot.common.control_flags import FlagState
from ossbot.common.images import load_image_pins
from ossbot.common.schemas import RetentionReport
from ossbot.common.snapshot import read_current
from ossbot.common.yamlio import YamlError, safe_load

log = logging.getLogger(__name__)

Status = Literal["WOULD DELETE", "DELETED", "DONE", "SKIP", "KEEP", "FAIL"]
STORES: tuple[str, ...] = (
    "tmp", "gatekeeper", "artifacts", "run-logs", "proxy-logs", "hermes-sessions", "images", "build-cache",
    "fstrim",
)  # fmt: skip
PROXY_LOG_LIVE = "access.log"  # squid writes it: never selected (rotated ones are access.log.<UTC stamp>)
FLOOR_LOG_LIVE = "floor.log"  # ossbot-floorlog writes it (ADR-0025): rotated ones are floor.log.<UTC stamp>
DEFAULT_STORES: tuple[str, ...] = tuple(s for s in STORES if s != "build-cache")  # build cache: weekly unit
EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_STOPPED = 0, 1, 2, 3
DOCKER = "/usr/bin/docker"
FSTRIM_ARGV = ["/usr/sbin/fstrim", "-v", "/"]
CID_RE = re.compile(r"^c_[0-9A-Z]{10}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# /tmp entries that belong to running services or the session (V10: PrivateTmp dirs are systemd-private-*).
TMP_SKIP_NAMES = frozenset({".X11-unix", ".ICE-unix", ".XIM-unix", ".font-unix", ".Test-unix", "snap-private-tmp"})
TMP_SKIP_PREFIXES = ("systemd-private-",)
# V5 (Hermes v0.22.2): sessions/ holds request_dump_* breadcrumbs and legacy per-session snapshots; the
# sessions themselves live in state.db. Kept: the legacy routing mirror and the divert transcripts
# (messages written there only when state.db was replaced under a live process; recovery data).
HERMES_KEEP_NAMES = frozenset({"sessions.json"})
HERMES_KEEP_SUFFIXES = (".jsonl",)
MAX_WALK = 100_000  # entries inspected below one top-level entry; more counts as recent (kept)
DOCKER_TIMEOUT_S = 120.0
PRUNE_TIMEOUT_S = 1500.0  # < the units' TimeoutStartSec=1800
DAY_S = 86400
KEEP_LINE = "audit log, open contributions, pending approvals: never selected"
WEEKLY = "weekly (ossctl gc --only build-cache)"
ITEMS_MAX = 200  # names listed per store in --json (never for hermes-sessions)
UNITS: dict[str, tuple[str, str]] = {
    "tmp": ("entry", "entries"), "gatekeeper": ("rows/dirs", "rows/dirs"), "artifacts": ("dir", "dirs"),
    "run-logs": ("file", "files"), "proxy-logs": ("file", "files"), "hermes-sessions": ("file", "files"),
    "images": ("image", "images"),
    "build-cache": ("record", "records"), "fstrim": ("", ""),
}  # fmt: skip

Runner = Callable[[Sequence[str], float], tuple[int, str]]


class GcConfigError(Exception):
    """No usable retention policy: nothing is deleted (exit 2)."""


class GatekeeperUnreachable(Exception):
    """The gatekeeper socket did not answer: gatekeeper and artifacts are skipped."""


class GatekeeperRefused(Exception):
    """The gatekeeper answered, but not 200 (e.g. 409 while killed or paused)."""


GkCall = Callable[[bool], RetentionReport]


@dataclass(frozen=True)
class Roots:
    tmp: Path = Path("/tmp")  # noqa: S108 - the store itself
    artifacts: Path = Path("/srv/ossbot/artifacts")
    run_logs: Path = Path("/srv/ossbot/logs/runs")
    proxy_logs: Path = Path("/srv/ossbot/logs/proxy")  # open-discovery §13.5: rotated squid access logs
    floor_logs: Path = Path("/srv/ossbot/logs/floor")  # build-env §4.4: rotated kernel floor logs, same retention
    hermes_sessions: Path = Path("/home/hermes/.hermes/sessions")
    state_file: Path = Path("/var/lib/ossbot/gc/last.json")
    lock: Path = Path("/etc/ossbot/versions.lock.yaml")


@dataclass(frozen=True)
class Entry:
    name: str  # one name below the store root (an image id for images)
    bytes: int


@dataclass
class StoreResult:
    name: str
    status: Status
    count: int = 0
    bytes: int = 0
    detail: str = ""
    items: list[str] = field(default_factory=list)  # --json only; never Hermes file names


@dataclass
class Report:
    dry_run: bool
    snapshot: str
    ts: str
    stores: list[StoreResult] = field(default_factory=list)
    stopped: str | None = None  # "killed" | "paused" when a flag stopped the run

    @property
    def exit_code(self) -> int:
        if self.stopped is not None:
            return EXIT_STOPPED
        return EXIT_FAIL if any(s.status == "FAIL" for s in self.stores) else EXIT_OK

    def deleting(self) -> list[StoreResult]:
        return [s for s in self.stores if s.status in ("WOULD DELETE", "DELETED")]


# ------------------------------------------------------------------------------- config
def load_policy(snapshot_root: Path) -> tuple[str, RetentionPolicy]:
    """``retention`` of the current, hash-verified config snapshot (never drvfs, §7.9). Fail closed."""
    try:
        digest, files = read_current(snapshot_root)
        policy = PolicyConfig.model_validate(safe_load(files["policy.yaml"]))
    except (OSError, KeyError, ValueError, YamlError) as exc:  # pydantic ValidationError is a ValueError
        raise GcConfigError(type(exc).__name__) from exc
    return digest, policy.retention


# ----------------------------------------------------------------------- safe dir access
def open_dir_nofollow(path: Path) -> int:
    """A dir fd for ``path``, opened from ``/`` one component at a time with ``O_NOFOLLOW``: a symlink
    anywhere in the path raises OSError (ELOOP/ENOTDIR) instead of being followed."""
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise OSError(f"not a plain absolute path: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _dir(path: Path) -> Iterator[int]:
    fd = open_dir_nofollow(path)
    try:
        yield fd
    finally:
        os.close(fd)


def _tree(dir_fd: int, name: str, cutoff: float | None) -> tuple[bool, int]:
    """(own and every descendant's mtime < cutoff, bytes) of ``name`` below ``dir_fd``; links are never
    followed. With ``cutoff=None`` only the size counts. More than MAX_WALK entries = recent."""
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    total = st.st_size
    if cutoff is not None and st.st_mtime >= cutoff:
        return False, total
    if not stat.S_ISDIR(st.st_mode):
        return True, total
    seen = 0
    for _top, dirs, files, top_fd in os.fwalk(name, dir_fd=dir_fd, follow_symlinks=False):
        for child in (*dirs, *files):
            seen += 1
            if seen > MAX_WALK:
                return cutoff is None, total
            try:
                cst = os.stat(child, dir_fd=top_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if cutoff is not None and cst.st_mtime >= cutoff:
                return False, total
            total += cst.st_size
    return True, total


# ---------------------------------------------------------------------------- selectors
def select_tmp(root: Path, days: int, now: float) -> list[Entry]:
    """Top-level entries (regular files and dirs only) whose own and every descendant's mtime is older
    than ``days``; private service dirs, X11/ICE sockets dirs, sockets, FIFOs and symlinks are skipped."""
    cutoff = now - days * DAY_S
    out: list[Entry] = []
    with _dir(root) as fd:
        for name in sorted(os.listdir(fd)):
            if name in TMP_SKIP_NAMES or name.startswith(TMP_SKIP_PREFIXES):
                continue
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    continue
                old, size = _tree(fd, name, cutoff)
            except OSError:
                continue
            if old:
                out.append(Entry(name, size))
    return out


def _select_files(root: Path, days: int, now: float, accept: Callable[[str], bool]) -> list[Entry]:
    cutoff = now - days * DAY_S
    out: list[Entry] = []
    with _dir(root) as fd:
        for name in sorted(os.listdir(fd)):
            if not accept(name):
                continue
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                out.append(Entry(name, st.st_size))
    return out


def select_run_logs(root: Path, days: int, now: float) -> list[Entry]:
    """``/srv/ossbot/logs/runs/*.log`` and the egress meter's ``*.egress.json`` summaries (open-discovery
    §13.2): regular files older than ``days``."""
    return _select_files(root, days, now, lambda name: name.endswith((".log", ".egress.json")))


def select_proxy_logs(root: Path, days: int, now: float) -> list[Entry]:
    """``/srv/ossbot/logs/proxy/access.log.*`` (renamed by ``ossbot-proxy-rotate``): regular files older than
    ``days`` (``retention.proxy_logs_days``, >= 30); never the live ``access.log`` squid writes."""
    return _select_files(root, days, now, lambda name: name.startswith(PROXY_LOG_LIVE + "."))


def select_floor_logs(root: Path, days: int, now: float) -> list[Entry]:
    """``/srv/ossbot/logs/floor/floor.log.*`` (renamed by ``ossbot-floorlog`` at 64 MiB): regular files older
    than ``days`` (the proxy-log retention); never the live ``floor.log``."""
    return _select_files(root, days, now, lambda name: name.startswith(FLOOR_LOG_LIVE + "."))


def select_hermes_sessions(root: Path, days: int, now: float) -> list[Entry]:
    """Regular files directly in Hermes' ``sessions/`` idle for ``days`` (V5); never ``sessions.json``,
    ``*.jsonl``, subdirs or anything outside the dir (``state.db`` is the session store)."""

    def accept(name: str) -> bool:
        return name not in HERMES_KEEP_NAMES and not name.endswith(HERMES_KEEP_SUFFIXES)

    return _select_files(root, days, now, accept)


def select_artifacts(root: Path, cids: Iterable[str]) -> list[Entry]:
    """``<root>/<cid>`` for the eligible cids the gatekeeper named (plan D2): the cid must match CID_RE
    (one plain name, so contained by construction) and be a real dir, not a symlink."""
    out: list[Entry] = []
    with _dir(root) as fd:
        for cid in sorted(set(cids)):
            if not CID_RE.fullmatch(cid):
                continue
            try:
                st = os.stat(cid, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode):
                    continue
                _, size = _tree(fd, cid, None)
            except OSError:
                continue
            out.append(Entry(cid, size))
    return out


def _delete(root: Path, entries: Sequence[Entry], still_old: Callable[[int, str], bool],
            stop: Callable[[], bool]) -> tuple[int, int, list[str]]:  # fmt: skip
    """Delete each entry relative to the root's fd (re-checked first); (deleted, bytes, error names)."""
    done, freed, errors = 0, 0, []
    with _dir(root) as fd:
        for entry in entries:
            if stop():
                break
            try:
                st = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if not still_old(fd, entry.name):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(entry.name, dir_fd=fd)
                elif stat.S_ISREG(st.st_mode):
                    os.unlink(entry.name, dir_fd=fd)
                else:
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(type(exc).__name__)
                continue
            done += 1
            freed += entry.bytes
    return done, freed, errors


# ------------------------------------------------------------------------------- docker
def run_argv(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """(exit code, stdout) of an argv list; never a shell."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, type(exc).__name__
    return proc.returncode, proc.stdout


@dataclass(frozen=True)
class KeepSet:
    refs: dict[str, str]  # keep ref -> the image id it resolves to before anything is removed
    ids: frozenset[str]


def _image_id(runner: Runner, ref: str) -> str | None:
    code, out = runner([DOCKER, "image", "inspect", "--format", "{{.Id}}", ref], DOCKER_TIMEOUT_S)
    value = out.strip()
    return value if code == 0 and IMAGE_ID_RE.fullmatch(value) else None


def base_image_digest(lock_path: Path) -> str | None:
    """``base_images.ubuntu_24_04`` of the root-owned lock copy (``sha256:<hex>``), or None."""
    try:
        data = safe_load(lock_path.read_bytes())
    except (OSError, YamlError, UnicodeDecodeError):
        return None
    base = data.get("base_images") if isinstance(data, dict) else None
    value = base.get("ubuntu_24_04") if isinstance(base, dict) else None
    return value if isinstance(value, str) and IMAGE_ID_RE.fullmatch(value) else None


def docker_keep_set(runner: Runner, lock_path: Path) -> KeepSet:
    """Every ``ossbot/*`` tag, the ``images:`` pins and ``ubuntu@<base_images.ubuntu_24_04>`` (plan D4)."""
    code, out = runner([DOCKER, "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}}"], DOCKER_TIMEOUT_S)
    if code != 0:
        raise OSError("docker image ls failed")
    refs: set[str] = set()
    for line in out.splitlines():
        repo, _, tag = line.strip().rpartition(":")
        if repo.startswith("ossbot/") and tag and tag != "<none>":
            refs.add(f"{repo}:{tag}")
    pins = load_image_pins(lock_path)
    refs.update(ref for name in pins.digests if (ref := pins.ref(name)) is not None)
    ids = {f"sha256:{digest}" for digest in pins.digests.values()}
    base = base_image_digest(lock_path)
    if base is not None:
        refs.add(f"ubuntu@{base}")
        ids.add(base)
    resolved = {ref: image for ref in sorted(refs) if (image := _image_id(runner, ref)) is not None}
    return KeepSet(refs=resolved, ids=frozenset(ids | set(resolved.values())))


def select_dangling(runner: Runner, keep: KeepSet) -> list[Entry]:
    """Dangling image ids minus the keep set, with their sizes."""
    code, out = runner([DOCKER, "image", "ls", "--filter", "dangling=true", "--no-trunc", "-q"], DOCKER_TIMEOUT_S)
    if code != 0:
        raise OSError("docker image ls --filter dangling=true failed")
    out_entries: list[Entry] = []
    for image in sorted({line.strip() for line in out.splitlines() if IMAGE_ID_RE.fullmatch(line.strip())}):
        if image in keep.ids:
            continue
        code, size = runner([DOCKER, "image", "inspect", "--format", "{{.Size}}", image], DOCKER_TIMEOUT_S)
        out_entries.append(Entry(image, int(size.strip()) if code == 0 and size.strip().isdigit() else 0))
    return out_entries


def builder_prune_argv(hours: int) -> list[str]:
    """Unused build cache older than ``hours`` (V1: ``until`` keeps records used in that window; ``--all``
    includes internal/frontend images; in-use records are never reclaimable). 0 = every unused record."""
    argv = [DOCKER, "builder", "prune", "--all", "--force"]
    return [*argv, "--filter", f"until={hours}h"] if hours else argv


_SI = {"B": 1, "kB": 10**3, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}


def _prune_summary(out: str) -> tuple[int, int]:
    """(records, bytes) from ``docker builder prune`` output (rows of ids, then ``Total: 1.2GB``)."""
    records = sum(1 for line in out.splitlines() if re.match(r"^[a-z0-9]{20,}\*?\s", line))
    match = re.search(r"Total:\s*([0-9.]+)\s*([kKMGT]?B)", out)
    freed = int(float(match.group(1)) * _SI[match.group(2)]) if match else 0
    return records, freed


# ------------------------------------------------------------------------------ executor
class _Stop:
    """Re-reads the kill and pause flags; remembers the state that stopped the run."""

    def __init__(self, flags_reader: Callable[[], FlagState]) -> None:
        self.flags_reader = flags_reader
        self.state: str | None = None

    def __call__(self) -> bool:
        if self.state is None:
            flags = self.flags_reader()
            if not flags.running:
                self.state = flags.state
        return self.state is not None


def human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            break
    return f"{value:.1f} {unit}"


def _now_iso(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_gc(
    stores: Sequence[str], dry_run: bool, policy: RetentionPolicy, snapshot: str, *, gk_call: GkCall,
    runner: Runner, flags_reader: Callable[[], FlagState], clock: Callable[[], float], roots: Roots,
    build_cache_hours: int | None = None,
) -> Report:  # fmt: skip
    """Run the selected stores in the fixed order (fstrim last), write ``last.json`` and return the report."""
    now = clock()
    report = Report(dry_run=dry_run, snapshot=snapshot, ts=_now_iso(now))
    stop = _Stop(flags_reader)
    selected = set(stores)
    gk: dict[str, Any] = {}  # "report": RetentionReport | "skip": reason

    def gatekeeper_report(want_dry: bool) -> RetentionReport | None:
        if "report" not in gk and "skip" not in gk:
            try:
                gk["report"] = gk_call(want_dry)
            except GatekeeperUnreachable:
                gk["skip"] = "gatekeeper unreachable"
            except GatekeeperRefused as exc:
                gk["skip"] = f"gatekeeper refused ({exc})"
                gk["fail"] = True
            except ValueError as exc:  # a report outside the schema (pydantic ValidationError)
                gk["skip"] = f"gatekeeper report invalid ({type(exc).__name__})"
                gk["fail"] = True
        return gk.get("report")  # asked at most once per run

    def files_store(name: str, entries: list[Entry], root: Path, detail: str, still_old: Callable[[int, str], bool],
                    listed: bool = True) -> StoreResult:  # fmt: skip
        items = [e.name for e in entries][:ITEMS_MAX] if listed else []
        if dry_run:
            return StoreResult(name, "WOULD DELETE", len(entries), sum(e.bytes for e in entries), detail, items)
        done, freed, errors = _delete(root, entries, still_old, stop)
        if errors:
            return StoreResult(name, "FAIL", done, freed, f"{detail}; {len(errors)} not deleted ({errors[0]})", items)
        return StoreResult(name, "DELETED", done, freed, detail, items)

    def mtime_old(cutoff: float) -> Callable[[int, str], bool]:
        return lambda fd, name: os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime < cutoff

    def do_tmp() -> StoreResult:
        cutoff = now - policy.tmp_days * DAY_S
        entries = select_tmp(roots.tmp, policy.tmp_days, now)
        return files_store("tmp", entries, roots.tmp, f"older than {policy.tmp_days} d",
                           lambda fd, name: _tree(fd, name, cutoff)[0])  # fmt: skip

    def do_gatekeeper() -> StoreResult:
        got = gatekeeper_report(dry_run)
        if got is None:
            return StoreResult("gatekeeper", "FAIL" if gk.get("fail") else "SKIP", detail=str(gk["skip"]))
        c = got.counts
        dirs = sum(c.get(k, 0) for k in ("mirrors", "derived", "imports", "work", "cache"))
        outbox, views, tokens = c.get("tg_outbox", 0), c.get("tg_views", 0), c.get("ui_tokens", 0)
        detail = f"outbox {outbox}, views {views}, tokens {tokens}, dirs {dirs}"
        status: Status = "WOULD DELETE" if got.dry_run else "DELETED"
        return StoreResult("gatekeeper", status, outbox + views + tokens + dirs, sum(got.bytes.values()), detail)

    def do_artifacts() -> StoreResult:
        # Eligibility always comes from the gatekeeper; alone, artifacts asks it in dry-run mode.
        got = gatekeeper_report(dry_run if "gatekeeper" in selected else True)
        if got is None:
            return StoreResult("artifacts", "SKIP", detail=f"{gk['skip']}: no eligible cids")
        entries = select_artifacts(roots.artifacts, got.eligible_cids)
        return files_store("artifacts", entries, roots.artifacts, f"terminal >= {policy.contributions_days} d",
                           lambda fd, name: True)  # fmt: skip

    def do_run_logs() -> StoreResult:
        entries = select_run_logs(roots.run_logs, policy.run_logs_days, now)
        return files_store("run-logs", entries, roots.run_logs, f"older than {policy.run_logs_days} d",
                           mtime_old(now - policy.run_logs_days * DAY_S))  # fmt: skip

    def do_proxy_logs() -> StoreResult:
        days = policy.proxy_logs_days
        entries = select_proxy_logs(roots.proxy_logs, days, now)
        proxy = files_store("proxy-logs", entries, roots.proxy_logs, f"rotated, older than {days} d",
                            mtime_old(now - days * DAY_S))  # fmt: skip
        if not roots.floor_logs.is_dir():  # ossbot-floorlog not provisioned yet
            return proxy
        floor = files_store("proxy-logs", select_floor_logs(roots.floor_logs, days, now), roots.floor_logs,
                            "floor logs", mtime_old(now - days * DAY_S))  # fmt: skip
        status: Status = "FAIL" if "FAIL" in (proxy.status, floor.status) else proxy.status
        detail = proxy.detail + (f"; floor logs {floor.count}" if floor.count or floor.status == "FAIL" else "")
        if floor.status == "FAIL":
            detail += f" ({floor.detail})"
        return StoreResult("proxy-logs", status, proxy.count + floor.count, proxy.bytes + floor.bytes, detail,
                           (proxy.items + floor.items)[:ITEMS_MAX])  # fmt: skip

    def do_hermes() -> StoreResult:
        entries = select_hermes_sessions(roots.hermes_sessions, policy.hermes_sessions_days, now)
        return files_store("hermes-sessions", entries, roots.hermes_sessions,
                           f"idle >= {policy.hermes_sessions_days} d",
                           mtime_old(now - policy.hermes_sessions_days * DAY_S), listed=False)  # fmt: skip

    def do_images() -> StoreResult:
        keep = docker_keep_set(runner, roots.lock)
        entries = select_dangling(runner, keep)
        detail = f"dangling, keep set {len(keep.refs)} refs intact"
        items = [e.name for e in entries][:ITEMS_MAX]
        if dry_run:
            return StoreResult("images", "WOULD DELETE", len(entries), sum(e.bytes for e in entries), detail, items)
        done, freed, refused = 0, 0, 0
        for entry in entries:
            if stop():
                break
            code, _ = runner([DOCKER, "image", "rm", entry.name], DOCKER_TIMEOUT_S)  # no --force: in use = refused
            if code == 0:
                done, freed = done + 1, freed + entry.bytes
            else:
                refused += 1
        broken = [ref for ref, image in keep.refs.items() if _image_id(runner, ref) != image]
        if broken:
            return StoreResult("images", "FAIL", done, freed, f"keep-set ref no longer resolves: {', '.join(broken)}",
                               items)  # fmt: skip
        if refused:
            detail += f", {refused} refused (in use)"
        return StoreResult("images", "DELETED", done, freed, detail, items)

    def do_build_cache() -> StoreResult:
        hours = policy.docker_build_cache_hours if build_cache_hours is None else build_cache_hours
        if dry_run:
            return StoreResult("build-cache", "SKIP", detail="dry run (docker builder prune has no dry run)")
        code, out = runner(builder_prune_argv(hours), PRUNE_TIMEOUT_S)
        records, freed = _prune_summary(out)
        detail = f"unused >= {hours} h" if hours else "every unused record (one-off)"
        if code != 0:
            return StoreResult("build-cache", "FAIL", records, freed, f"docker builder prune exit {code}")
        return StoreResult("build-cache", "DELETED", records, freed, detail)

    def do_fstrim() -> StoreResult:
        if dry_run:
            return StoreResult("fstrim", "SKIP", detail="dry run")
        code, out = runner(FSTRIM_ARGV, PRUNE_TIMEOUT_S)
        if code != 0:
            return StoreResult("fstrim", "FAIL", detail=f"fstrim exit {code}")
        match = re.search(r"\((\d+) bytes\) trimmed", out)
        return StoreResult("fstrim", "DONE", detail=f"/ trimmed {human(int(match.group(1))) if match else '?'}")

    handlers: dict[str, Callable[[], StoreResult]] = {
        "tmp": do_tmp, "gatekeeper": do_gatekeeper, "artifacts": do_artifacts, "run-logs": do_run_logs,
        "proxy-logs": do_proxy_logs, "hermes-sessions": do_hermes, "images": do_images,
        "build-cache": do_build_cache, "fstrim": do_fstrim,
    }  # fmt: skip
    for name in STORES:
        if name not in selected:
            report.stores.append(StoreResult(name, "SKIP", detail=WEEKLY if name == "build-cache" else "not selected"))
            continue
        if stop():
            break
        try:
            result = handlers[name]()
        except FileNotFoundError:
            result = StoreResult(name, "SKIP", detail="absent")
        except (OSError, ValueError) as exc:
            result = StoreResult(name, "FAIL", detail=type(exc).__name__)
        report.stores.append(result)
        log.info("gc_store", extra={"event": "gc_store", "store": name, "status": result.status,
                                    "count": result.count, "bytes": result.bytes, "dry_run": dry_run})  # fmt: skip
        if stop.state is not None:
            break
    report.stopped = stop.state
    try:
        write_last(roots.state_file, report)
    except OSError as exc:  # the deletions happened; doctor then shows the previous run
        log.warning("gc_state_not_written", extra={"event": "gc_state_not_written", "error": type(exc).__name__})
    return report


# -------------------------------------------------------------------------------- output
def render_text(report: Report) -> str:
    head = "ossctl gc --dry-run" if report.dry_run else "ossctl gc"
    lines = [f"{head} (retention from snapshot {report.snapshot[:12]})"]
    for s in report.stores:
        if s.status in ("WOULD DELETE", "DELETED"):
            one, many = UNITS[s.name]
            unit = one if s.count == 1 else many
            lines.append(f"{s.status:<14}{s.name:<16}{s.count:>3} {unit:<9}  {human(s.bytes):>9}  {s.detail}")
        else:
            lines.append(f"{s.status:<14}{s.name:<18}{s.detail}")
    lines.append(f"{'KEEP':<14}{KEEP_LINE}")
    if report.stopped is not None:
        lines.append(f"{'STOPPED':<14}{report.stopped}: the remaining stores did not run (exit 3)")
    deleting = report.deleting()
    lines.append(f"{'TOTAL':<14}{sum(s.count for s in deleting)} items, {human(sum(s.bytes for s in deleting))}")
    return "\n".join(line.rstrip() for line in lines) + "\n"


def report_dict(report: Report) -> dict[str, Any]:
    deleting = report.deleting()
    return {
        "dry_run": report.dry_run, "snapshot": report.snapshot, "ts": report.ts, "stopped": report.stopped,
        "exit_code": report.exit_code,
        "stores": {s.name: {"status": s.status, "count": s.count, "bytes": s.bytes, "detail": s.detail,
                            "items": s.items} for s in report.stores},
        "total": {"count": sum(s.count for s in deleting), "bytes": sum(s.bytes for s in deleting)},
    }  # fmt: skip


def render_json(report: Report) -> str:
    return json.dumps(report_dict(report), indent=2, sort_keys=True) + "\n"


def write_last(path: Path, report: Report) -> None:
    """``/var/lib/ossbot/gc/last.json`` (dir 0755, file 0644, atomic replace): what doctor shows."""
    doc = {
        "ts": report.ts, "dry_run": report.dry_run, "stopped": report.stopped,
        "stores": {s.name: {"count": s.count, "bytes": s.bytes, "status": s.status} for s in report.stores},
    }  # fmt: skip
    directory = path.parent
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError(f"{directory} is not a plain directory")
    directory.chmod(0o755)
    tmp = directory / f".{path.name}.tmp-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
