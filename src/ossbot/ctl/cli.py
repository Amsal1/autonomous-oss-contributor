"""``ossctl``: the operator CLI (DESIGN §12.2, §13, §18.3). Runs as root or ossadmin (operator role).

Talks to the gatekeeper over ``/run/ossbot/gatekeeper.sock`` with the caller's own uid (the role
comes from SO_PEERCRED). "Go" actions (resume, dry-run off, config reload) need an interactive TTY
confirm; the gatekeeper announces each one on Telegram. When the gatekeeper is down, ``kill`` and
``resume`` take the offline path through the root helpers (``ossbot-kill``, ``ossbot-resume``, via
``sudo`` for ossadmin, PLAN Decision 3); the offline resume is logged to ``resume.log`` first (N4).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from ossbot.common.config import ConfigError, LifecyclePolicy, parse_bundle
from ossbot.common.control_flags import lifecycle_name, read_flags, read_lifecycle, write_flag
from ossbot.common.paths import Paths
from ossbot.common.snapshot import read_current
from ossbot.common.uds import uds_client

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ossbot operator CLI")
config_app = typer.Typer(no_args_is_help=True, help="config snapshot")
audit_app = typer.Typer(no_args_is_help=True, help="audit log")
app.add_typer(config_app, name="config")
app.add_typer(audit_app, name="audit")

BIN = Path("/opt/ossbot/bin")
SANDBOXD_SOCK = Path("/run/ossbot/sandboxd.sock")
SYSTEMCTL = "/usr/bin/systemctl"
# CR-001 stop order: workers first, then the watchdog (so it does not count the stopped gatekeeper
# as unhealthy), the gatekeeper and the proxy last (the Telegram summary is flushed before).
WORKER_UNITS = ("ossbot-runner.service", "ossbot-hermes.service", "ossbot-sandboxd.service")
CORE_UNITS = ("ossbot-gatekeeper.service", "ossbot-proxy.service")
WATCHDOG_TIMER = "ossbot-watchdog.timer"
OUTBOX_FLUSH_S = 20.0


class GatekeeperDown(RuntimeError):
    pass


def _sock() -> Path:
    return Paths.default().gatekeeper_sock


def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    try:
        with uds_client(_sock(), timeout=httpx.Timeout(timeout, connect=3.0)) as client:
            resp = client.request(method, path, json=body)
    except (httpx.HTTPError, OSError) as exc:
        raise GatekeeperDown(str(exc)) from exc
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _fail(message: str, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _confirm(word: str) -> None:
    """TTY confirm for 'go' actions: the operator must type the word."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail(f"refused: this action needs an interactive TTY confirm (type {word})", 2)
    answer = input(f"Type {word} to confirm: ").strip()
    if answer != word:
        _fail("not confirmed", 1)


def _root_helper(name: str, *args: str) -> int:
    """Run /opt/ossbot/bin/<name> as root (directly, or via the NOPASSWD sudo rule for ossadmin)."""
    argv = [str(BIN / name), *args]
    if os.geteuid() != 0:
        argv = ["/usr/bin/sudo", "-n", *argv]
    return subprocess.run(argv, check=False).returncode


def _show(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True) if not isinstance(data, str) else data)


def _local_lifecycle() -> dict[str, Any]:
    """What the control dir says without the gatekeeper (root; best effort for ossadmin)."""
    paths = Paths.default()
    out: dict[str, Any] = {}
    if not os.access(paths.control_dir, os.R_OK | os.X_OK):  # unreadable flags would read as "killed"
        return {"control_dir": "not readable as this user (sudo ossctl status)"}
    flags = read_flags(paths.control_dir)
    info = read_lifecycle(paths.control_dir)
    out["killed"] = None if flags.killed is None else {"level": flags.killed.level, "reason": flags.killed.reason}
    out["paused"] = None if flags.paused is None else {"reason": flags.paused.reason}
    out["lifecycle_flag"] = lifecycle_name(info) if info is None else {"state": info.state, "scope": info.scope}
    try:
        out["last_shutdown"] = json.loads(paths.last_shutdown.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["last_shutdown"] = None
    return out


def _sandboxd(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    with uds_client(SANDBOXD_SOCK, timeout=httpx.Timeout(timeout, connect=3.0)) as client:
        resp = client.request(method, path, json=body)
    resp.raise_for_status()
    return resp.json()


@app.command()
def status() -> None:
    """Control state (running / paused / killed), lifecycle (up / draining / down), dry-run,
    running work, pending approvals, Telegram last poll and the last shutdown type (CR-001)."""
    try:
        code, data = api("GET", "/v1/control/lifecycle")
        if code == 403:  # not an operator uid: the plain control view only
            code, data = api("GET", "/v1/control")
    except GatekeeperDown as exc:
        typer.echo(f"gatekeeper unreachable ({exc})")
        _show(_local_lifecycle())
        raise typer.Exit(1) from None
    if code != 200:
        _fail(f"gatekeeper answered {code}: {data}")
    if isinstance(data, dict) and isinstance(data.get("control"), dict):
        data = {**data.pop("control"), **data}
        local = _local_lifecycle()
        data["last_shutdown"] = local.get("last_shutdown")
        try:
            health = _sandboxd("GET", "/v1/health")
            data["sandbox_running_jobs"] = health.get("running")
        except (httpx.HTTPError, OSError, ValueError):
            data["sandbox_running_jobs"] = None  # not root, or sandboxd stopped
    _show(data)


# ------------------------------------------------------------------- lifecycle (CR-001)
def _require_root(what: str) -> None:
    if os.geteuid() != 0:
        _fail(f"{what} runs as root (sudo ossctl {what}; Windows: scripts\\{what}.ps1)", 2)


def _lifecycle_policy() -> LifecyclePolicy:
    try:
        _, files = read_current(Paths.default().snapshot_root)
        return parse_bundle(files).policy.lifecycle
    except (OSError, ValueError, ConfigError):
        return LifecyclePolicy()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _systemctl(action: str, units: tuple[str, ...]) -> list[str]:
    """``systemctl <action>`` each unit that exists, in order; returns the failures."""
    failed: list[str] = []
    for unit in units:
        if subprocess.run([SYSTEMCTL, "cat", unit], capture_output=True, check=False).returncode != 0:
            continue  # not installed (runner/hermes before FEAT-006/007)
        if subprocess.run([SYSTEMCTL, action, unit], check=False).returncode != 0:
            failed.append(unit)
    return failed


def _lifecycle_field(name: str) -> Any:
    code, data = api("GET", "/v1/control/lifecycle", timeout=5.0)
    return data.get(name) if code == 200 and isinstance(data, dict) else None


def _wait(predicate: Any, timeout_s: float, step: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if predicate():
                return True
        except (GatekeeperDown, httpx.HTTPError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


@app.command()
def down(
    timeout: Annotated[int | None, typer.Option(help="drain timeout in s (default: policy lifecycle)")] = None,
    scope: Annotated[str, typer.Option(help="distro (stop everything) | workers (gatekeeper stays)")] = "distro",
    kind: Annotated[str, typer.Option(help="graceful | hook (shutdown marker type)")] = "graceful",
) -> None:
    """Graceful down (CR-001): drain, interrupt what does not finish, summary, stop the units.

    Not a kill: approvals stay valid, autostart stays enabled, and ``up`` (or the next distro
    start for scope distro) brings it back. Kill always overrides lifecycle.
    """
    _require_root("down")
    if scope not in {"distro", "workers"} or kind not in {"graceful", "hook"}:
        _fail("usage: ossctl down [--timeout S] [--scope distro|workers] [--kind graceful|hook]", 2)
    paths = Paths.default()
    drain_s = timeout if timeout is not None else _lifecycle_policy().drain_timeout_s
    drain_s = max(0, min(drain_s, 3600))
    started = time.monotonic()
    report: dict[str, Any] = {"scope": scope, "kind": kind, "drain_timeout_s": drain_s}
    reason = f"ossctl down ({kind})"
    try:
        code, data = api("POST", "/v1/control/drain", {"reason": reason, "timeout_s": drain_s, "scope": scope})
        report["gatekeeper"] = "draining" if code == 200 else f"drain refused {code}: {data}"
    except GatekeeperDown:
        report["gatekeeper"] = "unreachable"
    if read_lifecycle(paths.control_dir) is None:  # gatekeeper down or refused: write it ourselves
        write_flag(paths.lifecycle_flag, {"state": "draining", "scope": scope, "reason": reason,
                                          "actor": "ossctl", "ts": _now_iso(), "timeout_s": drain_s})  # fmt: skip
    try:
        result = _sandboxd("POST", "/v1/drain", {"timeout_s": drain_s}, timeout=drain_s + 60.0)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        result = {"finished": 0, "interrupted": 0, "error": f"sandboxd: {type(exc).__name__}"}
    report["sandbox"] = result
    left = max(5.0, drain_s - (time.monotonic() - started))
    gk_up = report["gatekeeper"] != "unreachable"
    report["submissions_settled"] = gk_up and _wait(lambda: _lifecycle_field("executing_actions") == 0, left)
    finished, interrupted = int(result.get("finished", 0)), int(result.get("interrupted", 0))
    try:
        code, data = api("POST", "/v1/control/down", {"finished": finished, "interrupted": interrupted, "scope": scope})
        # the Telegram summary must leave before the gatekeeper and the proxy stop
        report["summary_delivered"] = code == 200 and _wait(
            lambda: _lifecycle_field("outbox_pending") == 0, OUTBOX_FLUSH_S
        )
    except GatekeeperDown:
        report["summary_delivered"] = False
    info = read_lifecycle(paths.control_dir)
    if info is None or info.state != "down":
        write_flag(paths.lifecycle_flag, {"state": "down", "scope": scope, "reason": reason, "actor": "ossctl",
                                          "ts": _now_iso(), "timeout_s": drain_s})  # fmt: skip
    if scope == "distro":
        write_flag(paths.shutdown_marker, {"type": kind, "ts": _now_iso(), "interrupted": interrupted})
        failed = _systemctl("stop", (*WORKER_UNITS, WATCHDOG_TIMER, *CORE_UNITS))
    else:
        failed = _systemctl("stop", WORKER_UNITS)
    report["stop_failed"] = failed
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    _show(report)
    raise typer.Exit(1 if failed else 0)


@app.command()
def up(
    timeout: Annotated[int | None, typer.Option(help="health wait in s (default: policy lifecycle)")] = None,
) -> None:
    """Back up (CR-001): start the units, wait until healthy, lift draining/down, doctor --quick.

    Refused while killed (exit 3): kill overrides lifecycle and needs the human resume.
    """
    _require_root("up")
    paths = Paths.default()
    flags = read_flags(paths.control_dir)
    if flags.killed is not None:
        _fail(f"killed ({flags.killed.level}: {flags.killed.reason}): up refused; resume first "
              "(/resume, ossctl resume, scripts\\resume.ps1)", 3)  # fmt: skip
    wait_s = timeout if timeout is not None else _lifecycle_policy().up_health_timeout_s
    report: dict[str, Any] = {"start_failed": _systemctl("start", (*CORE_UNITS, *WORKER_UNITS, WATCHDOG_TIMER))}
    healthy = _wait(lambda: api("GET", "/v1/health", timeout=5.0)[0] == 200, wait_s)
    sandbox = _wait(lambda: _sandboxd("GET", "/v1/health").get("ok") is True, 30 if healthy else 1)
    report["gatekeeper_healthy"], report["sandboxd_healthy"] = healthy, sandbox
    if not healthy:
        _show(report)
        _fail(f"gatekeeper not healthy within {wait_s} s (journalctl -u ossbot-gatekeeper); lifecycle unchanged")
    try:
        code, data = api("POST", "/v1/control/up", {"resumed": 0})  # stage re-queue arrives with the runner
    except GatekeeperDown as exc:
        code, data = 503, str(exc)
    if code != 200:
        _show(report)
        _fail(f"up refused ({code}): {data}", 3 if code == 409 else 1)
    report["control"] = {k: data.get(k) for k in ("state", "lifecycle", "dry_run")}
    from ossbot.ctl import doctor as doctor_mod  # noqa: PLC0415

    results = doctor_mod.run(quick=True)
    report["doctor_quick"] = {s: sum(1 for r in results if r.status == s) for s in sorted({r.status for r in results})}
    report["doctor_failures"] = [f"{r.name}: {r.detail}" for r in results if r.status == "FAIL"]
    report["last_shutdown"] = _local_lifecycle().get("last_shutdown")
    _show(report)
    raise typer.Exit(1 if report["start_failed"] or not sandbox else 0)


@app.command()
def pause(reason: Annotated[str, typer.Option(help="why")] = "ossctl") -> None:
    """Pause: no new stages, jobs or outbound actions."""
    code, data = api("POST", "/v1/control/pause", {"reason": reason})
    if code != 200:
        _fail(f"pause failed ({code}): {data}")
    _show(data)


@app.command()
def kill(
    reason: Annotated[str, typer.Option(help="why")] = "ossctl",
    full: Annotated[bool, typer.Option("--full", help="also stop gatekeeper and proxy")] = False,
) -> None:
    """Kill (soft: gatekeeper keeps answering /status and /resume; --full stops it too)."""
    if not full:
        try:
            code, data = api("POST", "/v1/control/kill", {"reason": reason})
            if code == 200:
                _show(data)
                return
            typer.echo(f"gatekeeper refused the kill ({code}: {data}); using the root helper", err=True)
        except GatekeeperDown:
            typer.echo("gatekeeper unreachable; using the root helper", err=True)
    rc = _root_helper("ossbot-kill", "--full" if full else "--soft", "--reason", reason)
    raise typer.Exit(0 if rc == 0 else 1)


@app.command()
def resume() -> None:
    """Resume after pause/kill (TTY confirm). Offline path when the gatekeeper is down (N4)."""
    _confirm("RESUME")
    try:
        code, data = api("POST", "/v1/control/resume")
    except GatekeeperDown:
        typer.echo("gatekeeper unreachable: offline resume via ossbot-resume (logged to resume.log)")
        rc = _root_helper("ossbot-resume", "--offline", "--via", "ossctl", "--actor-uid", str(os.getuid()))
        raise typer.Exit(0 if rc == 0 else 1) from None
    if code != 202:
        _fail(f"resume refused ({code}): {data}")
    typer.echo("resume requested; the root enforcer removes the flags and starts the units")


@app.command()
def dryrun(mode: Annotated[str, typer.Argument(help="on | off")]) -> None:
    """Dry-run on (no GitHub writes) or off (TTY confirm). Every toggle invalidates open approvals."""
    if mode not in {"on", "off"}:
        _fail("usage: ossctl dryrun on|off", 2)
    if mode == "off":
        _confirm("DRY-RUN OFF")
    code, data = api("POST", "/v1/control/dry-run", {"enabled": mode == "on"})
    if code != 200:
        _fail(f"dry-run change refused ({code}): {data}")
    _show(data)


@config_app.command("reload")
def config_reload() -> None:
    """Show the diff of the config files against the snapshot, then apply after a TTY confirm."""
    code, data = api("POST", "/v1/config/reload", {})
    if code != 200:
        _fail(f"reload preview failed ({code}): {data}")
    if not data.get("changed_files"):
        typer.echo("config unchanged")
        return
    typer.echo(data.get("diff", ""))
    typer.echo(f"new snapshot {data['hash']} (changed: {', '.join(data['changed_files'])})")
    _confirm("RELOAD")
    code, data = api("POST", "/v1/config/reload", {"confirm_hash": data["hash"]})
    if code != 200:
        _fail(f"reload failed ({code}): {data}")
    _show(data)


@audit_app.command("verify")
def audit_verify() -> None:
    """Recompute the audit hash chain and check it against the root-written anchors (AC 16)."""
    code, data = api("GET", "/v1/audit/verify", timeout=120.0)
    if code != 200:
        _fail(f"audit verify failed ({code}): {data}")
    _show(data)
    if not data.get("ok"):
        raise typer.Exit(1)


@app.command()
def doctor(
    quick: Annotated[bool, typer.Option("--quick", help="local checks only, no probes")] = False,
    probes: Annotated[bool, typer.Option("--probes", help="re-run the Kiro safety probes (needs keys)")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Health checks (DESIGN §18.3, M11 order). Secret-dependent checks report AWAITING SECRET."""
    from ossbot.ctl import doctor as doctor_mod  # noqa: PLC0415 - heavy imports only for this command

    results = doctor_mod.run(quick=quick, probes=probes)
    if as_json:
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        doctor_mod.print_results(results)
    raise typer.Exit(1 if any(r.status == "FAIL" for r in results) else 0)


@app.command("test")
def run_tests(
    suite: Annotated[str, typer.Argument(help="system")] = "system",
    repo: Annotated[Path | None, typer.Option(help="repo root")] = None,
) -> None:
    """Run the root system tests (tests/system/*.sh) against this distro."""
    repo = repo or Paths.default().repo
    if suite != "system":
        _fail("only 'system' is run by ossctl; hermetic suites run with uv run pytest", 2)
    if os.geteuid() != 0:
        _fail("system tests run as root (sudo ossctl test system)", 2)
    failed: list[str] = []
    for script in sorted((repo / "tests" / "system").glob("test_*.sh")):
        typer.echo(f"== {script.name}")
        if subprocess.run(["/bin/bash", str(script)], cwd=repo, check=False).returncode != 0:
            failed.append(script.name)
    if failed:
        _fail(f"failed: {', '.join(failed)}")
    typer.echo("all system tests passed")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
