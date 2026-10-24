from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from shadow_replica.auth.token_verify import verify_request
from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)

router = APIRouter()


def make_router(store: ReceivedStore, trusted_devs: list) -> APIRouter:
    r = APIRouter()

    @r.post("/v1/receive")
    async def receive(request: Request):
        body = await request.body()
        dev = verify_request(request, body, trusted_devs)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        approval_id = data.get("approval_id", "")
        draft_id = data.get("draft_id", "")
        artifact_type = data.get("artifact_type", "")
        approved_at = data.get("approved_at", "")
        payload = data.get("payload", {})
        channel = _artifact_type_to_channel(artifact_type)

        if not approval_id:
            return JSONResponse({"error": "missing approval_id"}, status_code=400)

        store.save(
            approval_id=approval_id,
            developer_id=dev.developer_id,
            artifact_type=artifact_type,
            draft_id=draft_id,
            channel=channel,
            payload_json=json.dumps(payload),
            approved_at=approved_at,
        )

        logger.info(
            "Received %s from developer %s (approval_id=%s)",
            artifact_type,
            dev.developer_id,
            approval_id,
        )
        return JSONResponse({"status": "ok", "approval_id": approval_id}, status_code=201)

    return r


def _artifact_type_to_channel(artifact_type: str) -> str:
    mapping = {
        "weekly_digest": "weekly_digest",
        "commit_report": "commit_report",
        "standup": "standup",
        "pr_draft": "pr_draft",
    }
    return mapping.get(artifact_type, artifact_type)
