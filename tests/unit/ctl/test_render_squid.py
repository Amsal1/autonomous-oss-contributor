"""build-env FEAT-002 (ADR-0025): the squid floors of design §4.3 and the public-address floor files of §4.7.

Five modes, sandbox_full == sandbox_open but the label, the marked sandbox denies and the per-rule floor lines
ahead of the port checks, the split dst ACL, code_web_ok only on pool_fetch's web_public line, the built-ins
unioned so an old snapshot renders the full floors, host addresses from ``ip``, ``render squid --mode``."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from ossbot.common.config import parse_bundle
from ossbot.common.control_flags import CODE_WEB_MODES, EGRESS_MODES, LLM_WEB_MODES
from ossbot.common.snapshot import write_snapshot
from ossbot.ctl import render

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NS = "10.255.255.254"
TEMPLATE = (ROOT / "sandbox" / "proxy" / "squid.conf.j2").read_text(encoding="utf-8")
FLOOR = " !denied_dst_loop !denied_dst"


def _files(egress: bytes | None = None) -> dict[str, bytes]:
    files = {n: (ROOT / "config" / n).read_bytes() for n in ("policy.yaml", "egress.yaml", "targets.yaml")}
    files["profile.yaml"] = (ROOT / "tests" / "fixtures" / "profile.test.yaml").read_bytes()
    if egress is not None:
        files["egress.yaml"] = egress
    return files


def _bundle() -> render.ConfigBundle:
    return parse_bundle(_files())


def _old_egress() -> bytes:
    """egress.yaml as a pre-ADR-0025 snapshot has it: no sandbox_full, code_denied, notify_only, IPv6 additions."""
    text = (ROOT / "config" / "egress.yaml").read_text(encoding="utf-8")
    start = text.index("  # ADR-0025: the same grants as sandbox_open")
    text = text[:start] + text[text.index("# Per-job egress limits") :]
    text = text[: text.index("# ADR-0025: names no repo-code")]
    text = re.sub(r"\n  # ADR-0025: the one always-denied name.*?notify_only: \[[^\]]*\]\n", "\n", text, flags=re.S)
    return text.replace(', "ff00::/8", "64:ff9b::/96", "2002::/16"', "").encode()


def _squid(mode: str, bundle: render.ConfigBundle | None = None, hosts: Sequence[str] = ()) -> str:
    return render.render_squid(bundle or _bundle(), "0" * 64, [NS], TEMPLATE, mode=mode, host_cidrs=hosts)  # type: ignore[arg-type]


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ------------------------------------------------------------------------------- modes (AC 2)
def test_five_modes_render_and_sandbox_full_is_sandbox_open_but_the_label() -> None:
    texts = {m: _squid(m) for m in EGRESS_MODES}
    assert (
        texts["sandbox_full"].replace("# Egress mode: sandbox_full ", "# Egress mode: sandbox_open ", 1)
        == (texts["sandbox_open"])
    )
    for mode, text in texts.items():
        web = "http_access allow port_sandbox pool_fetch code_web_ok all" + FLOOR
        assert (web in _lines(text)) is (mode in CODE_WEB_MODES), mode
        assert "pool_strict all" not in text and "pool_strict code_web_ok" not in text


def test_llm_pool_is_identical_in_every_llm_web_mode() -> None:
    def llm(text: str) -> list[str]:
        return [ln for ln in _lines(text) if "pool_llm" in ln]

    views = {m: llm(_squid(m)) for m in LLM_WEB_MODES}
    assert all(v == views["open"] for v in views.values())


def test_check_mode_invariants_refuses_a_sandbox_full_that_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    real = render.mode_lists

    def leaky(egress: render.EgressConfig, mode: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        profiles, callers = real(egress, mode)  # type: ignore[arg-type]
        if mode == "sandbox_full":
            callers = {**callers, "hermes": [c for c in callers["hermes"] if c != "web_public"]}
        return profiles, callers

    monkeypatch.setattr(render, "mode_lists", leaky)
    with pytest.raises(render.RenderError, match="sandbox_full must render exactly what sandbox_open renders"):
        render.check_mode_invariants(_bundle(), [NS])


# ---------------------------------------------------------------------------- order (M4)
@pytest.mark.parametrize("mode", EGRESS_MODES)
def test_marked_denies_and_floor_lines_precede_the_port_checks(mode: str) -> None:
    lines = _lines(_squid(mode))
    safe = lines.index("http_access deny !Safe_ports")
    head = lines[:safe]
    marked = [
        "http_access deny port_sandbox notify_only mark_quiet",
        "http_access deny port_sandbox denied_domains mark_name",
        "http_access deny port_sandbox ip_literal mark_literal",
        "http_access deny port_sandbox code_pools code_denied mark_code",
    ]
    assert [ln for ln in head if ln in marked] == marked
    ctx = render.squid_context(_bundle(), "", [NS], mode=mode)  # type: ignore[arg-type]
    for r in ctx["sandbox_rules"]:
        assert head.count(f"http_access deny port_sandbox {r['src']} {r['dst']} denied_dst mark_dst") == 1
        assert head.count(f"http_access deny port_sandbox {r['src']} {r['dst']} denied_dst_loop mark_dst_loop") == 1
    # every floor line is scoped to the sandbox port, so the host callers keep today's exact order
    assert all(ln.startswith("http_access deny port_sandbox ") for ln in head if ln.startswith("http_access"))
    tail = lines[safe:]
    assert tail[:3] == [
        "http_access deny !Safe_ports",
        "http_access deny CONNECT !SSL_ports",
        "http_access deny manager",
    ]


def test_host_ports_keep_the_telegraph_pre_deny_before_deny_denied_domains() -> None:
    bundle = _bundle()
    tg = bundle.telegram.model_copy(
        update={"telegraph": bundle.telegram.telegraph.model_copy(update={"enabled": True})}
    )
    lines = _lines(_squid("sandbox_full", render.ConfigBundle(bundle.policy, bundle.targets, bundle.egress,
                                                              bundle.profile, tg)))  # fmt: skip
    pre = f"http_access allow port_gatekeeper src_host cat_telegraph !ip_literal{FLOOR}"
    assert lines.index(pre) < lines.index("http_access deny denied_domains")
    assert lines.index("http_access deny manager") < lines.index(pre)


def test_split_dst_acl_is_disjoint_and_covers_every_floor() -> None:
    hosts = ["192.0.2.10/32"]
    ctx = render.squid_context(_bundle(), "", [NS], mode="sandbox_full", host_cidrs=hosts)
    loop, rest = set(ctx["loop_cidrs"]), set(ctx["denied_cidrs_non_loop"]) | set(ctx["host_cidrs"])
    assert loop == {"127.0.0.0/8", "0.0.0.0/8", "::1/128", "::/128"}
    assert not loop & rest and loop | rest == set(ctx["denied_cidrs"]) | set(hosts)
    text = _squid("sandbox_full", hosts=hosts)
    assert "acl denied_dst_loop dst 127.0.0.0/8 0.0.0.0/8 ::1/128 ::/128" in _lines(text)
    inline = next(ln for ln in _lines(text) if ln.startswith("acl denied_dst dst ") and '"' not in ln)
    assert inline.endswith(" 192.0.2.10/32")
    assert [ln for ln in _lines(text) if ln.startswith("acl denied_dst dst ")][1] == (
        'acl denied_dst dst "/etc/squid/floor/public.txt"'
    )


def test_code_web_ok_only_on_the_pool_fetch_web_line() -> None:
    text = _squid("sandbox_full")
    uses = [ln for ln in _lines(text) if "code_web_ok" in ln]
    assert uses == [
        'acl code_web_ok src "/etc/squid/floor/code-web.txt"',
        f"http_access allow port_sandbox pool_fetch code_web_ok all{FLOOR}",
    ]
    assert "acl code_pools src 10.213.0.0/18 10.213.192.0/19" in _lines(text)


def test_renderer_refuses_code_web_ok_and_floor_line_tampering() -> None:
    good = _squid("sandbox_full")
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="sandbox_full")
    bad_templates = [
        TEMPLATE.replace("http_access deny port_sandbox code_pools code_denied mark_code\n", ""),
        TEMPLATE.replace('acl denied_dst dst "/etc/squid/floor/public.txt"\n', ""),
        TEMPLATE.replace("{% if r.src == code_pool_src and r.dst == \"all\" %} code_web_ok{% endif %}",
                         " code_web_ok"),
        TEMPLATE.replace("denied_dst_loop mark_dst_loop\n", "denied_dst_loop mark_dst_loop\n"
                         "http_access deny port_sandbox {{ r.src }} {{ r.dst }} denied_dst_loop mark_dst_loop\n"),
    ]  # fmt: skip
    for bad in bad_templates:
        assert bad != TEMPLATE
        with pytest.raises(render.RenderError):
            render.render_context(ctx, bad)
    assert render.render_context(ctx, TEMPLATE) == good


def test_logformat_has_the_eleventh_field() -> None:
    assert "logformat ossbot %ts.%03tu %>a %>lp %Ss/%03>Hs %rm %ru %<a %>st %<st %tr %{ossbot_floor}note" in _lines(
        _squid("allowlist")
    )
    for mark in ("dst", "dst_loop", "name", "quiet", "literal", "code"):
        assert f"acl mark_{mark} annotate_transaction ossbot_floor={mark}" in _lines(_squid("allowlist"))


# --------------------------------------------------------------------- names (§4.1, §4.3)
def test_required_code_denied_and_every_kiro_host() -> None:
    bundle = _bundle()
    names = render.code_denied_names(bundle)
    assert {".telegram.org", ".t.me", ".telegram.me", ".kiro.dev"} <= set(names)
    for bare in ("api.telegram.org", "t.me", "telegram.me", "runtime.us-east-1.kiro.dev", "app.kiro.dev"):
        assert bare not in names  # covered by a dotted entry (squid refuses both forms in one ACL)
    for cat in render.CODE_DENIED_KIRO_CATEGORIES:
        for host in bundle.egress.categories[cat]:
            assert host in names or any(host.endswith(d) for d in names if d.startswith(".")), host
    assert {"oidc.us-east-1.amazonaws.com", "test-org.awsapps.com"} <= set(names)  # the IdC login hosts
    assert "api.github.com" not in names and "github.com" not in names


def test_collapse_domains() -> None:
    assert render.collapse_domains(["t.me", ".t.me", "A.T.ME", "x.example", ".example", "y.other"]) == [
        ".example", ".t.me", "y.other",
    ]  # fmt: skip


def test_notify_only_default_and_subset_invariant() -> None:
    assert "acl notify_only dstdomain -n checkpoint-api.hashicorp.com" in _lines(_squid("open"))
    ctx = render.squid_context(_bundle(), "", [NS], mode="open")
    for bad in (["example.com"], ["uploads.github.com"], []):
        with pytest.raises(render.RenderError, match="notify_only"):
            render.assert_context_invariants({**ctx, "notify_only": bad})


def test_old_snapshot_renders_full_floors() -> None:
    """The deploy comes before the owner's /reload: a snapshot without the ADR-0025 keys renders the full
    floors (built-ins unioned) and sandbox_full falls back to sandbox_open's grants."""
    old = parse_bundle(_files(_old_egress()))
    assert "sandbox_full" not in old.egress.modes and old.egress.code_denied == []
    assert old.egress.always_denied.notify_only is None and "ff00::/8" not in old.egress.always_denied.cidrs
    for mode in EGRESS_MODES:
        new_text, old_text = _squid(mode), _squid(mode, old)
        new_acls = [ln for ln in _lines(new_text) if ln.startswith("acl ")]
        old_acls = [ln for ln in _lines(old_text) if ln.startswith("acl ")]
        assert {ln.split()[1] for ln in old_acls} == {ln.split()[1] for ln in new_acls}
        old_dst = next(ln for ln in old_acls if ln.startswith("acl denied_dst dst ") and '"' not in ln).split()[3:]
        new_dst = next(ln for ln in new_acls if ln.startswith("acl denied_dst dst ") and '"' not in ln).split()[3:]
        assert set(old_dst) == set(new_dst)
        assert next(ln for ln in old_acls if ln.startswith("acl code_denied ")) == next(
            ln for ln in new_acls if ln.startswith("acl code_denied ")
        )  # the shipped list is a subset of the built-ins and kiro hosts
    assert "acl notify_only dstdomain -n checkpoint-api.hashicorp.com" in _lines(_squid("open", old))
    assert _squid("sandbox_full", old).replace("sandbox_full", "sandbox_open", 1) == _squid("sandbox_open", old)


# ---------------------------------------------------------------------------- host floor
def test_host_floor_cidrs_drops_addresses_inside_the_static_floors(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.255.255.254\nnameserver 9.9.9.9\n")
    outputs = {
        "addr": "1: lo    inet 127.0.0.1/8 scope host lo\n1: lo    inet 10.255.255.254/32 brd 10.255.255.254 scope"
                " global lo\n2: eth0    inet 172.21.195.2/20 brd 172.21.207.255 scope global eth0\n"
                "5: wlan    inet 203.0.113.50/24 scope global wlan\n",
        "route": "default via 172.21.192.1 dev eth0 proto kernel\n",
    }  # fmt: skip
    calls: list[list[str]] = []

    def run(argv: Sequence[str]) -> str:
        calls.append(list(argv))
        return outputs["addr" if "addr" in argv else "route"]

    assert render.host_floor_cidrs(resolv_conf=resolv, run=run) == ("9.9.9.9/32", "203.0.113.50/32")  # by address
    assert calls == [["ip", "-4", "-o", "addr", "show"], ["ip", "-4", "route", "show", "default"]]
    outputs["addr"] = outputs["addr"].replace("5: wlan    inet 203.0.113.50/24 scope global wlan\n", "")
    resolv.write_text("nameserver 10.255.255.254\n")
    assert render.host_floor_cidrs(resolv_conf=resolv, run=run) == ()  # today's live layout (V-W3)


def test_host_floor_cidrs_fails_closed_when_ip_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.255.255.254\n")

    class Proc:
        returncode, stdout, stderr = 1, "", "Cannot open netlink socket"

    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(render.RenderError, match="ip -4 -o addr show failed"):
        render.host_floor_cidrs(resolv_conf=resolv)


def test_squid_drift_names_a_moved_host_address(tmp_path: Path) -> None:
    files = _files()
    root = tmp_path / "snap"
    digest = write_snapshot(root, files)
    control = tmp_path / "control"
    control.mkdir()
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {NS}\n")
    template = tmp_path / "squid.conf.j2"
    template.write_text(TEMPLATE, encoding="utf-8")
    conf = tmp_path / "squid.conf"
    conf.write_text(render.render_squid(parse_bundle(files), digest, [NS], TEMPLATE, mode="open"))
    assert render.squid_drift(conf, root, template, resolv, control, host_cidrs=()) == []
    [problem] = render.squid_drift(conf, root, template, resolv, control, host_cidrs=("192.0.2.10/32",))
    assert problem.startswith("firewall_drift:host_floor_changed ")


# ------------------------------------------------------------------- render squid CLI (§4.6, §4.7)
@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = tmp_path / "snap"
    write_snapshot(root, _files())
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {NS}\n")
    template = tmp_path / "squid.conf.j2"
    template.write_text(TEMPLATE, encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir()
    floor = tmp_path / "floor"
    monkeypatch.setattr(render, "host_floor_cidrs", lambda **kw: ())
    locks: list[tuple[bool, Path]] = []

    class Lock:
        def __init__(self, exclusive: bool, timeout_s: float, path: Path) -> None:
            locks.append((exclusive, path))

        def __enter__(self) -> None:
            floor.mkdir(exist_ok=True)

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(render, "floor_lock", Lock)
    env = {"root": root, "resolv": resolv, "template": template, "control": control, "floor": floor}
    env["locks"] = locks  # type: ignore[assignment]
    return env


def _cli(env: dict[str, Path], *extra: str) -> list[str]:
    paths = {"--snapshot-root": "root", "--template": "template", "--resolv-conf": "resolv",
             "--control-dir": "control", "--floor-dir": "floor"}  # fmt: skip
    return ["squid", *(x for opt, key in paths.items() for x in (opt, str(env[key]))), *extra]


def test_render_squid_creates_the_floor_sentinels_under_the_exclusive_lock(
    cli_env: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "squid.conf"
    assert render.main(_cli(cli_env, "--out", str(out))) == 0
    floor = cli_env["floor"]
    assert (floor / "public.txt").read_text() == "240.0.0.1/32\n"
    assert (floor / "code-web.txt").read_text() == "255.255.255.255/32\n"  # fresh install: unknown
    assert oct((floor / "public.txt").stat().st_mode & 0o777) == "0o644"
    assert cli_env["locks"] == [(True, floor / ".lock")]  # type: ignore[comparison-overlap]
    (floor / "public.txt").write_text("240.0.0.1/32\n203.0.113.7/32\n")
    assert render.main(_cli(cli_env, "--out", str(out))) == 0
    assert (floor / "public.txt").read_text() == "240.0.0.1/32\n203.0.113.7/32\n"  # existing files untouched


def test_render_squid_mode_is_refused_with_the_live_output(cli_env: dict[str, Path], tmp_path: Path) -> None:
    assert render.main(_cli(cli_env, "--mode", "sandbox_full", "--out", str(render.DEFAULT_SQUID_OUT))) == 2
    out = tmp_path / "squid-sandbox_full.conf"
    assert render.main(_cli(cli_env, "--mode", "sandbox_full", "--out", str(out))) == 0
    assert "# Egress mode: sandbox_full " in out.read_text()


def test_firewall_print_takes_given_floors_only_with_print(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        render.main(["firewall", "--check", "--public-file", str(tmp_path / "p.txt")])


def test_public_floor_values_are_never_inline() -> None:
    """§4.7 privacy: the address lives only in the floor directory; the conf names the file."""
    text = _squid("sandbox_full")
    for line in _lines(text):
        if line.startswith("acl denied_dst dst ") and '"' not in line:
            for value in line.split()[3:]:
                net = ipaddress.ip_network(value)
                assert not net.is_global or net.prefixlen < net.max_prefixlen, value
