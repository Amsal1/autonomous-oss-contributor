"""Commit-signing reads (owner addition 2026-10-21, ADR-0006 addendum).

* ``GET /v1/signing`` (worker, operator): ``policy.commit.signing``, whether a usable key is set and
  its public fingerprint. Vet reads it: a repo that requires signed commits is skipped only when
  nothing will be signed. Never the key.
* ``GET /v1/contributions/{cid}/head-bundle?rev=N`` (worker): the signed head of rev N as a one-ref
  bundle (``refs/heads/ossbot-change``, public commit data only), for the next fix round's unbundle
  (the sandbox holds only the unsigned commit). 404 ``not_signed`` when rev N was not signed.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from ossbot.gatekeeper import db, signing
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
CID_RE = re.compile(r"^c_[0-9A-Z]{10}$")


def status(c: Any) -> dict[str, Any]:
    mode = c.config.policy.commit.signing
    try:
        signer = c.signer
    except signing.SigningError:
        return {"mode": mode, "available": False, "fingerprint": None, "error": "signing_key_invalid"}
    available = signer is not None and mode != "off"
    return {"mode": mode, "available": available, "fingerprint": signer.fingerprint if signer else None}


@router.get("/signing")
async def get_signing(
    request: Request, caller: Annotated[Caller, Depends(require(Role.WORKER, Role.OPERATOR))]
) -> dict[str, Any]:
    return status(ctx(request))


@router.get("/contributions/{cid}/head-bundle")
async def head_bundle(
    request: Request,
    cid: str,
    caller: Annotated[Caller, Depends(require(Role.WORKER))],
    rev: Annotated[int, Query(ge=1, le=100_000)],
) -> Response:
    if not CID_RE.match(cid):
        return JSONResponse(status_code=404, content={"error": "unknown_cid"})
    c = ctx(request)
    if db.one(c.conn, "SELECT 1 FROM contributions WHERE id = ?", (cid,)) is None:
        return JSONResponse(status_code=404, content={"error": "unknown_cid"})
    path = c.gitops.signed_bundle(cid, rev)
    try:
        meta = json.loads((c.gitops.derived_dir(cid, rev) / "meta.json").read_text(encoding="utf-8"))
        data = path.read_bytes()
    except (OSError, ValueError):
        return JSONResponse(status_code=404, content={"error": "not_signed"})
    if not isinstance(meta, dict) or signing.from_meta(meta) is None:
        return JSONResponse(status_code=404, content={"error": "not_signed"})
    return Response(content=data, media_type="application/octet-stream")
