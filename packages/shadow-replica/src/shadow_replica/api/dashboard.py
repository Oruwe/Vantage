from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from shadow_replica.auth.token_verify import verify_request
from shadow_replica.metrics import dashboard_requests
from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def make_router(store: ReceivedStore, trusted_devs: list) -> APIRouter:
    r = APIRouter()

    @r.get("/v1/dashboard")
    async def get_dashboard(
        request: Request,
        channel: str | None = Query(default=None),
        limit: int = Query(default=50, le=500),
    ):
        """Aggregated view of all received artifacts across all registered developers.

        Any valid bearer token from a trusted dev grants read access — the data here
        is already approved artifacts, and the replica is subscriber-controlled.
        """
        body = b""
        verify_request(request, body, trusted_devs)
        dashboard_requests.inc()

        rows = store.list_all(channel=channel, limit=limit)
        items = [
            {
                "developer_id": r.developer_id,
                "approval_id": r.approval_id,
                "artifact_type": r.artifact_type,
                "channel": r.channel,
                "approved_at": r.approved_at,
                "received_at": r.received_at,
                "payload": json.loads(r.payload_json),
            }
            for r in rows
        ]
        developers = store.list_developers()
        return JSONResponse({
            "developers": developers,
            "items": items,
            "total": len(items),
        })

    @r.get("/v1/devs")
    async def list_devs(request: Request):
        """List all developer IDs that have sent at least one artifact to this replica."""
        body = b""
        verify_request(request, body, trusted_devs)

        developer_ids = store.list_developers()
        devs_info = []
        for dev_id in developer_ids:
            recent = store.list_for_developer(dev_id, limit=1)
            devs_info.append({
                "developer_id": dev_id,
                "name": next(
                    (d.name for d in trusted_devs if d.developer_id == dev_id),
                    dev_id,
                ),
                "last_received_at": recent[0].received_at if recent else None,
                "artifact_count": len(store.list_for_developer(dev_id, limit=10000)),
            })
        return JSONResponse({"developers": devs_info})

    return r
