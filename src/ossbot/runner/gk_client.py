"""Gatekeeper client for the runner (worker role over ``/run/ossbot/gatekeeper.sock``, DESIGN §12.2).

The runner never imports the gatekeeper (import-linter contract); it only speaks the API. Every
non-2xx answer becomes ``GkError(status, code)`` so stages can act on refusals (409, 423).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from ossbot.common.config import ConfigBundle, parse_bundle
from ossbot.common.uds import uds_client

DEFAULT_SOCK = Path("/run/ossbot/gatekeeper.sock")


class GkError(RuntimeError):
    def __init__(self, status: int, code: str, detail: Any = None) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"gatekeeper {status} {code}")


class Gatekeeper:
    def __init__(self, client: httpx.Client | None = None, sock: Path = DEFAULT_SOCK) -> None:
        self.client = client or uds_client(sock)

    def _call(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self.client.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise GkError(503, "gatekeeper_unreachable", type(exc).__name__) from exc
        if resp.status_code >= 400:
            try:
                doc = resp.json()
            except ValueError:
                doc = {}
            code = str(doc.get("error") or doc.get("detail") or resp.status_code) if isinstance(doc, dict) else "error"
            raise GkError(resp.status_code, code, doc)
        if not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        return resp.json() if "json" in ctype else resp.content

    # --------------------------------------------------------------- control/config
    def health(self) -> bool:
        try:
            self._call("GET", "/v1/health")
        except GkError:
            return False
        return True

    def control(self) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", "/v1/control")
        return doc

    def config(self) -> tuple[str, ConfigBundle]:
        doc = self._call("GET", "/v1/config/snapshot")
        files = {name: text.encode("utf-8") for name, text in doc["files"].items()}
        return str(doc["hash"]), parse_bundle(files)

    def notify(self, text: str) -> None:
        self._call("POST", "/v1/notify", {"text": text[:3500]})

    def pause(self, reason: str) -> None:
        """A soft stop the owner resumes (the gatekeeper announces it on Telegram)."""
        self._call("POST", "/v1/control/pause", {"reason": reason[:200]})

    # --------------------------------------------------------------- contributions
    def contributions(self, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state
        doc: list[dict[str, Any]] = self._call("GET", "/v1/contributions", params=params)
        return doc

    def active_contributions(self, page: int = 200, max_pages: int = 50) -> list[dict[str, Any]]:
        """Every non-terminal contribution, newest first, paged (the dispatch view: an old WATCHING
        or snoozed item never falls out behind a pile of terminal ones)."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for n in range(max_pages):
            params = {"limit": page, "active": "true", "offset": n * page}
            rows: list[dict[str, Any]] = self._call("GET", "/v1/contributions", params=params)
            out.extend(r for r in rows if str(r["id"]) not in seen)  # an update between pages may repeat one
            seen.update(str(r["id"]) for r in rows)
            if len(rows) < page:
                break
        return out

    def contribution(self, cid: str) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", f"/v1/contributions/{cid}")
        return doc

    def create(self, repo: str, issue: int, source: str, score: float | None, issue_hash: str | None) -> str:
        body = {"repo": repo, "issue_number": issue, "source": source, "score": score, "issue_hash": issue_hash}
        return str(self._call("POST", "/v1/contributions", body)["cid"])

    def transition(self, cid: str, from_: str, to: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"from": from_, "to": to, "evidence": evidence or {}}
        doc: dict[str, Any] = self._call("POST", f"/v1/contributions/{cid}/transition", body)
        return doc

    def import_bundle(self, cid: str) -> bytes:
        data = self._call("GET", f"/v1/contributions/{cid}/import-bundle")
        return data if isinstance(data, bytes) else base64.b64decode(data)

    # --------------------------------------------------------------- commit signing (ADR-0006 addendum)
    def signing(self) -> dict[str, Any]:
        """``{"mode", "available", "fingerprint"}``: will the gatekeeper SSH-sign packaged commits?"""
        doc: dict[str, Any] = self._call("GET", "/v1/signing")
        return doc

    def head_bundle(self, cid: str, rev: int) -> bytes | None:
        """The signed head of ``rev`` as a bundle (fix rounds), None when that rev was not signed."""
        try:
            data = self._call("GET", f"/v1/contributions/{cid}/head-bundle", params={"rev": rev})
        except GkError as exc:
            if exc.status == 404 and exc.code == "not_signed":
                return None
            raise
        return data if isinstance(data, bytes) else None

    # --------------------------------------------------------------- gate 1
    def submit_proposal(
        self, cid: str, proposal: dict[str, Any], revises: int | None, feedback: str | None,
        *, code_inspection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        body: dict[str, Any] = {"proposal": proposal, "revises": revises, "feedback": feedback}
        if code_inspection is not None:
            body["code_inspection"] = code_inspection
        doc: dict[str, Any] = self._call("POST", f"/v1/contributions/{cid}/proposal", body)
        return doc

    def proposal(self, cid: str) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", f"/v1/contributions/{cid}/proposal")
        return doc

    def prefs(self, repo: str | None = None) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("GET", "/v1/discovery-prefs", params={"repo": repo} if repo else None)
        return dict(doc["effective"])

    # --------------------------------------------------------------- open discovery (post-v1, §8.4)
    def discovered(self, status: str | None = None, *, untried: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if untried:
            params["untried"] = "true"
        doc: list[dict[str, Any]] = self._call("GET", "/v1/discovered-targets", params=params)
        return doc

    def excluded(self) -> set[str]:
        """Lower-cased repos open discovery must not record again (never, kept, cooling, queued)."""
        return {str(r).lower() for r in self._call("GET", "/v1/discovered-targets/excluded")["repos"]}

    def record_discovered(self, facts: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("POST", "/v1/discovered-targets", facts)
        return doc

    def resolve_target(self, repo: str) -> dict[str, Any]:
        """``{"source": "config"|"discovered", "target": ResolvedTargetView}``; 404 ``not_a_target``."""
        doc: dict[str, Any] = self._call("GET", "/v1/targets/resolve", params={"repo": repo})
        return doc

    def egress_mode(self) -> str:
        return str(self._call("GET", "/v1/control/egress-mode")["mode"])

    def discovery_cycle(self, report: dict[str, Any]) -> None:
        """``POST /v1/discovery-cycles``: one finished or cut discovery cycle (audited; a finished one gets
        the silent Discovery FYI). It records and notifies only; it changes no state."""
        self._call("POST", "/v1/discovery-cycles", report)

    def pacing(self) -> dict[str, Any]:
        """``proposal.max_per_day`` state: ``remaining`` and ``next_slot_at`` (§9.5b)."""
        doc: dict[str, Any] = self._call("GET", "/v1/discovery-prefs")
        return dict(doc["pacing"])

    # --------------------------------------------------------------- stage runs
    def stage_open(self, cid: str, stage: str, **fields: Any) -> dict[str, Any]:
        body = {"stage": stage, **{k: v for k, v in fields.items() if v is not None}}
        doc: dict[str, Any] = self._call("POST", f"/v1/contributions/{cid}/stage-runs", body)
        return doc

    def stage_update(self, run_id: str, **fields: Any) -> None:
        self._call("POST", f"/v1/stage-runs/{run_id}", {k: v for k, v in fields.items() if v is not None})

    def stage_runs(self, status: str | None = None, cid: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in (("status", status), ("cid", cid)) if v}
        doc: list[dict[str, Any]] = self._call("GET", "/v1/stage-runs", params=params or None)
        return doc

    # --------------------------------------------------------------- models
    def catalog(self) -> list[dict[str, Any]]:
        doc: list[dict[str, Any]] = self._call("GET", "/v1/models/catalog")
        return doc

    def upsert_catalog(self, entries: list[dict[str, Any]]) -> list[str]:
        return list(self._call("POST", "/v1/models/catalog", {"models": entries})["new_ids"])

    def claim_refresh(self) -> bool:
        return bool(self._call("POST", "/v1/models/refresh/claim")["requested"])

    # --------------------------------------------------------------- chat PDF jobs (§14.12.5)
    def claim_chat_job(self) -> dict[str, Any] | None:
        """The next assistant job: ``kind`` ``pdf`` (``pdf_b64``) or ``code`` (``code_b64``, ``files``)."""
        job = self._call("POST", "/v1/chat/jobs/claim")["job"]
        return dict(job) if isinstance(job, dict) and job else None

    def chat_job_result(self, job_id: str, *, run_id: str | None, ok: bool, error: str | None = None) -> None:
        self._call("POST", f"/v1/chat/jobs/{job_id}/result", {"run_id": run_id, "ok": ok, "error": error})

    def claim_discover(self) -> list[str] | None:
        """A pending ``ossctl discover`` request: its repos ([] = all), or None when there is none."""
        doc = self._call("POST", "/v1/discover/claim")
        return [str(r) for r in doc["repos"]] if doc.get("requested") else None

    # --------------------------------------------------------------- package/actions
    def package_ts(self, cid: str, rev: int) -> str:
        return str(self._call("POST", f"/v1/contributions/{cid}/package-ts", {"rev": rev})["package_ts"])

    def package(self, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = self._call("POST", f"/v1/contributions/{cid}/package", body)
        return doc

    def action(self, cid: str, kind: str, target_number: int, body: str) -> dict[str, Any]:
        payload = {"cid": cid, "kind": kind, "target_number": target_number, "body": body}
        doc: dict[str, Any] = self._call("POST", "/v1/actions", payload)
        return doc
