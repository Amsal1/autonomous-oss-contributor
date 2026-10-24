"""CR-001: every ossbot service unit gives a graceful stop at least the drain timeout + 30 s, and
systemd's reverse start order stops the workers before the gatekeeper and the proxy."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ossbot.common.config import LifecyclePolicy
from ossbot.common.control_flags import DEFAULT_DRAIN_TIMEOUT_S, EGRESS_MODE_FILE
from ossbot.common.paths import Paths
from ossbot.ctl import cli

UNITS = Path(__file__).resolve().parents[2] / "systemd"
SERVICES = sorted(UNITS.glob("ossbot-*.service"))


def _unit(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|min)?", value.strip())
    assert match, value
    return int(match.group(1)) * (60 if match.group(2) == "min" else 1)


@pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
def test_timeout_stop_covers_the_drain(path: Path) -> None:
    service = _unit(path)["Service"]
    assert _seconds(service["TimeoutStopSec"]) >= LifecyclePolicy().drain_timeout_s + 30


def test_sandboxd_drain_fits_its_stop_timeout() -> None:
    text = (UNITS / "ossbot-sandboxd.service").read_text(encoding="utf-8")
    service = _unit(UNITS / "ossbot-sandboxd.service")["Service"]
    lines = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("Environment=")]
    env = dict(e.split("=", 1) for line in lines for e in line.split() if "=" in e)
    assert int(env["OSSBOT_DRAIN_TIMEOUT_S"]) + 30 <= _seconds(service["TimeoutStopSec"])


def test_policy_drain_timeout_cannot_exceed_the_static_unit_values() -> None:
    """P1-9: TimeoutStopSec, the sandboxd SIGTERM drain and the down.ps1 cut-off are static, so the
    policy may not ask for a longer drain than they cover."""
    assert LifecyclePolicy(drain_timeout_s=DEFAULT_DRAIN_TIMEOUT_S).drain_timeout_s == DEFAULT_DRAIN_TIMEOUT_S
    with pytest.raises(ValidationError):
        LifecyclePolicy(drain_timeout_s=DEFAULT_DRAIN_TIMEOUT_S + 1)
    text = (UNITS / "ossbot-sandboxd.service").read_text(encoding="utf-8")
    assert f"OSSBOT_DRAIN_TIMEOUT_S={DEFAULT_DRAIN_TIMEOUT_S}" in text
    down_ps1 = (UNITS.parent / "scripts" / "down.ps1").read_text(encoding="utf-8")
    assert f"else {{ {DEFAULT_DRAIN_TIMEOUT_S} }}" in down_ps1


NON_ROOT = [p for p in SERVICES if _unit(p)["Service"].get("User", "root") != "root"]


def test_non_root_units_are_known() -> None:
    """The restriction test below must not pass vacuously; Phase 2 runner/hermes units join it."""
    assert {
        "ossbot-gatekeeper.service",
        "ossbot-sandboxd.service",
        "ossbot-runner.service",
        "ossbot-hermes.service",
    } <= {p.name for p in NON_ROOT}


def test_hermes_units_are_wired_everywhere() -> None:
    """FEAT-007 item 35: Hermes (non-root, ProtectHome=tmpfs + the hermes home bound back, P1-17) and
    the root probe socket are enabled by provisioning, stopped by the kill, resumed, watched by system tests."""
    root = UNITS.parent
    service = _unit(UNITS / "ossbot-hermes.service")["Service"]
    text = (UNITS / "ossbot-hermes.service").read_text(encoding="utf-8")
    assert service["User"] == "hermes" and service["ProtectHome"] == "tmpfs"
    assert service["BindPaths"] == "/home/hermes" and service["ProtectSystem"] == "strict"
    assert "ConditionPathExists=!/var/lib/ossbot/control/killed" in text
    assert service["ExecStartPre"] == "+/opt/ossbot/bin/ossbot-firewall --check"
    assert "HERMES_DISABLE_LAZY_INSTALLS=1" in text and "HTTPS_PROXY=http://10.212.0.2:3141" in text
    assert "API_SERVER_HOST=127.0.0.1 API_SERVER_PORT=8642" in text
    # post-v1 item 7: a planned stop exits 0 (Hermes' own stop marker), and real crashes stay visible
    assert service["ExecStop"] == (
        "/home/hermes/.hermes/hermes-agent/.venv/bin/python -m gateway.systemd_stop_mark $MAINPID"
    )
    assert "SuccessExitStatus" not in service
    sock = _unit(UNITS / "ossbot-hermes-probe.socket")["Socket"]
    assert sock["ListenStream"] == "/run/ossbot/hermes-probe.sock" and sock["SocketGroup"] == "gatekeeper"
    assert sock["SocketMode"] == "0660" and sock["Accept"] == "yes" and sock["MaxConnections"] == "1"
    probe = _unit(UNITS / "ossbot-hermes-probe@.service")
    assert probe["Service"].get("User", "root") == "root"
    assert "python -I -m ossbot.ctl.hermes_probe" in probe["Service"]["ExecStart"]
    assert probe["Unit"]["ConditionPathExists"] == "!/var/lib/ossbot/control/killed"
    services_sh = (root / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    enable = services_sh.split("OSSBOT_ENABLE_UNITS=(", 1)[1].split(")", 1)[0]
    assert "ossbot-hermes.service" in enable and "ossbot-hermes-probe.socket" in enable
    assert "ossbot-hermes.service" in (root / "scripts" / "bin" / "ossbot-kill").read_text()
    assert "ossbot-hermes.service" in (root / "scripts" / "bin" / "ossbot-resume").read_text()
    lib = (root / "tests" / "system" / "lib.sh").read_text(encoding="utf-8")
    assert "ossbot-hermes " in lib.split("PHASE1_UNITS=(", 1)[1].split(")", 1)[0]


def test_runner_unit_is_wired_everywhere() -> None:
    """FEAT-006 item 31: enabled by provisioning, stopped by the kill, resumed, watched by system tests."""
    root = UNITS.parent
    service = _unit(UNITS / "ossbot-runner.service")
    assert service["Service"]["User"] == "ossrunner"
    assert "ConditionPathExists=!/var/lib/ossbot/control/killed" in (UNITS / "ossbot-runner.service").read_text()
    assert service["Service"]["ExecStartPre"] == "+/opt/ossbot/bin/ossbot-firewall --check"
    services_sh = (root / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    enable = services_sh.split("OSSBOT_ENABLE_UNITS=(", 1)[1].split(")", 1)[0]
    assert "ossbot-runner.service" in enable
    assert "stop_units ossbot-runner.service" in (root / "scripts" / "bin" / "ossbot-kill").read_text()
    assert "ossbot-runner.service" in (root / "scripts" / "bin" / "ossbot-resume").read_text()
    lib = (root / "tests" / "system" / "lib.sh").read_text(encoding="utf-8")
    assert "ossbot-runner" in lib.split("PHASE1_UNITS=(", 1)[1].split(")", 1)[0]


def test_gc_units_are_wired_everywhere() -> None:
    """DESIGN §18.10 (plan D6): both retention jobs are root oneshots skipped while killed or paused, their
    timers are persistent and enabled and started by provisioning, and the kill stops both services."""
    root = UNITS.parent
    services_sh = (root / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    enable = services_sh.split("OSSBOT_ENABLE_UNITS=(", 1)[1].split(")", 1)[0].split()
    start = services_sh.split("step_enable_start()", 1)[1].split("case $level", 1)[0]
    kill = (root / "scripts" / "bin" / "ossbot-kill").read_text(encoding="utf-8")
    assert "stop_units ossbot-gc.service ossbot-docker-prune.service" in kill
    assert kill.index("stop_units ossbot-gc.service") < kill.index("stop_units ossbot-runner.service")
    ossctl = "/opt/ossbot/app/.venv/bin/ossctl gc"
    for name, exec_start in (
        ("ossbot-gc", ossctl),
        ("ossbot-docker-prune", f"{ossctl} --only build-cache --only fstrim"),
    ):
        text = (UNITS / f"{name}.service").read_text(encoding="utf-8")
        service = _unit(UNITS / f"{name}.service")["Service"]
        assert "ConditionPathExists=!/var/lib/ossbot/control/killed" in text.splitlines()
        assert "ConditionPathExists=!/var/lib/ossbot/control/paused" in text.splitlines()
        assert service["Type"] == "oneshot" and "User" not in service and service["ExecStart"] == exec_start
        assert _seconds(service["TimeoutStartSec"]) == 1800 and service["IOSchedulingClass"] == "idle"
        timer = _unit(UNITS / f"{name}.timer")
        assert timer["Timer"]["Persistent"] == "true" and timer["Timer"]["RandomizedDelaySec"] == "30min"
        assert timer["Install"]["WantedBy"] == "timers.target"
        assert f"{name}.timer" in enable and f"{name}.timer" in start
    assert _unit(UNITS / "ossbot-gc.timer")["Timer"]["OnCalendar"] == "*-*-* 04:30:00"
    assert _unit(UNITS / "ossbot-docker-prune.timer")["Timer"]["OnCalendar"] == "Sun *-*-* 05:00:00"


def test_proxy_rotate_units_are_wired_everywhere() -> None:
    """Open-discovery design §13.5: a daily root oneshot renames access.log and signals squid (rotate=0);
    not gated on kill or pause (rotation keeps every line); timer persistent, enabled and started."""
    root = UNITS.parent
    services_sh = (root / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    enable = services_sh.split("OSSBOT_ENABLE_UNITS=(", 1)[1].split(")", 1)[0].split()
    start = services_sh.split("step_enable_start()", 1)[1].split("case $level", 1)[0]
    bins = services_sh.split("OSSBOT_BINS=(", 1)[1].split(")", 1)[0].split()
    service = _unit(UNITS / "ossbot-proxy-rotate.service")["Service"]
    assert service["Type"] == "oneshot" and "User" not in service
    assert service["ExecStart"] == "/opt/ossbot/bin/ossbot-proxy-rotate"
    assert _seconds(service["TimeoutStopSec"]) == 150
    timer = _unit(UNITS / "ossbot-proxy-rotate.timer")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 00:10:00 UTC" and timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert "ossbot-proxy-rotate.timer" in enable and "ossbot-proxy-rotate.timer" in start
    assert "ossbot-proxy-rotate" in bins
    script = (root / "scripts" / "bin" / "ossbot-proxy-rotate").read_text(encoding="utf-8")
    assert script.startswith("#!/usr/bin/env bash\n") and "\nset -euo pipefail\n" in script
    assert 'docker exec --user proxy "$CONTAINER" squid -k rotate -f /etc/squid/squid.conf' in script
    assert 'mv -- "$LOG" "$target"' in script and "date -u +%Y%m%dT%H%M%SZ" in script
    # Boot race (bot-down-after-boot F1): no rename unless the proxy container is running.
    assert "CONTAINER=${OSSBOT_PROXY_CONTAINER:-ossbot-proxy}" in script
    assert "docker inspect -f '{{.State.Running}}' \"$CONTAINER\"" in script
    guard = script.index("docker inspect")
    assert guard < script.index('mv -- "$LOG" "$target"')
    not_running = script[guard:].split("if [[ $running != true ]]; then", 1)[1].split("fi\n", 1)[0]
    assert "is not running" in not_running and "exit 0" in not_running
    squid = (root / "sandbox" / "proxy" / "squid.conf.j2").read_text(encoding="utf-8")
    assert "access_log stdio:/var/log/squid/access.log logformat=ossbot rotate=0" in squid


def test_journald_dropin_is_time_based_only() -> None:
    """Plan D5: 30 days of journal, no size cap (owner decision); provisioning installs it and restarts journald."""
    path = UNITS / "journald.conf.d" / "ossbot.conf"
    journal = _unit(path)["Journal"]
    assert journal["MaxRetentionSec"] == "30d" and journal["MaxFileSec"] == "1week"
    assert not [k for k in journal if k.endswith(("MaxUse", "KeepFree", "MaxFileSize", "MaxFiles"))]
    services_sh = (UNITS.parent / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    assert "/etc/systemd/journald.conf.d/60-ossbot.conf root root 0644" in services_sh
    assert "systemctl restart systemd-journald" in services_sh


@pytest.mark.parametrize("path", NON_ROOT, ids=lambda p: p.name)
def test_non_root_units_restrict_address_families(path: Path) -> None:
    """P1-13: AF_VSOCK reaches the Windows host (WSL init, the drvfs Plan 9 server) past every IP
    layer, so every non-root unit may open only unix and IP sockets."""
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("RestrictAddressFamilies=")]
    assert lines == ["RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6"]


@pytest.mark.parametrize("path", NON_ROOT, ids=lambda p: p.name)
def test_non_root_units_filter_io_uring(path: Path) -> None:
    """P1-14: IORING_OP_SOCKET creates sockets without socket(2), so the address-family filter above
    never sees them. Every non-root unit refuses the io_uring syscalls (EPERM, so a library that
    probes io_uring falls back instead of dying on SIGSYS), even if kernel.io_uring_disabled is lost."""
    service = _unit(path)["Service"]
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("SystemCallFilter=")]
    assert lines == ["SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register"]
    assert service["SystemCallErrorNumber"] == "EPERM"
    # no 32-bit/x32 ABI either: socketcall(2) would be a second socket entry point past both filters
    assert service["SystemCallArchitectures"] == "native"


@pytest.mark.parametrize("path", sorted(UNITS.glob("ossbot-*.path")), ids=lambda p: p.name)
def test_path_units_trigger_an_existing_service_and_are_enabled(path: Path) -> None:
    unit = _unit(path)["Path"]["Unit"]
    assert (UNITS / unit).is_file(), unit
    services_sh = (UNITS.parent / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    enable = services_sh.split("OSSBOT_ENABLE_UNITS=(", 1)[1].split(")", 1)[0].split()
    assert path.name in enable


def test_egress_path_watches_the_flag_the_gatekeeper_writes() -> None:
    """§11.5: ossbot-egress.path -> ossbot-egress-apply re-renders squid for the flag's mode."""
    watched = _unit(UNITS / "ossbot-egress.path")["Path"]["PathChanged"]
    assert watched == str(Paths.default().control_dir / EGRESS_MODE_FILE)
    apply = _unit(UNITS / "ossbot-egress-apply.service")["Service"]
    assert apply["ExecStart"] == "/opt/ossbot/bin/ossbot-egress-apply" and apply["Type"] == "oneshot"
    services_sh = (UNITS.parent / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    assert "ossbot-egress-apply" in services_sh.split("OSSBOT_BINS=(", 1)[1].split(")", 1)[0]


def test_lifecycle_path_watches_the_request_the_gatekeeper_writes() -> None:
    """PLAN item 42: ossbot-lifecycle.path -> ossbot-lifecycle --request (root) runs Telegram /down, /up."""
    watched = _unit(UNITS / "ossbot-lifecycle.path")["Path"]["PathExists"]
    assert watched == str(Paths.default().lifecycle_request)
    svc = _unit(UNITS / "ossbot-lifecycle.service")["Service"]
    assert svc["ExecStart"] == "/opt/ossbot/bin/ossbot-lifecycle --request" and svc["Type"] == "oneshot"
    assert "User" not in svc  # root: it stops and starts units
    assert _seconds(svc["TimeoutStartSec"]) > 3600  # the longest drain the request may ask for
    # refused requests in a burst must not trip the start limit and leave the path unit failed
    assert _unit(UNITS / "ossbot-lifecycle.service")["Unit"]["StartLimitIntervalSec"] == "0"
    services_sh = (UNITS.parent / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    assert "ossbot-lifecycle" in services_sh.split("OSSBOT_BINS=(", 1)[1].split(")", 1)[0]


def test_proxy_renders_before_its_firewall_check() -> None:
    """--check compares the live squid.conf with the render for the current mode, so the proxy
    renders first (a mode switched while it was stopped would otherwise fail its own start)."""
    pre = [line for line in (UNITS / "ossbot-proxy.service").read_text(encoding="utf-8").splitlines()
           if line.startswith("ExecStartPre=")]  # fmt: skip
    render = next(i for i, line in enumerate(pre) if "ossbot.ctl.render squid" in line)
    check = next(i for i, line in enumerate(pre) if "ossbot-firewall --check" in line)
    assert render < check


def test_proxy_netns_enables_plpmtud() -> None:
    """F1c (build-env design §7): squid originates every upstream TCP connection, so its netns probes
    the path MTU (a black hole below the MSS clamp recovers) next to ip_forward=0."""
    run = (UNITS.parent / "scripts" / "bin" / "ossbot-proxy-run").read_text(encoding="utf-8")
    docker_run = run.split("exec /usr/bin/docker run", 1)[1]
    flags = re.findall(r"--sysctl (\S+)", docker_run)
    assert flags == ["net.ipv4.ip_forward=0", "net.ipv4.tcp_mtu_probing=1"]


def test_host_sysctls_enable_plpmtud_next_to_io_uring() -> None:
    """F1c: the host netns gets tcp_mtu_probing = 1 in the same managed sysctl file as io_uring, and
    provisioning applies it at once when the live value differs."""
    firewall_sh = (UNITS.parent / "scripts" / "lib" / "firewall.sh").read_text(encoding="utf-8")
    conf = firewall_sh.split("ensure_file /etc/sysctl.d/60-ossbot.conf root root 0644 <<'EOF'\n", 1)[1]
    conf = conf.split("\nEOF\n", 1)[0].splitlines()
    assert [line for line in conf if not line.startswith("#")] == [
        "kernel.io_uring_disabled = 2", "net.ipv4.tcp_mtu_probing = 1",
    ]  # fmt: skip
    assert "$(cat /proc/sys/net/ipv4/tcp_mtu_probing 2>/dev/null) != 1" in firewall_sh
    assert "sysctl -q -w net.ipv4.tcp_mtu_probing=1" in firewall_sh


def _services_sh() -> tuple[list[str], str, str]:
    services_sh = (UNITS.parent / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    enable = services_sh.split("OSSBOT_ENABLE_UNITS=(", 1)[1].split(")", 1)[0].split()
    start = services_sh.split("step_enable_start()", 1)[1].split("case $level", 1)[0]
    return enable, start, services_sh


def test_floorlog_unit_shape_and_wiring() -> None:
    """build-env design §4.4: a root reader of the kernel journal, restarted by systemd, gated by the boot
    gate, enabled and started by provisioning (root helpers: always), restarted on a code deploy."""
    unit = _unit(UNITS / "ossbot-floorlog.service")
    service = unit["Service"]
    assert service["Type"] == "simple" and service["Restart"] == "always" and service["RestartSec"] == "5"
    assert _seconds(service["TimeoutStopSec"]) == 150 and "User" not in service
    assert service["ExecCondition"] == "/opt/ossbot/bin/ossbot-boot-gate --allow minimal"
    assert service["ExecStart"] == "/opt/ossbot/app/.venv/bin/python -I -m ossbot.watchdog.floorlog"
    assert "systemd-journald.service" in unit["Unit"]["After"].split()
    enable, start, services_sh = _services_sh()
    assert "ossbot-floorlog.service" in enable and "ossbot-floorlog.service" in start
    restart = services_sh.split("if ((OSSBOT_APP_CHANGED || OSSBOT_UNITS_CHANGED)) || [[ -n $pending ]]; then",
                                1)[1].split("done", 1)[0]  # fmt: skip
    assert "ossbot-floorlog.service" in restart
    # the log dir sandboxd reads (group ossbox), created by step audit-anchors
    assert "ensure_dir /srv/ossbot/logs/floor root ossbox 0750" in services_sh
    assert 'install -o root -g ossbox -m 0640 /dev/null "$floor"' in services_sh


def test_pubip_units_and_the_proxy_floor_directory() -> None:
    """build-env design §4.7: a root oneshot every 60 s (20 s after boot), before the proxy starts; the proxy
    binds the root-only floor directory read-only as a directory (renames stay visible after SIGHUP)."""
    service = _unit(UNITS / "ossbot-pubip.service")["Service"]
    assert service["Type"] == "oneshot" and "User" not in service
    assert service["ExecStart"] == "/opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.pubip"
    assert service["ExecCondition"] == "/opt/ossbot/bin/ossbot-boot-gate --allow minimal"
    assert _seconds(service["TimeoutStartSec"]) == 45 and _seconds(service["TimeoutStopSec"]) == 150
    assert (service["NoNewPrivileges"], service["ProtectHome"], service["PrivateTmp"]) == ("yes", "yes", "yes")
    timer = _unit(UNITS / "ossbot-pubip.timer")["Timer"]
    assert (timer["OnBootSec"], timer["OnUnitActiveSec"], timer["AccuracySec"]) == ("20s", "60s", "5s")
    proxy = _unit(UNITS / "ossbot-proxy.service")["Unit"]
    assert "ossbot-pubip.service" in proxy["Wants"].split() and "ossbot-pubip.service" in proxy["After"].split()
    enable, start, services_sh = _services_sh()
    assert "ossbot-pubip.timer" in enable and "ossbot-pubip.timer" in start
    assert "ensure_dir /etc/ossbot/proxy/floor root root 0755" in services_sh
    run = (UNITS.parent / "scripts" / "bin" / "ossbot-proxy-run").read_text(encoding="utf-8")
    assert '--mount "type=bind,src=$FLOOR,dst=/etc/squid/floor,readonly"' in run
    assert "FLOOR=/etc/ossbot/proxy/floor" in run


@pytest.mark.parametrize("path", NON_ROOT, ids=lambda p: p.name)
def test_non_root_units_protect_home(path: Path) -> None:
    """P1-17: no non-root unit sees /home or /root (yes) or only an empty tmpfs (tmpfs, for units
    that need a private home view, e.g. Hermes in FEAT-007)."""
    assert _unit(path)["Service"].get("ProtectHome") in {"yes", "tmpfs"}


def test_workers_stop_before_gatekeeper_and_proxy() -> None:
    after = _unit(UNITS / "ossbot-sandboxd.service")["Unit"]["After"].split()
    assert "ossbot-gatekeeper.service" in after and "ossbot-proxy.service" in after  # stop = reverse order
    order = [*cli.WORKER_UNITS, cli.WATCHDOG_TIMER, *cli.CORE_UNITS]
    assert order.index("ossbot-sandboxd.service") < order.index("ossbot-gatekeeper.service")
    assert order[-2:] == ["ossbot-gatekeeper.service", "ossbot-proxy.service"]
