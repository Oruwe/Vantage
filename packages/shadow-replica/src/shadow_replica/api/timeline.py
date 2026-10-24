from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from shadow_replica.auth.token_verify import verify_request
from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def make_router(store: ReceivedStore, trusted_devs: list) -> APIRouter:
    r = APIRouter()

    @r.get("/v1/timeline/{developer_id}")
    async def get_timeline(
        request: Request,
        developer_id: str,
        channel: str | None = Query(default=None),
        limit: int = Query(default=20, le=200),
    ):
        body = b""
        dev = verify_request(request, body, trusted_devs)

        if dev.developer_id != developer_id:
            return JSONResponse({"error": "forbidden"}, status_code=403)

        rows = store.list_for_developer(developer_id, channel=channel, limit=limit)
        items = [
            {
                "approval_id": r.approval_id,
                "artifact_type": r.artifact_type,
                "channel": r.channel,
                "approved_at": r.approved_at,
                "received_at": r.received_at,
                "payload": json.loads(r.payload_json),
            }
            for r in rows
        ]
        return JSONResponse({"developer_id": developer_id, "items": items, "total": len(items)})

    return r
