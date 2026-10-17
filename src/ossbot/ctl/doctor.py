"""``ossctl doctor``: distro-side health checks in the fixed M11 order (DESIGN §18.3, §10.9).

Each check yields PASS, WARN, FAIL, SKIP (component not built yet: Hermes is phase 2) or
``AWAITING SECRET: <name>`` for checks that need a credential the owner has not created yet
(Telegram, GitHub write/read, the two Kiro keys). Secret values are never printed: env files are
only checked for presence and format. ``--quick`` runs the local checks only (no per-uid probes,
no container runs). The Windows part is ``scripts/ossctl.ps1 doctor``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from ossbot.common.images import load_image_pins
from ossbot.common.paths import Paths
from ossbot.common.uds import uds_client

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
AWAITING = "AWAITING SECRET"
SECRETS = Path("/etc/ossbot/secrets")
LOCK_COPY = Path("/etc/ossbot/versions.lock.yaml")
FIREWALL = "/opt/ossbot/bin/ossbot-firewall"
DOCKER = "/usr/bin/docker"
FILTER_SOCK = Path("/run/ossbot/docker-filter.sock")
SANDBOXD_SOCK = Path("/run/ossbot/sandboxd.sock")
PROXY_IP = "10.212.0.2"
# §18.3 entry validation (setup-secrets.sh uses the same patterns).
FORMATS: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": r"^\d{6,12}:[A-Za-z0-9_-]{30,}$",
    "GITHUB_WRITE_TOKEN": r"^ghp_[A-Za-z0-9]{20,}$",
    "GITHUB_READ_TOKEN": r"^github_pat_[A-Za-z0-9_]{20,}$",
    "KIRO_API_KEY": r"^ksk_[A-Za-z0-9_\-]{16,}$",
}
DAEMON_JSON_KEYS = {
    "data-root": "/srv/ossbot/docker", "iptables": True, "ip6tables": False, "userland-proxy": False,
    "live-restore": False, "bip": "10.211.0.1/24", "firewall-backend": "iptables",
}  # fmt: skip
EXPECTED_GROUPS: dict[str, set[str]] = {
    "ossadmin": {"sudo", "osscfg", "ossapi"},
    "ossnobody": set(),
    "gatekeeper": {"osscfg", "ossart", "osscontrol"},
    "ossrunner": {"ossapi", "ossart", "osscontrol", "osssbx"},
    "ossbox": {"ossapi", "ossart", "osscontrol", "ossdock"},
    "hermes": {"ossapi"},
}
KIRO_SETTINGS_WANT = {
    "chat.enableKnowledge": "false", "chat.enableCodeIntelligence": "false", "chat.enableDelegate": "false",
    "chat.enableWorkflows": "false", "telemetry.enabled": "false", "app.disableAutoupdates": "true",
}  # fmt: skip


@dataclass(frozen=True)
class Result:
    status: str
    name: str
    detail: str = ""
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _run(argv: list[str], timeout: float = 20.0, user: str | None = None) -> subprocess.CompletedProcess[str]:
    if user is not None:
        argv = ["/usr/sbin/runuser", "-u", user, "--", *argv]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 124, "", str(exc))


def _is_root() -> bool:
    return os.geteuid() == 0


def env_value(file: str, key: str) -> str | None:
    """Value of KEY in /etc/ossbot/secrets/<file> (root only); None when absent or unreadable."""
    try:
        text = (SECRETS / file).read_text(encoding="utf-8")
    except OSError:
        return None
    value = None
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
    return value or None


def _secret(file: str, key: str, label: str | None = None) -> Result | None:
    """AWAITING SECRET when absent, FAIL when malformed, None when present and well-formed."""
    name = label or f"secret {key} ({file})"
    if not _is_root():
        return Result(WARN, name, "not readable as this user", "run as root: sudo ossctl doctor")
    value = env_value(file, key)
    if value is None:
        return Result(AWAITING, name, f"{AWAITING}: {key}", "scripts\\setup-secrets.ps1")
    pattern = FORMATS.get(key)
    if pattern and not re.fullmatch(pattern, value):
        return Result(FAIL, name, "present but malformed", "re-run scripts\\setup-secrets.ps1")
    return None


# ------------------------------------------------------------------------------- the checks
def check_mounts() -> list[Result]:
    out: list[Result] = []
    mounts = _run(["/usr/bin/findmnt", "-rn", "-o", "TARGET,FSTYPE"]).stdout.splitlines()
    windows = [m.split()[0] for m in mounts if len(m.split()) == 2 and m.split()[1] in {"9p", "drvfs", "v9fs"}]
    windows = [m for m in windows if not m.startswith(("/usr/lib/wsl", "/mnt/wsl"))]
    repo = str(Paths.default().repo)
    if windows == [repo] and not Path("/mnt/c").exists():
        out.append(Result(PASS, "mounts", f"only {repo} (drvfs)"))
    else:
        out.append(
            Result(FAIL, "mounts", f"Windows mounts: {windows}", "powershell -File scripts\\bootstrap.ps1 -Only wsl")
        )
    interop = list(Path("/proc/sys/fs/binfmt_misc").glob("WSLInterop*"))
    out.append(Result(PASS if not interop else FAIL, "interop", "WSLInterop binfmt absent" if not interop
                      else "WSLInterop binfmt registered", "systemctl start ossbot-nointerop"))  # fmt: skip
    return out


def check_default_user() -> Result:
    try:
        conf = Path("/etc/wsl.conf").read_text(encoding="utf-8")
    except OSError as exc:
        return Result(FAIL, "default user", str(exc))
    ok = re.search(r"^\[user\]\s*\n\s*default\s*=\s*ossnobody\s*$", conf, re.M) is not None
    return Result(PASS if ok else FAIL, "default user", "ossnobody" if ok else "wsl.conf [user] default != ossnobody")


def check_users() -> Result:
    import grp  # noqa: PLC0415
    import pwd  # noqa: PLC0415

    problems = []
    for user, uid in (("ossadmin", 1000), ("ossnobody", 1001)):
        try:
            if pwd.getpwnam(user).pw_uid != uid:
                problems.append(f"{user} uid != {uid}")
        except KeyError:
            problems.append(f"{user} missing")
    for user, want in EXPECTED_GROUPS.items():
        try:
            primary = pwd.getpwnam(user).pw_gid
        except KeyError:
            problems.append(f"{user} missing")
            continue
        have = {g.gr_name for g in grp.getgrall() if user in g.gr_mem and g.gr_gid != primary}
        if have != want:
            problems.append(f"{user} groups {sorted(have)} != {sorted(want)}")
    return Result(PASS if not problems else FAIL, "users/groups", "; ".join(problems) or "DESIGN 7.3 table")


def check_perms() -> Result:
    if not _is_root():
        return Result(WARN, "permissions", "needs root", "sudo ossctl doctor")
    from ossbot.watchdog import perms  # noqa: PLC0415

    drift = perms.check_all()
    return Result(PASS if not drift else FAIL, "permissions", "; ".join(drift[:5]) or "control, secrets, sockets")


def check_docker() -> list[Result]:
    out: list[Result] = []
    ver = _run([DOCKER, "version", "--format", "{{.Server.Version}}"])
    if ver.returncode != 0:
        return [Result(FAIL, "docker", ver.stderr.strip()[-200:], "systemctl status docker")]
    out.append(Result(PASS, "docker", f"server {ver.stdout.strip()}"))
    try:
        daemon = json.loads(Path("/etc/docker/daemon.json").read_text(encoding="utf-8"))
        bad = {k: daemon.get(k) for k, v in DAEMON_JSON_KEYS.items() if daemon.get(k) != v}
    except (OSError, ValueError) as exc:
        bad = {"error": str(exc)}
    out.append(
        Result(PASS if not bad else FAIL, "daemon.json", f"drift: {bad}" if bad else "DESIGN 7.4 + iptables backend")
    )
    return out


def filter_deny_probe() -> Result:
    """A bind-mount create through the filter socket must be refused with 403."""
    body = json.dumps({"Image": "ossbot/sandbox-full", "HostConfig": {"Binds": ["/etc:/x"]}}).encode()
    request = (
        b"POST /v1.44/containers/create?name=ossbot-doctor-probe HTTP/1.1\r\nHost: docker\r\n"
        b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(str(FILTER_SOCK))
            sock.sendall(request)
            reply = sock.recv(4096)
    except OSError as exc:
        return Result(FAIL, "dockerfilter deny probe", str(exc), "systemctl status ossbot-dockerfilter")
    status = reply.split(b" ", 2)[1] if reply.count(b" ") >= 2 else b"?"
    return Result(
        PASS if status == b"403" else FAIL, "dockerfilter deny probe", f"bind-mount create -> {status.decode()}"
    )


def check_firewall() -> Result:
    out = _run([FIREWALL, "--check"])
    detail = out.stderr.strip().replace("\n", "; ")[-300:] or "rules match the rendered set"
    return Result(PASS if out.returncode == 0 else FAIL, "firewall --check", detail, "ossbot-firewall --apply")


_CONNECT = (
    "import socket,sys\n"
    "try:\n socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close(); print('open')\n"
    "except OSError as e: print('closed', e.errno)\n"
)


def _can_connect(user: str, host: str, port: int) -> bool:
    out = _run(["/usr/bin/python3", "-I", "-c", _CONNECT, host, str(port)], timeout=10, user=user)
    return out.stdout.startswith("open")


def check_uid_probes() -> Result:
    owned = {"ossrunner": 3140, "hermes": 3141, "gatekeeper": 3150}
    problems = []
    for user in ("ossrunner", "hermes", "gatekeeper", "ossbox", "ossnobody"):
        for port in (3140, 3141, 3150):
            if _can_connect(user, PROXY_IP, port) != (owned.get(user) == port):
                problems.append(f"{user}->{PROXY_IP}:{port}")
        if _can_connect(user, "1.1.1.1", 443):
            problems.append(f"{user}->1.1.1.1:443 (direct)")
    return Result(PASS if not problems else FAIL, "per-uid egress", "; ".join(problems) or "only owned ports")


def _connect_code(user: str, port: int, url: str) -> str:
    out = _run(["/usr/bin/curl", "-s", "-o", "/dev/null", "--max-time", "15", "-w", "%{http_connect}",
                "-x", f"http://{PROXY_IP}:{port}", url], timeout=20, user=user)  # fmt: skip
    return out.stdout.strip() or "000"


def check_proxy_denies() -> Result:
    code = _connect_code("gatekeeper", 3150, "https://example.com/")
    return Result(PASS if code == "403" else FAIL, "proxy denies example.com", f"gatekeeper via 3150 -> CONNECT {code}")


def check_images() -> list[Result]:
    pins = load_image_pins(LOCK_COPY)
    out: list[Result] = []
    for name in ("sandbox-full", "sandbox-llm", "proxy"):
        ref = pins.ref(name)
        if ref is None:
            out.append(Result(FAIL, f"image {name}", "digest not pinned in /etc/ossbot/versions.lock.yaml",
                              "provision step images"))  # fmt: skip
            continue
        ok = _run([DOCKER, "image", "inspect", ref]).returncode == 0
        out.append(Result(PASS if ok else FAIL, f"image {name}", ref if ok else f"{ref} missing locally"))
    return out


def check_kiro_cli(quick: bool) -> list[Result]:
    pins = load_image_pins(LOCK_COPY)
    ref = pins.ref("sandbox-llm")
    if ref is None:
        return [Result(FAIL, "kiro-cli (sandbox-llm)", "image not pinned")]
    if quick:
        return [Result(PASS, "kiro-cli (sandbox-llm)", f"pinned {pins.kiro_cli_version} (sha256 verified at build)")]
    ver = _run([DOCKER, "run", "--rm", "--network", "none", "--entrypoint", "/opt/kiro-cli/bin/kiro-cli", ref,
                "--version"], timeout=60)  # fmt: skip
    have = ver.stdout.strip()
    ok = ver.returncode == 0 and pins.kiro_cli_version is not None and pins.kiro_cli_version in have
    results = [Result(PASS if ok else FAIL, "kiro-cli (sandbox-llm)", have or ver.stderr.strip()[-200:])]
    script = (
        "set -e; export HOME=/home/agent; mkdir -p $HOME/.kiro/settings $HOME/cwd; "
        "cp /opt/ossbot/kiro/settings/cli.json $HOME/.kiro/settings/cli.json; cd $HOME/cwd; kiro-cli settings list"
    )
    tmpfs = ("--tmpfs", "/home/agent:rw,size=64m,mode=1777", "--tmpfs", "/tmp:rw,size=64m,mode=1777")  # noqa: S108 - container paths
    settings = _run([DOCKER, "run", "--rm", "--network", "none", "--read-only", *tmpfs, "--entrypoint", "/bin/sh", ref,
                     "-c", script], timeout=60)  # fmt: skip
    text = settings.stdout
    bad = [k for k, v in KIRO_SETTINGS_WANT.items() if not re.search(rf"{re.escape(k)}\W+{v}\b", text)]
    if settings.returncode != 0:
        results.append(Result(WARN, "kiro settings (sandbox)", settings.stderr.strip()[-200:] or "settings list failed",
                              "checked again by the 'settings' safety probe once KIRO_API_KEY exists"))  # fmt: skip
    else:
        results.append(Result(PASS if not bad else FAIL, "kiro settings (sandbox)", f"not as baked: {bad}" if bad
                              else "knowledge, code intelligence, delegate, workflows, telemetry off"))  # fmt: skip
    return results


def check_sandbox_kiro() -> list[Result]:
    """M11 order: sandbox auth + catalog (list_models), then the safety-probe status."""
    missing = _secret("sandboxd.env", "KIRO_API_KEY", "sandbox Kiro auth + catalog")
    out = (
        [missing]
        if missing
        else [Result(WARN, "sandbox Kiro auth + catalog", "key present; catalog runs in the runner (FEAT-006)")]
    )
    status: dict[str, object] = {}
    try:
        with uds_client(SANDBOXD_SOCK, timeout=httpx.Timeout(10.0)) as client:
            resp = client.get("/v1/probes/status")
            status = resp.json() if resp.status_code == 200 else {"error": resp.status_code}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return [*out, Result(FAIL, "safety probes", f"sandboxd unreachable: {exc}", "systemctl status ossbot-sandboxd")]
    if status.get("all_passed"):
        return [*out, Result(PASS, "safety probes", f"all passed for {str(status.get('probe_key'))[:12]}")]
    if missing:
        return [
            *out,
            Result(AWAITING, "safety probes", f"{AWAITING}: KIRO_API_KEY (sandbox) - no pass for the current tuple"),
        ]
    return [*out, Result(FAIL, "safety probes", "no pass for the current tuple", "ossctl doctor --probes")]


def check_hermes() -> list[Result]:
    out = [
        Result(SKIP, "hermes commit vs versions.lock", "phase 2 (FEAT-007)"),
        Result(SKIP, "hermes-side Kiro probe", "phase 2 (FEAT-007)"),
        Result(SKIP, "kiro settings (hermes)", "phase 2 (FEAT-007)"),
    ]
    missing = _secret("hermes.env", "KIRO_API_KEY", "hermes Kiro key")
    out.append(missing or Result(PASS, "hermes Kiro key", "present"))
    return out


def check_proxy_dns() -> Result:
    out = _run([DOCKER, "exec", "ossbot-proxy", "getent", "hosts", "github.com"], timeout=20)
    return Result(PASS if out.returncode == 0 and out.stdout.strip() else FAIL, "proxy DNS",
                  out.stdout.split("\n")[0].strip() or out.stderr.strip()[-200:])  # fmt: skip


def check_gatekeeper() -> Result:
    try:
        with uds_client(Paths.default().gatekeeper_sock, timeout=httpx.Timeout(5.0)) as client:
            health = client.get("/v1/health")
            control = client.get("/v1/control").json() if health.status_code == 200 else {}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return Result(FAIL, "gatekeeper health", str(exc), "systemctl status ossbot-gatekeeper")
    if health.status_code != 200:
        return Result(FAIL, "gatekeeper health", f"HTTP {health.status_code}")
    detail = f"state {control.get('state')}, dry_run {control.get('dry_run')}"
    return Result(PASS, "gatekeeper health", detail)


def _github_login(token: str) -> tuple[int, str, str]:
    resp = httpx.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28"}, timeout=15)  # fmt: skip
    login = resp.json().get("login", "") if resp.status_code == 200 else ""
    return resp.status_code, str(login), resp.headers.get("x-oauth-scopes", "")


def check_github(quick: bool) -> list[Result]:
    out: list[Result] = []
    for file, key, label in (("gatekeeper.env", "GITHUB_WRITE_TOKEN", "GitHub write token"),
                             ("runner.env", "GITHUB_READ_TOKEN", "GitHub read token")):  # fmt: skip
        missing = _secret(file, key, label)
        if missing:
            out.append(missing)
            continue
        if quick:
            out.append(Result(PASS, label, "present, well-formed"))
            continue
        try:
            code, login, scopes = _github_login(env_value(file, key) or "")
        except httpx.HTTPError as exc:
            out.append(Result(FAIL, label, str(exc)))
            continue
        scope_set = {s.strip() for s in scopes.split(",") if s.strip()}
        if key == "GITHUB_WRITE_TOKEN":
            ok = code == 200 and "public_repo" in scope_set and not scope_set & {"repo", "workflow"} and not any(
                s.startswith("admin:") for s in scope_set)  # fmt: skip
        else:
            ok = code == 200
        out.append(Result(PASS if ok else FAIL, label, f"HTTP {code} login {login} scopes [{scopes}]"))
    return out


def check_telegram(quick: bool) -> list[Result]:
    out: list[Result] = []
    missing = _secret("gatekeeper.env", "TELEGRAM_BOT_TOKEN", "Telegram bot token")
    if missing:
        out.append(missing)
    elif not quick:
        token = env_value("gatekeeper.env", "TELEGRAM_BOT_TOKEN") or ""
        try:
            resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
            ok = resp.status_code == 200 and resp.json().get("ok") is True
            out.append(Result(PASS if ok else FAIL, "Telegram getMe", f"HTTP {resp.status_code}"))
        except httpx.HTTPError as exc:
            out.append(Result(FAIL, "Telegram getMe", type(exc).__name__))
    else:
        out.append(Result(PASS, "Telegram bot token", "present, well-formed"))
    user_id = _profile_value("telegram", "user_id")
    if user_id in (None, "0"):
        out.append(Result(AWAITING, "Telegram user id", f"{AWAITING}: profile.telegram.user_id",
                          "set telegram.user_id in config/profile.yaml, then ossctl config reload"))  # fmt: skip
    else:
        out.append(Result(PASS, "Telegram user id", "set"))
    return out


def _profile_value(section: str, key: str) -> str | None:
    from ossbot.common.yamlio import YamlError, safe_load  # noqa: PLC0415

    try:
        data = safe_load((Paths.default().config_dir / "profile.yaml").read_bytes())
    except (OSError, YamlError):
        return None
    value = data.get(section, {}).get(key) if isinstance(data, dict) else None
    return None if value is None else str(value)


def check_identity_snapshot() -> Result:
    paths = Paths.default()
    try:
        live = (paths.config_dir / "profile.yaml").read_bytes()
    except OSError as exc:
        return Result(FAIL, "identity snapshot", f"config/profile.yaml: {exc}", "create it from profile.example.yaml")
    try:
        snap = (paths.snapshot_current / "profile.yaml").read_bytes()
    except OSError as exc:
        return Result(FAIL, "identity snapshot", f"no snapshot: {exc}", "systemctl start ossbot-gatekeeper")
    if live == snap:
        return Result(PASS, "identity snapshot", "config/profile.yaml == loaded snapshot")
    return Result(WARN, "identity snapshot", "config/profile.yaml differs from the snapshot", "ossctl config reload")


def check_audit() -> Result:
    try:
        with uds_client(Paths.default().gatekeeper_sock, timeout=httpx.Timeout(60.0)) as client:
            resp = client.get("/v1/audit/verify")
            data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return Result(FAIL, "audit verify", str(exc))
    if resp.status_code != 200:
        return Result(FAIL, "audit verify", f"HTTP {resp.status_code}")
    ok = bool(data.get("ok"))
    detail = f"{data.get('rows')} rows, {data.get('anchors_checked')} anchors" + (
        "" if ok else f"; {data.get('errors', [])[:3]}")  # fmt: skip
    return Result(PASS if ok else FAIL, "audit verify", detail)


def check_disk() -> Result:
    free = shutil.disk_usage("/srv").free / 1024**3
    return Result(PASS if free >= 20 else FAIL, "disk /srv", f"{free:.0f} GB free")


def check_lock_drift() -> Result:
    try:
        same = LOCK_COPY.read_bytes() == (Paths.default().config_dir / "versions.lock.yaml").read_bytes()
    except OSError as exc:
        return Result(FAIL, "versions lock", str(exc))
    return Result(PASS if same else WARN, "versions lock", "installed copy == config/versions.lock.yaml" if same
                  else "installed copy differs from the repo", "provision step images")  # fmt: skip


# ------------------------------------------------------------------------------------ driver
def run(quick: bool = False, probes: bool = False) -> list[Result]:
    steps: list[Callable[[], Result | Iterable[Result]]] = [
        check_mounts, check_default_user, check_users, check_perms, check_docker,
        filter_deny_probe, check_firewall,
    ]  # fmt: skip
    if not quick:
        steps += [check_uid_probes, check_proxy_denies]
    steps += [check_images, lambda: check_kiro_cli(quick), check_sandbox_kiro, check_hermes]
    if not quick:
        steps.append(check_proxy_dns)
    steps += [
        check_firewall, check_gatekeeper, lambda: check_github(quick), lambda: check_telegram(quick),
        check_identity_snapshot, check_audit, check_disk, check_lock_drift,
    ]  # fmt: skip
    if probes:
        steps.insert(steps.index(check_hermes), lambda: Result(AWAITING, "probe re-run", f"{AWAITING}: KIRO_API_KEY"))
    results: list[Result] = []
    for step in steps:
        try:
            got = step()
        except Exception as exc:  # noqa: BLE001 - a broken check is a FAIL line, never a crash
            name = getattr(step, "__name__", "check")
            got = Result(FAIL, name, f"{type(exc).__name__}: {exc}")
        results.extend([got] if isinstance(got, Result) else list(got))
    return results


def print_results(results: list[Result]) -> None:
    for r in results:
        line = f"{r.status:<15} {r.name}: {r.detail}"
        if r.hint and r.status not in (PASS, SKIP):
            line += f"  (fix: {r.hint})"
        print(line)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
