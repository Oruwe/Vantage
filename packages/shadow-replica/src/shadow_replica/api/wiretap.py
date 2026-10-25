from __future__ import annotations

import asyncio
import hmac as _hmac
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

if TYPE_CHECKING:
    from shadow_replica.config import ReplicaConfig
    from shadow_replica.wiretap.bus import WiretapBus

logger = logging.getLogger(__name__)

_SSE_HEARTBEAT = 15.0


def _require_manager(request: Request, manager_token: str, allow_query_token: bool = False) -> None:
    """Authorize a manager request.

    EventSource cannot set custom headers in browsers, so for the SSE stream we
    also accept the token as a `?token=` query param.
    """
    auth = request.headers.get("Authorization", "")
    provided = ""
    if auth.startswith("Bearer "):
        provided = auth.removeprefix("Bearer ").strip()
    elif allow_query_token:
        provided = request.query_params.get("token", "")
    if not manager_token or not provided or not _hmac.compare_digest(provided, manager_token):
        raise HTTPException(status_code=401, detail="Invalid manager token")


def make_router(bus: "WiretapBus", cfg: "ReplicaConfig") -> APIRouter:
    r = APIRouter(prefix="/ui/api/wiretap")

    @r.get("/recent")
    async def recent(request: Request, limit: int = 100):
        _require_manager(request, cfg.manager_token)
        envs = bus.recent(limit=min(max(limit, 1), 500))
        return JSONResponse([e.model_dump(mode="json") for e in envs])

    @r.get("/stream")
    async def stream(request: Request, token: str = ""):
        _require_manager(request, cfg.manager_token, allow_query_token=True)

        async def event_gen():
            # Initial burst: send recent envelopes so first paint is populated.
            for env in bus.recent(limit=50):
                yield f"data: {env.model_dump_json()}\n\n"
            try:
                async for env in bus.subscribe():
                    if await request.is_disconnected():
                        break
                    yield f"data: {env.model_dump_json()}\n\n"
            except asyncio.CancelledError:
                pass

        headers = {
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)

    return r
