"""sandboxd client for the runner (``/run/ossbot/sandboxd.sock``, DESIGN §10.2).

Job specs are built here from fixed per-stage values; nothing in them comes from LLM output
except validated model ids and file names the runner wrote itself.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ossbot.common.uds import uds_client

DEFAULT_SOCK = Path("/run/ossbot/sandboxd.sock")
FINAL = frozenset({"succeeded", "failed", "cancelled"})
PROBES_TIMEOUT_S = 5400.0


class SbxError(RuntimeError):
    def __init__(self, status: int, code: str, detail: Any = None) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"sandboxd {status} {code}")


class Sandbox:
    def __init__(self, client: httpx.Client | None = None, sock: Path = DEFAULT_SOCK) -> None:
        self.client = client or uds_client(sock, timeout=httpx.Timeout(60.0, connect=5.0))

    def _call(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
        try:
            resp = self.client.request(
                method, path, json=body, timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
            )
        except httpx.HTTPError as exc:
            raise SbxError(503, "sandboxd_unreachable", type(exc).__name__) from exc
        if resp.status_code >= 400:
            try:
                doc = resp.json()
            except ValueError:
                doc = {}
            raise SbxError(resp.status_code, str(doc.get("error", resp.status_code)), doc.get("detail"))
        return resp.json()

    def submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("POST", "/v1/jobs", spec)
        return doc

    def get(self, run_id: str) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", f"/v1/jobs/{run_id}")
        return doc

    def cancel(self, run_id: str) -> None:
        self._call("DELETE", f"/v1/jobs/{run_id}")

    def destroy(self, cid: str) -> None:
        self._call("POST", f"/v1/tasks/{cid}/destroy")

    def health(self) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", "/v1/health")
        return doc

    def probes_status(self) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", "/v1/probes/status")
        return doc

    def probes_run(self, models: dict[str, str]) -> dict[str, Any]:
        # five real sessions, run synchronously by sandboxd (each job up to 900 s)
        doc: dict[str, Any] = self._call("POST", "/v1/probes/run", {"models": models}, timeout=PROBES_TIMEOUT_S)
        return doc

    def run(
        self, spec: dict[str, Any], *, tick: Callable[[], None] | None = None, poll_s: float = 2.0
    ) -> dict[str, Any]:
        """Submit and wait for a final state. ``tick`` runs between polls (heartbeat, gate and
        timeout checks); an exception from it cancels the job and propagates."""
        job = self.submit(spec)
        while job.get("state") not in FINAL:
            if tick is not None:
                try:
                    tick()
                except BaseException:
                    with contextlib.suppress(SbxError):
                        self.cancel(spec["run_id"])
                    raise
            time.sleep(poll_s)
            job = self.get(spec["run_id"])
        return job
