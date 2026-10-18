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
from ossbot.common.control_flags import (
    EGRESS_MODES,
    HERMES_TIERS,
    lifecycle_name,
    read_flags,
    read_lifecycle,
    write_flag,
)
from ossbot.common.paths import Paths
from ossbot.common.snapshot import read_current
from ossbot.common.uds import uds_client

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ossbot operator CLI")
config_app = typer.Typer(no_args_is_help=True, help="config snapshot")
audit_app = typer.Typer(no_args_is_help=True, help="audit log")
egress_app = typer.Typer(no_args_is_help=True, help="egress mode (allowlist | research | open)")
hermes_app = typer.Typer(no_args_is_help=True, help="Hermes (capability tier)")
app.add_typer(config_app, name="config")
app.add_typer(audit_app, name="audit")
app.add_typer(egress_app, name="egress")
app.add_typer(hermes_app, name="hermes")
model_app = typer.Typer(no_args_is_help=True, help="global model switch (config/models.yaml)")
models_app = typer.Typer(no_args_is_help=True, help="Kiro model catalog")
app.add_typer(model_app, name="model")
app.add_typer(models_app, name="models")
size_app = typer.Typer(no_args_is_help=True, help="autonomous size preference (CR-004; never the ceiling)")
app.add_typer(size_app, name="size")
contribution_app = typer.Typer(no_args_is_help=True, help="contributions (operator import, §9.13)")
app.add_typer(contribution_app, name="contribution")
targets_app = typer.Typer(no_args_is_help=True, help="targets (config/targets.yaml)")
app.add_typer(targets_app, name="targets")
kiro_app = typer.Typer(no_args_is_help=True, help="Kiro CLI checks (sandbox key)")
app.add_typer(kiro_app, name="kiro")

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
        code, data = api("POST", "/v1/control/up", {})  # the gatekeeper counts the stages the runner re-queues
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


@egress_app.command("mode")
def egress_mode(mode: Annotated[str, typer.Argument(help="allowlist | research | open")]) -> None:
    """Switch the egress mode (TTY confirm; DESIGN §11.5). Root re-renders squid and restarts the
    proxy (ossbot-egress.path); fetch/validate, runner and gatekeeper egress never change."""
    if mode not in EGRESS_MODES:
        _fail(f"usage: ossctl egress mode {'|'.join(EGRESS_MODES)}", 2)
    _confirm(f"EGRESS {mode.upper()}")
    code, data = api("POST", "/v1/control/egress-mode", {"mode": mode})
    if code != 200:
        _fail(f"egress mode change refused ({code}): {data}")
    _show(data)


@egress_app.command("status")
def egress_status() -> None:
    """The active egress mode and the snapshot default (``egress.mode_default``)."""
    code, data = api("GET", "/v1/control/egress-mode")
    if code != 200:
        _fail(f"gatekeeper answered {code}: {data}")
    _show(data)


@hermes_app.command("tier")
def hermes_tier(tier: Annotated[str | None, typer.Argument(help="locked | research | self_improve")] = None) -> None:
    """Show (no argument) or switch the Hermes capability tier (TTY confirm; DESIGN §15.5). A switch
    invalidates open skill approvals; applying it to Hermes arrives with the Hermes install (FEAT-007)."""
    if tier is None:
        code, data = api("GET", "/v1/control/hermes-tier")
        if code != 200:
            _fail(f"gatekeeper answered {code}: {data}")
        _show(data)
        return
    if tier not in HERMES_TIERS:
        _fail(f"usage: ossctl hermes tier [{'|'.join(HERMES_TIERS)}]", 2)
    _confirm(f"TIER {tier.upper()}")
    code, data = api("POST", "/v1/control/hermes-tier", {"tier": tier})
    if code != 200:
        _fail(f"tier change refused ({code}): {data}")
    _show(data)


@model_app.command("list")
def model_list() -> None:
    """Every role: configured model, effort and what it resolves to against the current catalog."""
    from ossbot.common.config import ROLE_NAMES, ModelsConfig  # noqa: PLC0415
    from ossbot.common.models import Catalog, ModelUnavailable, resolve_config  # noqa: PLC0415

    code, data = api("GET", "/v1/models")
    if code != 200:
        _fail(f"gatekeeper answered {code}: {data}")
    code, rows = api("GET", "/v1/models/catalog")
    if code != 200:
        _fail(f"gatekeeper answered {code}: {rows}")
    cfg = ModelsConfig.model_validate(data)
    catalog = Catalog.from_rows(rows)
    typer.echo(f"default: {cfg.default.model} (effort {cfg.default.effort})")
    for role in ROLE_NAMES:
        entry = getattr(cfg.roles, role)
        try:
            res = resolve_config(cfg, role, catalog)
            resolved = f"{res.model} [{res.effort}, {res.source}]"
        except ModelUnavailable:
            resolved = "UNAVAILABLE (model_unavailable)"
        typer.echo(f"{role:<11} {entry.model:<28} -> {resolved}")
    typer.echo(f"catalog: {len(catalog.ids)} models, {len(catalog.forced)} forced")


@model_app.command("set")
def model_set(
    role: Annotated[str, typer.Argument(help="role name or 'default'")],
    model: Annotated[str, typer.Argument(help="catalog id, 'default' or a role name (inherit)")],
    effort: Annotated[str | None, typer.Option(help="low | medium | high | xhigh | max")] = None,
    force: Annotated[bool, typer.Option("--force", help="accept an id outside the catalog (recorded)")] = False,
    commit: Annotated[bool, typer.Option("--commit", help="git commit config/models.yaml as ossadmin")] = False,
) -> None:
    """Switch a role's model (DESIGN §16.2). The gatekeeper rewrites models.yaml and prints the diff."""
    body: dict[str, Any] = {"model": model, "force": force}
    if effort is not None:
        body["effort"] = effort
    code, data = api("PUT", f"/v1/models/{role}", body)
    if code != 200:
        _fail(f"model set refused ({code}): {data}")
    typer.echo(data.get("diff") or "models.yaml unchanged")
    if commit:
        repo = Paths.default().repo
        argv = ["/usr/bin/git", "-C", str(repo), "commit", "-m", f"chore(models): {role} -> {model}", "--",
                "config/models.yaml"]  # fmt: skip
        if os.geteuid() == 0:
            argv = ["/usr/sbin/runuser", "-u", "ossadmin", "--", *argv]
        raise typer.Exit(subprocess.run(argv, check=False).returncode)


@models_app.command("refresh")
def models_refresh() -> None:
    """Ask the runner to run the catalog job (sandboxd list_models) on its next scheduler tick."""
    code, data = api("POST", "/v1/models/refresh")
    if code != 202:
        _fail(f"refresh request refused ({code}): {data}")
    typer.echo(f"catalog refresh requested at {data.get('requested_at')}; the runner picks it up on its next tick")


@app.command("work")
def work(
    url: Annotated[str, typer.Argument(help="GitHub issue, PR or repo URL of a configured target")],
    notes: Annotated[str, typer.Option(help="what you want (≤ 1000 chars; shown on every card)")] = "",
) -> None:
    """User-directed work (👤): vet -> scout -> proposal card; size preference and filters don't apply."""
    code, data = api("POST", "/v1/work", {"url": url, "notes": notes})
    if code != 201:
        _fail(f"work request refused ({code}): {data}")
    _show(data)


@app.command("discover")
def discover(
    repos: Annotated[list[str] | None, typer.Argument(help="owner/repo or GitHub URL (default: every target)")] = None,
) -> None:
    """One discovery cycle on the runner's next tick (§9.4; reads only, nothing is posted)."""
    code, data = api("POST", "/v1/discover", {"repos": repos or []})
    if code != 202:
        _fail(f"discover request refused ({code}): {data}")
    typer.echo(f"discovery requested for {', '.join(data['repos']) or 'every enabled target'}; the runner picks it"
               " up on its next tick and sends proposal cards as they are scouted")  # fmt: skip


@contribution_app.command("import")
def contribution_import(
    repo: Annotated[str, typer.Option(help="owner/repo of an enabled target")],
    from_bundle: Annotated[Path, typer.Option("--from-bundle", help="git bundle with exactly one head")],
    base: Annotated[str, typer.Option(help="base commit sha (the bundle's prerequisite)")],
    title_file: Annotated[Path, typer.Option("--title-file", help="PR title (first line)")],
    body_file: Annotated[Path, typer.Option("--body-file", help="PR body (markdown)")],
    base_ref: Annotated[str, typer.Option("--base-ref", help="upstream branch")] = "main",
    issue: Annotated[int | None, typer.Option(help="issue number it fixes")] = None,
) -> None:
    """Operator import (§9.13, M3): uploads the bundle bytes; validate -> review -> package -> card."""
    import base64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415

    try:
        data = from_bundle.read_bytes()  # read with the caller's own permissions; the gatekeeper never opens a path
        title = title_file.read_text(encoding="utf-8").strip().splitlines()[0] if title_file.stat().st_size else ""
        body = body_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, IndexError) as exc:
        _fail(f"cannot read the import files: {exc}")
    if len(data) > 50 * 1024 * 1024:
        _fail("bundle larger than 50 MB")
    req = {"repo": repo, "base_ref": base_ref, "base_sha": base, "bundle_b64": base64.b64encode(data).decode("ascii"),
           "bundle_sha256": hashlib.sha256(data).hexdigest(), "title": title, "body": body,
           "issue_number": issue}  # fmt: skip
    code, doc = api("POST", "/v1/contributions/import", req, timeout=300.0)
    if code != 201:
        _fail(f"import refused ({code}): {doc}")
    _show(doc)


SMOKE_CID = "c_KIROSMOKE0"
SMOKE_PROMPT = (
    "This is an ossbot connectivity check. Do not use any tool. Reply with exactly this fenced block and"
    ' nothing else:\n```json\n{"answer": "pong"}\n```\n'
)


@kiro_app.command("smoke")
def kiro_smoke(timeout: Annotated[int, typer.Option(help="job timeout in s")] = 300) -> None:
    """Headless ``chat --v3`` round-trip with the sandbox key (root): one sandboxd job, role
    summarizer, research profile, a canned prompt; the answer is parsed like any stage output."""
    _require_root("kiro smoke")
    import secrets as pysecrets  # noqa: PLC0415

    from pydantic import BaseModel, ConfigDict  # noqa: PLC0415

    from ossbot.common.models import LIVE_TEST_ROLE, Catalog, ModelsFile, ModelUnavailable  # noqa: PLC0415
    from ossbot.runner import output_parse  # noqa: PLC0415

    class Pong(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str

    paths = Paths.default()
    code, rows = api("GET", "/v1/models/catalog")
    if code != 200:
        _fail(f"catalog unavailable ({code})")
    try:
        model = ModelsFile(paths.config_dir / "models.yaml").resolve(LIVE_TEST_ROLE, Catalog.from_rows(rows))
    except ModelUnavailable as exc:
        _fail(f"model_unavailable: {exc}")
    run_id = f"smoke-{pysecrets.token_hex(6)}"
    inputs = paths.srv / "runner" / "inputs" / "smoke" / run_id  # sandboxd reads runner/inputs (group osssbx)
    inputs.mkdir(parents=True, mode=0o750)
    prompt = inputs / "summarizer-task.md"
    prompt.write_text(SMOKE_PROMPT, encoding="utf-8")
    for p in (inputs.parent, inputs, prompt):
        os.chown(p, -1, (inputs.parent.parent.stat().st_gid))
    prompt.chmod(0o640)
    spec = {"run_id": run_id, "cid": SMOKE_CID, "profile": "research", "entrypoint": "kiro", "repo_mode": "none",
            "args": {"role": "summarizer", "prompt_file": prompt.name, "model": model.model, "effort": model.effort},
            "inputs": {prompt.name: str(prompt)}, "timeout_s": timeout}  # fmt: skip
    started = time.monotonic()
    try:
        job = _sandboxd("POST", "/v1/jobs", spec, timeout=30.0)
        while job.get("state") not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() - started > timeout + 60:
                _fail(f"smoke job {run_id} did not finish")
            time.sleep(2.0)
            job = _sandboxd("GET", f"/v1/jobs/{run_id}")
    except httpx.HTTPError as exc:
        _fail(f"sandboxd refused the smoke job: {exc}")
    duration = round(time.monotonic() - started, 1)
    report: dict[str, Any] = {"run_id": run_id, "model": model.model, "effort": model.effort,
                              "state": job.get("state"), "error_class": job.get("error_class"),
                              "duration_s": duration}  # fmt: skip
    if job.get("state") == "succeeded" and job.get("artifact_dir"):
        art = Path(str(job["artifact_dir"]))
        stderr = art / "summarizer.stderr"
        out = output_parse.read_events((art / "summarizer.events.jsonl").read_bytes(),
                                       stderr.read_bytes() if stderr.is_file() else b"")  # fmt: skip
        report["model_actual"] = out.model_actual
        try:
            report["answer"] = output_parse.parse_model(out.text, Pong).answer
        except output_parse.OutputInvalid as exc:
            report["answer_error"] = exc.reason[:200]
    _show(report)
    raise typer.Exit(0 if report.get("answer") == "pong" else 1)


FIREWALL_CHECK = "/opt/ossbot/bin/ossbot-firewall"
# The hardening of systemd/ossbot-runner.service, for the transient unit of `targets verify`
# (tests/unit/ctl/test_cli_pipeline.py compares the two).
TARGETS_VERIFY_PROPERTIES = (
    "EnvironmentFile=/etc/ossbot/secrets/runner.env", "UMask=0027", "NoNewPrivileges=yes", "ProtectSystem=strict",
    "ProtectHome=yes", "PrivateTmp=yes", "CapabilityBoundingSet=", "RestrictSUIDSGID=yes", "LockPersonality=yes",
    "SystemCallArchitectures=native", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    "SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register", "SystemCallErrorNumber=EPERM",
)  # fmt: skip


def targets_verify_argv() -> list[str]:
    props = [arg for prop in TARGETS_VERIFY_PROPERTIES for arg in ("-p", prop)]
    return ["/usr/bin/systemd-run", "--quiet", "--collect", "--wait", "--pipe", "--uid=ossrunner", "--gid=ossrunner",
            *props, "/opt/ossbot/app/.venv/bin/python", "-m", "ossbot.runner.verify_targets"]  # fmt: skip


@targets_app.command("verify")
def targets_verify() -> None:
    """Read every enabled target with the fine-grained read token (root; runs as ossrunner via 3140)."""
    _require_root("targets verify")
    from ossbot.common.config import DiscoveryFilters, all_targets  # noqa: PLC0415

    try:
        _, files = read_current(Paths.default().snapshot_root)
        bundle = parse_bundle(files)
    except (OSError, ValueError, ConfigError) as exc:
        _fail(f"no usable config snapshot ({exc}); ossctl config reload")
    repos = [t.repo for t in all_targets(bundle.targets) if t.enabled]
    check = subprocess.run([FIREWALL_CHECK, "--check"], capture_output=True, text=True, timeout=60, check=False)
    if check.returncode != 0:  # the runner unit's ExecStartPre: no read with the token while egress is open
        _fail(f"firewall check failed (exit {check.returncode}): {check.stderr.strip()[-300:]}")
    req = json.dumps({"repos": repos, "stale_days": DiscoveryFilters().min_maintainer_activity_days})
    proc = subprocess.run(targets_verify_argv(), input=req, capture_output=True, text=True, timeout=900, check=False)
    try:
        doc = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        _fail(f"targets verify failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
    for row in doc["rows"]:
        typer.echo(f"{row['status']:<5} {row['repo']:<55} {row.get('pushed_at') or '-'!s:<21} {row['detail']}")
    counts = {s: sum(1 for r in doc["rows"] if r["status"] == s) for s in ("PASS", "WARN", "FAIL")}
    typer.echo(
        f"{len(doc['rows'])} enabled targets: {counts['PASS']} PASS, {counts['WARN']} WARN, {counts['FAIL']} FAIL"
    )
    raise typer.Exit(0 if doc["ok"] else 1)


def _prefs_show(repo: str | None) -> None:
    code, data = api("GET", "/v1/discovery-prefs" + (f"?repo={repo}" if repo else ""))
    if code != 200:
        _fail(f"gatekeeper answered {code}: {data}")
    _show(data["effective"])


def _prefs_put(body: dict[str, Any]) -> None:
    code, data = api("PUT", "/v1/discovery-prefs", body)
    if code != 200:
        _fail(f"preference change refused ({code}): {data}")
    _show(data)
    typer.echo(f"undo within 60 s: ossctl size undo {data['undo_id']}")


@size_app.command("show")
def size_show(repo: Annotated[str | None, typer.Option(help="owner/repo (default: global)")] = None) -> None:
    """Effective autonomous size preference and filters (where each value comes from)."""
    _prefs_show(repo)


@size_app.command("set")
def size_set(
    size_class: Annotated[str, typer.Argument(help="xs | s | m | l | xl (inherit: per repo only)")],
    repo: Annotated[str | None, typer.Option(help="owner/repo override (autonomous discovery only)")] = None,
) -> None:
    """Largest class the bot picks on its own. Never limits /work items; the ceiling is config-only."""
    _prefs_put({"scope": repo or "*", "autonomous_max_class": size_class})


@size_app.command("undo")
def size_undo(undo_id: Annotated[str, typer.Argument(help="u_... from the last change")]) -> None:
    code, data = api("POST", "/v1/discovery-prefs/undo", {"undo_id": undo_id})
    if code != 200:
        _fail(f"undo refused ({code}): {data}")
    _show(data)


@app.command("filters")
def filters(
    set_: Annotated[list[str] | None, typer.Option("--set", help="key=value (JSON value; repeatable)")] = None,
    repo: Annotated[str | None, typer.Option(help="owner/repo override")] = None,
) -> None:
    """Show (no --set) or change the autonomous discovery filters (kinds, labels, tier, age, ...)."""
    if not set_:
        _prefs_show(repo)
        return
    values: dict[str, Any] = {}
    for item in set_:
        key, sep, raw = item.partition("=")
        if not sep:
            _fail("usage: --set key=value", 2)
        try:
            values[key] = json.loads(raw)
        except ValueError:
            values[key] = raw
    _prefs_put({"scope": repo or "*", "filters": values})


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
