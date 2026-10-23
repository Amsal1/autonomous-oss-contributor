"""Re-record the Docker filter corpus from the pinned Docker CLI (DESIGN §23 item 15, FEAT-004).

Run on the provisioned distro (needs /usr/bin/docker, no daemon access):

    ROOTDEV python -m tests.unit.dockerfilter.record_corpus

A fake Docker API on a temporary Unix socket answers just enough for the CLI to finish each
command and records every request. Every sandboxd argv variant (``tests.sandboxharness.variants``)
is run through ``docker run``/``docker create``, and every other operation sandboxd performs goes
through ``DockerCliRuntime`` itself. The result is written to ``corpus/recorded.json``;
``test_recorded_corpus.py`` asserts that the CLI model equals these bodies and that the filter
allows every recorded request.
"""

from __future__ import annotations

import base64
import io
import json
import os
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ossbot.sandboxd.argv import build_argv
from ossbot.sandboxd.runtime.docker_cli import DockerCliRuntime
from tests.sandboxharness import ENVIRON, variants

OUT = Path(__file__).parent / "corpus" / "recorded.json"
FAKE_ID = "f" * 64
RECORDED: list[dict[str, Any]] = []


def _tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("output")
        info.type = tarfile.DIRTYPE
        info.mode = 0o700
        tar.addfile(info)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, payload: Any = None, raw: bytes | None = None, headers: dict[str, str] | None = None,
              ctype: str = "application/json") -> None:  # fmt: skip
        data = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("Api-Version", "1.52")
        self.send_header("Docker-Experimental", "false")
        self.send_header("Ostype", "linux")
        self.send_header("Content-Type", ctype)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self) -> None:
        url = urlsplit(self.path)
        body = self._body()
        parts = url.path.split("/")
        path = "/" + "/".join(parts[2:]) if len(parts) > 1 and parts[1].startswith("v1.") else url.path
        entry: dict[str, Any] = {"method": self.command, "path": url.path, "query": parse_qs(url.query)}
        if body:
            try:
                entry["body"] = json.loads(body)
            except ValueError:
                entry["raw_len"] = len(body)
        RECORDED.append(entry)
        info = {"name": "in", "size": 0, "mode": 2147484096, "mtime": "2026-01-01T00:00:00Z", "linkTarget": ""}
        stat = base64.b64encode(json.dumps(info).encode()).decode()
        if path == "/_ping":
            self._send(200, raw=b"OK", ctype="text/plain")
        elif path == "/containers/create":
            self._send(201, {"Id": FAKE_ID, "Warnings": []})
        elif path.endswith("/start") or path.endswith("/kill") or self.command == "DELETE":
            self._send(204)
        elif path.endswith("/wait"):
            self._send(200, {"StatusCode": 0})
        elif path.endswith("/archive"):
            if self.command == "GET":
                self._send(200, raw=_tar(), ctype="application/x-tar", headers={"X-Docker-Container-Path-Stat": stat})
            else:
                self._send(200, headers={"X-Docker-Container-Path-Stat": stat})
        elif path.endswith("/logs"):
            self._send(200, raw=b"", ctype="application/vnd.docker.raw-stream")
        elif path == "/containers/json":
            self._send(200, [])
        elif path.startswith("/containers/") and path.endswith("/json"):
            state = {"Status": "exited", "Running": False, "ExitCode": 0}
            self._send(200, {"Id": FAKE_ID, "Name": "/x", "State": state, "Config": {"Tty": False, "Labels": {}}})
        elif path == "/networks/create":
            self._send(201, {"Id": FAKE_ID, "Warning": ""})
        elif path == "/volumes/create":
            self._send(201, {"Name": entry.get("body", {}).get("Name", "v"), "Driver": "local"})
        else:
            self._send(200, {})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _ops(rt: DockerCliRuntime) -> dict[str, Any]:
    """Every non-create operation sandboxd performs, in the CLI's own words."""
    ops: dict[str, Any] = {}

    def rec(name: str, fn: Any) -> None:
        start = len(RECORDED)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a failure is recorded, not fatal
            print(f"{name}: {exc}", file=sys.stderr)
        ops[name] = [r for r in RECORDED[start:] if r["path"].split("/")[-1] != "_ping"]

    labels = {"ossbot.sandbox": "1", "ossbot.cid": "c_01abcdefgh", "ossbot.profile": "research"}
    rec("network_create", lambda: rt.create_network("ossbot-t-c-01abcdefgh-research", "10.213.64.0/28", True, labels))
    rec("network_connect", lambda: rt.connect("ossbot-t-c-01abcdefgh-research", "ossbot-proxy", alias="proxy"))
    rec("network_disconnect", lambda: rt.disconnect("ossbot-t-c-01abcdefgh-research", "ossbot-proxy"))
    rec("network_remove", lambda: rt.remove_network("ossbot-t-c-01abcdefgh-research"))
    # ADR-0024: the networked agent-exec's per-task network, a /28 in pool_fetch
    exec_labels = {**labels, "ossbot.profile": "exec"}
    rec("network_create_exec",
        lambda: rt.create_network("ossbot-t-c-01abcdefgh-exec", "10.213.0.32/28", True, exec_labels))  # fmt: skip
    rec(
        "volume_create", lambda: rt.create_volume("ossbot-in-r1-plan", {"ossbot.sandbox": "1", "ossbot.run": "r1-plan"})
    )
    rec("volume_remove", lambda: rt.remove_volume("ossbot-in-r1-plan"))
    rec("put_archive", lambda: rt.put_archive("ossbot-r1-plan-stagein", "/in", _tar()))
    rec("inspect_status", lambda: rt.inspect_status("ossbot-r1-plan-exec"))
    rec("wait", lambda: rt.wait("ossbot-r1-plan", 30))
    rec("logs", lambda: rt.logs("ossbot-r1-plan", None))
    rec("get_archive", lambda: b"".join(rt.get_archive("ossbot-r1-plan", "/io/output")))
    rec("kill", lambda: rt.kill("ossbot-r1-plan"))
    rec("remove", lambda: rt.remove("ossbot-r1-plan"))
    rec("list", lambda: rt.list({"ossbot.sandbox": "1", "ossbot.cid": "c_01abcdefgh"}))
    return ops


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ossbot-corpus-"))
    sock = tmp / "docker.sock"
    server = Server(str(sock), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["OSSBOT_DOCKER_CONFIG"] = str(tmp / "config")
    (tmp / "config").mkdir()
    env = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": f"unix://{sock}", "DOCKER_CONFIG": str(tmp / "config"),
           "DOCKER_CLI_HINTS": "false", **ENVIRON}  # fmt: skip
    version = subprocess.run(["/usr/bin/docker", "version", "--format", "{{.Client.Version}}"], env=env,
                             capture_output=True, text=True, check=False).stdout.strip()  # fmt: skip
    creates: dict[str, Any] = {}
    for name, spec in sorted(variants().items()):
        start = len(RECORDED)
        argv = build_argv(spec, docker="/usr/bin/docker")
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False, timeout=60)
        if proc.returncode != 0:
            print(f"{name}: {proc.stderr.strip()}", file=sys.stderr)
        made = [r for r in RECORDED[start:] if r["path"].endswith("/containers/create")]
        if len(made) != 1:
            print(f"{name}: expected one create, got {len(made)}", file=sys.stderr)
            return 1
        creates[name] = {"query": made[0]["query"], "body": made[0]["body"],
                         "followed_by": [f"{r['method']} {r['path'].split('/', 2)[-1]}" for r in RECORDED[start:]
                                         if r is not made[0] and not r["path"].endswith("/_ping")]}  # fmt: skip
    rt = DockerCliRuntime(socket=sock, docker="/usr/bin/docker")
    doc = {
        "_comment": "Recorded from the pinned Docker CLI by record_corpus.py (DESIGN §23 item 15).",
        "docker_cli_version": version,
        "creates": creates,
        "operations": _ops(rt),
    }  # fmt: skip
    server.shutdown()
    OUT.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {len(creates)} creates and {len(doc['operations'])} operations from docker {version} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
