"""``POST /v1/features/{feature}/cards`` (DESIGN §14.12.7, U12, AC 35): Hermes feature cards.

Role ``requester`` (Hermes), at most 30 per hour. The feature must be registered and listed in
``telegram.yaml features.enabled`` (else 404). ``data`` is validated with the feature's model; a
validation error is a 422 whose detail is never echoed raw. The session key must name an active turn
(else 409 ``no_active_turn``). The card is stored in the feature's own store, rendered with the kit,
checked to end its header line with ``· 🤖 assistant``, sent where U12 allows and counted on the turn
(``chat_turns.feature_cards``, which the Telegraph eligibility check reads).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ossbot.common.clock import to_iso
from ossbot.common.schemas import FeatureCardRequest
from ossbot.gatekeeper import db, features
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)
CARDS_PER_HOUR = 30
SESSION_HEADER = "X-Hermes-Session-Key"
HEADER_MARK = "🤖 assistant"


def _header_text(view: Any) -> str:
    for block in view.blocks:
        for el in block.elements:
            lines = getattr(el, "lines", None)
            if lines:
                return "".join(str(getattr(span, "value", "")) for span in lines[0])
    return ""


@router.post("/features/{feature}/cards", status_code=202)
async def feature_card(
    request: Request,
    feature: str,
    body: FeatureCardRequest,
    caller: Annotated[Caller, Depends(require(Role.REQUESTER))],
) -> Any:
    from ossbot.gatekeeper.telegram import hermes_out  # noqa: PLC0415 - telegram imports the context
    from ossbot.gatekeeper.telegram.runtime import ui_of  # noqa: PLC0415

    c = ctx(request)
    spec = features.get(feature, c.config.telegram.features.enabled)
    if spec is None or body.kind not in spec.schema_models:
        return JSONResponse(status_code=404, content={"error": "unknown_feature"})
    since = to_iso(c.clock.now() - timedelta(hours=1))
    if c.audit.count_since("feature_card", since, caller.actor) >= CARDS_PER_HOUR:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "limit_per_hour": CARDS_PER_HOUR})
    try:
        data = spec.schema_models[body.kind].model_validate(body.data)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"error": "invalid_data", "fields": [
            ".".join(str(p) for p in e["loc"]) for e in exc.errors()][:10]})  # fmt: skip
    target = hermes_out.resolve(c, request.headers.get(SESSION_HEADER))
    if target is None:
        return JSONResponse(status_code=409, content={"error": "no_active_turn"})
    prefs = ui_of(c).prefs
    view = spec.cards[body.kind](data, {"rich_views": prefs.get("rich_views", c.config.telegram.rich_views_default)})
    if view.kind != "feature" or not _header_text(view).endswith(HEADER_MARK):
        log.error("feature %s card %s breaks the header rule", feature, body.kind, extra={"event": "feature_bad_card"})
        return JSONResponse(status_code=500, content={"error": "bad_card"})
    store = features.FeatureStore(c.paths.gatekeeper_dir / "features", spec)
    try:
        store.add(body.kind, data, target.turn_id, to_iso(c.clock.now()))
    finally:
        store.close()
    with db.transaction(c.conn):
        c.audit.append(caller.actor, "feature_card", {"feature": feature, "kind": body.kind})
    assert c.chat is not None  # resolve() returned a target
    c.chat.service.turns.count_feature_card(target.turn_id)
    hermes_out.deliver(c, target, view, label=feature, priority=2)
    return {"queued": True}
