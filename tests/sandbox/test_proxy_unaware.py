"""Proxy-unaware tools reach the web through netshim (build-env design §3.2, §15, ADR-0025, AC 5).

A strict validate job (``egress: strict``: pool_strict, the base allowlist, so the result holds in every egress
mode) runs tools that ignore the proxy variables: ``curl --noproxy '*'``, ``git ls-remote`` with the proxy
variables unset, a Java ``HttpURLConnection`` opened with ``Proxy.NO_PROXY``, and a build script's own curl. Each
one resolves its name through netshim's stub (a 127.70.x.y address), connects to netshim, and netshim turns the
connection into an ordinary CONNECT through squid: the squid log shows the CONNECT lines with bytes from the job's
/28. An IP literal has no route (``curl --noproxy '*' https://1.1.1.1/`` fails). Root, Docker, the real filter.
"""

from __future__ import annotations

import ipaddress
import json
import time
from pathlib import Path

from ossbot.common.egress_log import parse_line
from tests.sandbox.conftest import Sandbox

CID = "c_SMOKETEST7"
ACCESS_LOG = Path("/srv/ossbot/logs/proxy/access.log")
UNSET = "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u JAVA_TOOL_OPTIONS -u JDK_JAVA_OPTIONS"
FETCH_JAVA = """import java.net.*;
public class Fetch {
  public static void main(String[] a) throws Exception {
    HttpURLConnection c = (HttpURLConnection) new URL(a[0]).openConnection(Proxy.NO_PROXY);
    c.setConnectTimeout(30000);
    c.setReadTimeout(30000);
    System.out.println("status=" + c.getResponseCode());
  }
}
"""
BUILD_SH = """#!/bin/sh
# a build script that downloads with its own curl and never looks at the proxy variables
set -e
curl -fsS --noproxy '*' --max-time 60 -o /tmp/maven-metadata.xml \\
  https://repo.maven.apache.org/maven2/junit/junit/maven-metadata.xml
grep -c '<version>' /tmp/maven-metadata.xml
"""


def _lines_from(subnet: ipaddress.IPv4Network, since: float) -> list[tuple[str, str, int, int]]:
    out = []
    for raw in ACCESS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        e = parse_line(raw)
        if e is None or e.ts < since or ipaddress.IPv4Address(e.client) not in subnet:
            continue
        out.append((e.method, e.host, e.up, e.down))
    return out


def test_proxy_unaware_tools_go_through_netshim_and_squid(sandbox: Sandbox) -> None:
    start = time.time() - 1
    try:
        clone = sandbox.run(sandbox.plan(
            CID, "fetch", entrypoint="fetch", repo_mode="rw", create_task=True, egress="strict",
            env={"OSSBOT_ARG_REPO": "octocat/Hello-World", "OSSBOT_ARG_CLONE_DEPTH": "1"},
        ))  # fmt: skip
        assert clone.exit_code == 0, clone.logs[-2000:]
        inputs = sandbox.root / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        (inputs / "fetch.java").write_text(FETCH_JAVA, encoding="utf-8")
        (inputs / "build.sh").write_text(BUILD_SH, encoding="utf-8")
        plan = {"commands": [
            {"name": "stub-dns", "cmd": "getent hosts repo.maven.apache.org", "timeout_s": 60},
            {"name": "curl-noproxy", "timeout_s": 120,
             "cmd": "curl -fsS --noproxy '*' --max-time 60 -o /dev/null -w '%{http_code}' https://repo.maven.apache.org/maven2/"},
            {"name": "git-ls-remote", "timeout_s": 180,
             "cmd": f"{UNSET} git ls-remote https://github.com/octocat/Hello-World HEAD"},
            {"name": "java-no-proxy", "timeout_s": 300,
             "cmd": f"{UNSET} java /io/input/fetch.java https://repo.maven.apache.org/maven2/"},
            {"name": "build-script-curl", "timeout_s": 120, "cmd": f"{UNSET} sh /io/input/build.sh"},
            {"name": "ip-literal-no-route", "timeout_s": 60,
             "cmd": "! curl -sS --noproxy '*' --max-time 10 -o /dev/null https://1.1.1.1/"},
        ]}  # fmt: skip
        (inputs / "unaware.json").write_text(json.dumps(plan), encoding="utf-8")
        files = {name: inputs / name for name in ("unaware.json", "fetch.java", "build.sh")}
        out = sandbox.run(sandbox.plan(
            CID, "validate", entrypoint="validate", repo_mode="rw", egress="strict",
            env={"OSSBOT_ARG_PLAN_FILE": "unaware.json"}, inputs=files, timeout=1200,
        ))  # fmt: skip
        assert out.exit_code == 0, out.logs[-2000:]
        commands = {c["name"]: c for c in json.loads(out.files["validation.json"])["commands"]}
        for name, cmd in commands.items():
            assert cmd["exit_code"] == 0, (name, cmd["tail"][-1500:], out.logs[-1500:])
        assert commands["stub-dns"]["tail"].split()[0].startswith("127.70."), commands["stub-dns"]["tail"]
        assert commands["curl-noproxy"]["tail"].strip().endswith("200"), commands["curl-noproxy"]["tail"]
        assert "HEAD" in commands["git-ls-remote"]["tail"], commands["git-ls-remote"]["tail"]
        assert "status=200" in commands["java-no-proxy"]["tail"], commands["java-no-proxy"]["tail"][-1500:]
        subnet = ipaddress.IPv4Network(sandbox.svc.networks.subnets.allocate("validate-strict", CID))
        time.sleep(2)  # squid writes a tunnel's line when it closes
        lines = _lines_from(subnet, start)
        tunnels = {(m, h) for m, h, up, down in lines if m == "CONNECT" and up > 0 and down > 0}
        assert ("CONNECT", "repo.maven.apache.org") in tunnels, lines[-20:]
        assert ("CONNECT", "github.com") in tunnels, lines[-20:]
        assert not [h for _, h, _, _ in lines if h == "1.1.1.1"], "the IP literal never reached the proxy"
    finally:
        sandbox.cleanup(CID)
