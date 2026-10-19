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
from typing import Any

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
    # WSL's interop sockets (/run/WSL/<pid>_interop, 0777) stay root-only (egress threat table).
    run_wsl = Path("/run/WSL")
    mode = run_wsl.stat().st_mode & 0o777 if run_wsl.is_dir() else 0
    out.append(Result(PASS if not mode & 0o077 else FAIL, "interop sockets", f"/run/WSL mode {mode:o}",
                      "systemctl restart ossbot-nointerop"))  # fmt: skip
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


_DNS_QUERY = (
    "import socket,sys\n"
    "q = b'\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x07example\\x03com\\x00\\x00\\x01\\x00\\x01'\n"
    "try:\n"
    " if sys.argv[2] == 'udp':\n"
    "  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q, (sys.argv[1], 53))\n"
    " else:\n"
    "  s = socket.create_connection((sys.argv[1], 53), 3); s.sendall(len(q).to_bytes(2, 'big') + q)\n"
    " print('answer' if s.recv(512) else 'none')\n"
    "except OSError as e: print('none', e.errno)\n"
)


def _dns_answers(user: str, server: str, proto: str) -> bool:
    out = _run(["/usr/bin/python3", "-I", "-c", _DNS_QUERY, server, proto], timeout=10, user=user)
    return out.stdout.startswith("answer")


_VARLINK_RESOLVE = (
    "import json,socket,sys\n"
    "q = {'method': 'io.systemd.Resolve.ResolveHostname', 'parameters': {'name': 'example.com'}}\n"
    "try:\n"
    " s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5); s.connect(sys.argv[1])\n"
    " s.sendall(json.dumps(q).encode() + b'\\0')\n"
    " print('answer' if s.recv(65536) else 'none')\n"
    "except OSError as e: print('none', e.errno)\n"
)
BUSCTL_RESOLVE = [
    "/usr/bin/busctl", "--system", "--timeout=5", "call", "org.freedesktop.resolve1", "/org/freedesktop/resolve1",
    "org.freedesktop.resolve1.Manager", "ResolveHostname", "isit", "0", "example.com", "2", "0",
]  # fmt: skip


def _resolved_ipc_answers(user: str) -> list[str]:
    """P1-6: systemd-resolved IPC channels that answer ``user`` (varlink and D-Bus)."""
    from ossbot.ctl.render import RESOLVED_VARLINK  # noqa: PLC0415

    out = []
    varlink = _run(["/usr/bin/python3", "-I", "-c", _VARLINK_RESOLVE, str(RESOLVED_VARLINK)], timeout=10, user=user)
    if varlink.stdout.startswith("answer"):
        out.append(f"{user}->systemd-resolved varlink")
    if _run(BUSCTL_RESOLVE, timeout=10, user=user).returncode == 0:
        out.append(f"{user}->systemd-resolved D-Bus")
    return out


def check_resolved() -> Result:
    """P1-6: systemd-resolved masked and not listening (also part of ``ossbot-firewall --check``)."""
    from ossbot.ctl.render import resolved_ipc_problems  # noqa: PLC0415

    problems = [p.removeprefix("firewall_drift:resolved_ipc ") for p in resolved_ipc_problems()]
    return Result(
        PASS if not problems else FAIL, "systemd-resolved masked", "; ".join(problems) or "masked, varlink closed",
        "provision step firewall (systemctl mask systemd-resolved)",
    )  # fmt: skip


LINGER_DIR = Path("/var/lib/systemd/linger")
RUN_USER = Path("/run/user")


# The uids that run (or will run) ossbot units (DESIGN §7.3). ossnobody is left out: it is the default
# WSL login, so WSL gives it a session and /run/user/1001 by design, and no unit runs as it.
UNIT_UIDS = ("ossrunner", "hermes", "gatekeeper", "ossbox")


def check_no_user_managers(linger_dir: Path = LINGER_DIR, run_user: Path = RUN_USER) -> Result:
    """P1-17: no service uid lingers or has a /run/user runtime dir (no systemd --user instance)."""
    import pwd  # noqa: PLC0415

    problems = []
    for user in UNIT_UIDS:
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            problems.append(f"{user} missing")
            continue
        if (linger_dir / user).exists():
            problems.append(f"{user} lingers")
        if (run_user / str(uid)).exists():
            problems.append(f"/run/user/{uid} ({user}) exists")
    return Result(PASS if not problems else FAIL, "service uids: no user manager", "; ".join(problems)
                  or "Linger=no and no /run/user/<uid> for the four unit uids",
                  "loginctl disable-linger <user>; loginctl terminate-user <user>")  # fmt: skip


def check_io_uring() -> Result:
    """P1-14: io_uring off for every process (also part of ``ossbot-firewall --check``)."""
    from ossbot.ctl.render import io_uring_problems  # noqa: PLC0415

    problems = [p.removeprefix("firewall_drift:io_uring ") for p in io_uring_problems()]
    return Result(
        PASS if not problems else FAIL, "io_uring disabled", "; ".join(problems) or "kernel.io_uring_disabled = 2",
        "provision step firewall (/etc/sysctl.d/60-ossbot.conf)",
    )  # fmt: skip


def dns_probe_targets() -> list[str]:
    """Every resolv.conf nameserver plus the systemd-resolved stub address (P1-1)."""
    from ossbot.ctl.render import RenderError, nameservers  # noqa: PLC0415

    try:
        servers = nameservers()
    except (OSError, RenderError):
        servers = []
    return list(dict.fromkeys([*servers, "127.0.0.53"]))


def check_uid_probes() -> Result:
    owned = {"ossrunner": 3140, "hermes": 3141, "gatekeeper": 3150}
    problems = []
    resolvers = dns_probe_targets()
    for user in ("ossrunner", "hermes", "gatekeeper", "ossbox", "ossnobody"):
        for port in (3140, 3141, 3150):
            if _can_connect(user, PROXY_IP, port) != (owned.get(user) == port):
                problems.append(f"{user}->{PROXY_IP}:{port}")
        if _can_connect(user, "1.1.1.1", 443):
            problems.append(f"{user}->1.1.1.1:443 (direct)")
        for server in ["1.1.1.1", *resolvers]:
            for proto in ("udp", "tcp"):
                if _dns_answers(user, server, proto):
                    problems.append(f"{user}->{server}:53/{proto} (direct DNS)")
        problems.extend(_resolved_ipc_answers(user))
    return Result(
        PASS if not problems else FAIL, "per-uid egress",
        "; ".join(problems) or "only owned ports, no direct DNS, no resolved IPC",
    )  # fmt: skip


def _connect_code(user: str, port: int, url: str) -> str:
    out = _run(["/usr/bin/curl", "-s", "-o", "/dev/null", "--max-time", "15", "-w", "%{http_connect}",
                "-x", f"http://{PROXY_IP}:{port}", url], timeout=20, user=user)  # fmt: skip
    return out.stdout.strip() or "000"


def check_proxy_denies() -> Result:
    code = _connect_code("gatekeeper", 3150, "https://example.com/")
    # P1-11: an IP-literal target is denied before any name ACL (hermes has web_public in egress
    # modes research and open, so 3141 shows the explicit ip_literal deny, not "no category matched").
    literal = _connect_code("hermes", 3141, "https://203.0.113.7/")
    ok = code == "403" and literal == "403"
    detail = f"gatekeeper via 3150 -> example.com CONNECT {code}; hermes via 3141 -> 203.0.113.7 CONNECT {literal}"
    return Result(PASS if ok else FAIL, "proxy denies example.com", detail)


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


def _catalog_rows() -> list[dict[str, Any]] | None:
    try:
        with uds_client(Paths.default().gatekeeper_sock, timeout=httpx.Timeout(10.0)) as client:
            resp = client.get("/v1/models/catalog")
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    rows = resp.json()
    return rows if isinstance(rows, list) else None


def _run_probes(rows: list[dict[str, Any]]) -> Result:
    """M11 step 3 from doctor: the five probes with the models resolve() picks for planner and coder."""
    from ossbot.common.models import LIVE_TEST_ROLE, Catalog, ModelsFile, ModelUnavailable  # noqa: PLC0415

    try:
        models_file = ModelsFile(Paths.default().config_dir / "models.yaml")
        cat = Catalog.from_rows(rows)
        test_model = models_file.resolve(LIVE_TEST_ROLE, cat).model  # roles.test: live checks never prompt
        models = dict.fromkeys(("planner", "coder"), test_model)  # the probe agents; enforcement is the test
    except (ModelUnavailable, OSError, ValueError) as exc:
        return Result(FAIL, "probe re-run", f"model_unavailable: {exc}", "ossctl model set <role> <catalog id>")
    try:
        with uds_client(SANDBOXD_SOCK, timeout=httpx.Timeout(5400.0, connect=5.0)) as client:
            resp = client.post("/v1/probes/run", json={"models": models})
    except (httpx.HTTPError, OSError) as exc:
        return Result(FAIL, "probe re-run", f"sandboxd: {type(exc).__name__}", "systemctl status ossbot-sandboxd")
    if resp.status_code != 200:
        return Result(FAIL, "probe re-run", f"sandboxd answered {resp.status_code}")
    results = resp.json().get("results", {})
    failed = sorted(p for p, passed in results.items() if passed is not True)  # ProbeRunner: {probe: bool}
    if not results:
        failed = ["(no results)"]
    detail = f"{LIVE_TEST_ROLE}={test_model} (planner and coder agents)"
    if failed:
        return Result(FAIL, "probe re-run", f"{detail}; failed: {', '.join(failed)}", "journalctl -u ossbot-sandboxd")
    return Result(PASS, "probe re-run", f"{detail}; all five passed")


def check_sandbox_kiro(probes: bool = False) -> list[Result]:
    """M11 order: sandbox auth + catalog (the runner's list_models job fills it), then with
    ``--probes`` the probe run for the resolved models, then the safety-probe status."""
    missing = _secret("sandboxd.env", "KIRO_API_KEY", "sandbox Kiro auth + catalog")
    rows = None if missing else _catalog_rows()
    if missing:
        out = [missing]
    elif rows is None:
        out = [Result(FAIL, "sandbox Kiro auth + catalog", "gatekeeper catalog unreadable", "ossctl status")]
    elif not [r for r in rows if not r.get("forced")]:
        out = [Result(FAIL, "sandbox Kiro auth + catalog", "catalog_empty: the runner's list_models has not filled it",
                      "journalctl -u ossbot-runner; ossctl models refresh")]  # fmt: skip
    else:
        out = [Result(PASS, "sandbox Kiro auth + catalog", f"{len(rows)} models (list_models with the sandbox key)")]
    if probes:
        if missing:
            out.append(Result(AWAITING, "probe re-run", f"{AWAITING}: KIRO_API_KEY"))
        elif out[0].status != PASS or rows is None:
            out.append(Result(FAIL, "probe re-run", "not run: no catalog yet (M11 order)"))
        else:
            out.append(_run_probes(rows))
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
    recorded = status.get("probes")
    if isinstance(recorded, dict) and not any(v is not None for v in recorded.values()):
        # Never run for this tuple (they need catalog models, FEAT-006); Kiro jobs stay refused.
        return [*out, Result(WARN, "safety probes", "not run yet for the current tuple; Kiro jobs refused until "
                             "they pass", "ossctl doctor --probes (after the model catalog exists)")]  # fmt: skip
    return [*out, Result(FAIL, "safety probes", "no pass for the current tuple", "ossctl doctor --probes")]


HERMES_HOME_DIR = Path("/home/hermes")
HERMES_API = "http://127.0.0.1:8642"


def _lock_scalar(section: str, key: str, path: Path = LOCK_COPY) -> str | None:
    """A scalar of a top-level section of versions.lock.yaml (flat 2-space layout)."""
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = line[:-1]
        elif current == section and line.startswith(f"  {key}:"):
            return line.split(":", 1)[1].split(" #", 1)[0].strip().strip('"')
    return None


def check_hermes_install() -> list[Result]:
    """§15.4: the checked-out commit (read as data, root never runs git in the hermes tree), the
    hermes-side Kiro files (§15.2) and the root-owned paths inside the hermes home (§15.5)."""
    from ossbot.ctl import hermes_probe as hp  # noqa: PLC0415

    out: list[Result] = []
    want = _lock_scalar("hermes", "peeled_commit") or ""
    try:
        head_path = HERMES_HOME_DIR / ".hermes/hermes-agent/.git/HEAD"
        fd = os.open(head_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            head = fh.read(80).decode("ascii", "replace").strip()
        out.append(Result(PASS if head == want else FAIL, "hermes commit vs versions.lock",
                          f"{head[:12]} (lock {want[:12]})", "ossctl hermes upgrade"))  # fmt: skip
    except OSError as exc:
        out.append(Result(FAIL, "hermes commit vs versions.lock", f"not installed: {exc.strerror}",
                          "provision step hermes"))  # fmt: skip
    repo = Paths.default().repo
    pairs = (
        (HERMES_HOME_DIR / ".kiro/settings/cli.json", repo / "sandbox/kiro/settings/cli.json"),
        (HERMES_HOME_DIR / ".kiro/settings/permissions.yaml", repo / "hermes/config/permissions.yaml"),
        (HERMES_HOME_DIR / ".kiro/agents/ossbot-chat.json", repo / ".kiro/agents/ossbot-chat.json"),
        (HERMES_HOME_DIR / ".hermes/.env", repo / "hermes/config/dotenv"),
    )
    bad: list[str] = []
    for deployed, source in pairs:
        try:
            if hp.file_sha256(deployed) != hp.file_sha256(source):
                bad.append(f"{deployed.name} differs")
        except (OSError, hp.Drift) as exc:
            bad.append(f"{deployed.name}: {exc}")
    for path in (
        HERMES_HOME_DIR / ".hermes/skills",
        HERMES_HOME_DIR / ".hermes/hooks",
        HERMES_HOME_DIR / ".hermes/.env",
    ):
        try:
            st = os.lstat(path)
            if st.st_uid != 0 or st.st_mode & 0o022:
                bad.append(f"{path.name} is not root-owned read-only")
        except OSError:
            bad.append(f"{path.name} missing")
    out.append(Result(FAIL if bad else PASS, "kiro settings (hermes)", "; ".join(bad) or
                      "cli.json, permissions.yaml, ossbot-chat agent, .env, skills/hooks root-owned",
                      "provision steps kiro-cli-hermes,hermes"))  # fmt: skip
    try:
        from ossbot.ctl import skills_root  # noqa: PLC0415

        problems = skills_root.drift()
        out.append(Result(FAIL if problems else PASS, "hermes skills (active vs approved)",
                          "; ".join(problems)[:300] or "every active skill matches its approval",
                          "investigate: only root activates approved skills (§15.5)"))  # fmt: skip
    except (OSError, ValueError) as exc:
        out.append(Result(FAIL, "hermes skills (active vs approved)", str(exc)[:200], "provision step hermes"))
    try:
        from ossbot.ctl.hermes_render import MANAGED_CONFIG, render, tier_from_flag  # noqa: PLC0415

        tier = tier_from_flag()
        same = MANAGED_CONFIG.read_text(encoding="utf-8") == render(tier)
        out.append(Result(PASS if same else FAIL, "hermes managed config", f"/etc/hermes/config.yaml, tier {tier}",
                          "python -I -m ossbot.ctl.hermes_render"))  # fmt: skip
    except OSError as exc:
        out.append(Result(FAIL, "hermes managed config", str(exc), "provision step hermes"))
    return out


def check_hermes_surface() -> Result:
    """§15.1 runtime check: ``GET /v1/toolsets`` against the active tier's allowlist."""
    from ossbot.ctl.hermes_render import tier_from_flag  # noqa: PLC0415
    from ossbot.gatekeeper.chat.hermes_surface import check_toolsets  # noqa: PLC0415

    key = env_value("gatekeeper.env", "HERMES_API_KEY")
    if not key:
        return Result(AWAITING, "hermes tool surface", "HERMES_API_KEY missing", "provision step secrets-seed")
    tier = tier_from_flag()
    res = check_toolsets(HERMES_API, key, tier)
    status = {"ok": PASS, "unavailable": WARN}.get(res.outcome, FAIL)
    detail = f"{res.outcome}: {res.detail}" + (f"; unexpected {', '.join(res.unexpected)}" if res.unexpected else "")
    return Result(status, "hermes tool surface", detail[:400], "systemctl status ossbot-hermes")


def check_hermes_probe(run_probe: bool = False) -> list[Result]:
    """§15.2 M9: the Hermes-side Kiro probe result (``--probes`` runs it) and the chat relay state."""
    from ossbot.ctl import hermes_probe as hp  # noqa: PLC0415

    doc = hp.probe(force=True) if run_probe else hp.read_result()
    if doc is None:
        probe = Result(WARN, "hermes-side Kiro probe", "not run yet", "ossctl hermes probe")
    else:
        status = {"pass": PASS, "fail": FAIL}.get(str(doc.get("verdict")), WARN)
        probe = Result(status, "hermes-side Kiro probe",
                       f"{doc.get('verdict')}: {str(doc.get('reason'))[:200]} (checked {doc.get('checked_at')})",
                       "ossctl hermes probe")  # fmt: skip
    killed = Paths.default().killed_flag.exists() or Paths.default().paused_flag.exists()
    fresh = hp.relay_open(doc)
    relay = Result(PASS if fresh and not killed else WARN, "chat relay",
                   "enabled" if fresh and not killed else
                   ("disabled: killed or paused" if killed else "disabled: no Hermes-side probe pass younger than 2 h"),
                   "ossctl hermes probe")  # fmt: skip
    return [probe, relay]


def check_hermes(probes: bool = False) -> list[Result]:
    out = check_hermes_install()
    missing = _secret("hermes.env", "KIRO_API_KEY", "hermes Kiro key")
    out.append(missing or Result(PASS, "hermes Kiro key", "present"))
    out.append(check_hermes_surface())
    out.extend(check_hermes_probe(probes))
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


def check_egress_mode() -> Result:
    """§11.5: the active egress mode (the squid render for it is checked by ``firewall --check``)."""
    try:
        with uds_client(Paths.default().gatekeeper_sock, timeout=httpx.Timeout(5.0)) as client:
            resp = client.get("/v1/control/egress-mode")
            data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return Result(FAIL, "egress mode", str(exc), "systemctl status ossbot-gatekeeper")
    if not data:
        return Result(FAIL, "egress mode", f"HTTP {resp.status_code}")
    return Result(PASS, "egress mode", f"{data.get('mode')} (default {data.get('default')})")


def check_squid_verifiable() -> Result:
    """The drift check skips a live squid.conf whose snapshot the installed schema rejects (after a
    deploy, until the gatekeeper and proxy restart); say so instead of passing silently."""
    from ossbot.ctl.render import squid_snapshot_rejected  # noqa: PLC0415

    digest = squid_snapshot_rejected()
    if digest is None:
        return Result(PASS, "squid.conf verifiable", "its snapshot parses with the installed schema")
    detail = f"snapshot {digest[:12]} is rejected by the installed schema; the drift check cannot verify the conf"
    return Result(WARN, "squid.conf verifiable", detail, "systemctl restart ossbot-gatekeeper ossbot-proxy")


def check_hermes_tier() -> Result:
    """§15.5: the active Hermes capability tier and its toolsets (applied to Hermes in FEAT-007)."""
    try:
        with uds_client(Paths.default().gatekeeper_sock, timeout=httpx.Timeout(5.0)) as client:
            resp = client.get("/v1/control/hermes-tier")
            data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return Result(FAIL, "hermes tier", str(exc), "systemctl status ossbot-gatekeeper")
    if not data:
        return Result(FAIL, "hermes tier", f"HTTP {resp.status_code}")
    tools = ", ".join(data.get("toolsets", []))
    return Result(PASS, "hermes tier", f"{data.get('tier')} (default {data.get('default')}): {tools}")


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


GITHUB_MCP_WRAPPER = Path("/opt/ossbot/bin/ossbot-github-mcp")
GITHUB_MCP_PUBLIC_REPO = "github/github-mcp-server"  # any public repository; its root listing is read


def check_github_mcp(quick: bool) -> Result:
    """GHMCP-3/4: the read-only GitHub MCP server as Hermes runs it (uid hermes, squid 3141): the
    startup check (no write tool), and unless ``quick`` ``get_me`` equals ``github_login`` and a public
    repository's root is listed. The token goes only into the child's environment; output is metadata."""
    name = "github mcp (read-only)"
    if not GITHUB_MCP_WRAPPER.is_file():
        return Result(WARN, name, "not installed", "bootstrap -Only provision")
    missing = _secret("hermes.env", "GITHUB_READ_TOKEN", name)
    if missing:
        return missing
    proxy = "http://10.212.0.2:3141"
    env = {"HOME": "/home/hermes", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HTTPS_PROXY": proxy,
           "https_proxy": proxy, "GITHUB_READ_TOKEN": env_value("hermes.env", "GITHUB_READ_TOKEN") or ""}  # fmt: skip
    mode = ["--check"] if quick else ["--doctor", GITHUB_MCP_PUBLIC_REPO]
    try:
        proc = subprocess.run(["/usr/sbin/runuser", "-u", "hermes", "--", str(GITHUB_MCP_WRAPPER), *mode], env=env,
                              cwd="/", capture_output=True, text=True, timeout=90, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(FAIL, name, type(exc).__name__)
    try:
        doc = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict) or not doc:
        return Result(FAIL, name, f"exit {proc.returncode}: {proc.stderr.strip()[-200:]}",
                      "the startup check refused the server or it could not start")  # fmt: skip
    if doc.get("refused"):
        return Result(FAIL, name, f"write tools listed: {', '.join(map(str, doc['refused']))[:200]}")
    if quick:
        return Result(PASS if doc.get("ok") else FAIL, name, f"{doc.get('tools')} read-only tools")
    want = _top_profile_value("github_login")
    login = doc.get("login")
    ok = bool(doc.get("ok")) and isinstance(login, str) and want is not None and login.lower() == want.lower()
    detail = f"get_me {login}, {GITHUB_MCP_PUBLIC_REPO} root {doc.get('entries')} entries"
    return Result(PASS if ok else FAIL, name, detail, "" if ok else "check GITHUB_READ_TOKEN and github_login")


def _top_profile_value(key: str) -> str | None:
    from ossbot.common.yamlio import YamlError, safe_load  # noqa: PLC0415

    try:
        data = safe_load((Paths.default().config_dir / "profile.yaml").read_bytes())
    except (OSError, YamlError):
        return None
    value = data.get(key) if isinstance(data, dict) else None
    return None if value is None else str(value)


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
        filter_deny_probe, check_firewall, check_resolved, check_io_uring, check_no_user_managers,
    ]  # fmt: skip
    if not quick:
        steps += [check_uid_probes, check_proxy_denies]
    steps += [
        check_images,
        lambda: check_kiro_cli(quick),
        lambda: check_sandbox_kiro(probes),
        lambda: check_hermes(probes),
    ]
    if not quick:
        steps.append(check_proxy_dns)
    steps += [
        check_firewall, check_squid_verifiable, check_gatekeeper, check_egress_mode, check_hermes_tier,
        lambda: check_github(quick),
        lambda: check_github_mcp(quick),
        lambda: check_telegram(quick),
        check_identity_snapshot, check_audit, check_disk, check_lock_drift,
    ]  # fmt: skip
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
