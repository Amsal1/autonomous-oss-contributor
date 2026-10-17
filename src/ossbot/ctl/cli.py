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
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from ossbot.common.paths import Paths
from ossbot.common.uds import uds_client

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ossbot operator CLI")
config_app = typer.Typer(no_args_is_help=True, help="config snapshot")
audit_app = typer.Typer(no_args_is_help=True, help="audit log")
app.add_typer(config_app, name="config")
app.add_typer(audit_app, name="audit")

BIN = Path("/opt/ossbot/bin")


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


@app.command()
def status() -> None:
    """Control state (running / paused / killed), dry-run and config hash."""
    try:
        code, data = api("GET", "/v1/control")
    except GatekeeperDown as exc:
        flags = Paths.default().killed_flag
        typer.echo(f"gatekeeper unreachable ({exc})")
        try:
            typer.echo(f"killed flag: {flags.read_text(encoding='utf-8').strip()}")
        except FileNotFoundError:
            typer.echo("killed flag: absent")
        except PermissionError:
            typer.echo("killed flag: not readable as this user (sudo ossctl status)")
        raise typer.Exit(1) from None
    if code != 200:
        _fail(f"gatekeeper answered {code}: {data}")
    _show(data)


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
