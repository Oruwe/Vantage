from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from shadow_replica.auth.token_verify import verify_request
from shadow_replica.metrics import artifacts_received
from shadow_replica.notify.notifier import NotificationConfig, dispatch_all
from shadow_replica.storage.received_store import ReceivedStore
from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent

if TYPE_CHECKING:
    from shadow_replica.wiretap.bus import WiretapBus

logger = logging.getLogger(__name__)

router = APIRouter()

_REPLICA_REF = A2AAgentRef(kind="shadow-replica", label="Shadow Replica")


def make_router(
    store: ReceivedStore,
    trusted_devs: list,
    bus: "WiretapBus | None" = None,
    notifications: list[NotificationConfig] | None = None,
) -> APIRouter:
    _notifications = notifications or []
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

        payload_json = json.dumps(payload)
        await store.save_async(
            approval_id=approval_id,
            developer_id=dev.developer_id,
            artifact_type=artifact_type,
            draft_id=draft_id,
            channel=channel,
            payload_json=payload_json,
            approved_at=approved_at,
        )
        artifacts_received.labels(developer_id=dev.developer_id, channel=channel).inc()

        logger.info(
            "Received %s from developer %s (approval_id=%s)",
            artifact_type,
            dev.developer_id,
            approval_id,
        )

        if bus is not None:
            try:
                bus.publish(A2AEnvelope(
                    intent=A2AIntent.ARTIFACT_PUSH,
                    from_agent=A2AAgentRef(
                        kind="ghost-daemon",
                        developer_id=dev.developer_id,
                        label=dev.name,
                    ),
                    to_agent=_REPLICA_REF,
                    summary=_push_summary(artifact_type, payload),
                    payload={
                        "approval_id": approval_id,
                        "artifact_type": artifact_type,
                        "channel": channel,
                        "approved_at": approved_at,
                        "preview": _push_preview(payload),
                    },
                ))
            except Exception as exc:
                logger.debug("Wiretap publish failed: %s", exc)

        if _notifications:
            await dispatch_all(
                _notifications,
                artifact_type=artifact_type,
                developer_name=dev.name,
                developer_id=dev.developer_id,
                payload_json=payload_json,
            )

        return JSONResponse({"status": "ok", "approval_id": approval_id}, status_code=201)

    return r


def _push_summary(artifact_type: str, payload: dict) -> str:
    try:
        if artifact_type == "commit_report":
            msg = payload.get("author_msg", "") or payload.get("generated_summary", "")
            return f"commit · {msg[:100]}"
        if artifact_type == "weekly_digest":
            highlights = payload.get("highlights", [])
            return f"digest · {highlights[0][:100]}" if highlights else "digest"
        if artifact_type == "standup":
            today = payload.get("today", [])
            return f"standup · {today[0][:100]}" if today else "standup"
        if artifact_type == "pr_draft":
            return f"pr · {payload.get('title', '')[:100]}"
        if artifact_type == "quick_note":
            return f"note · {payload.get('message', '')[:100]}"
        if artifact_type == "bug_resolution":
            sig = payload.get("error_signature", "")[:80]
            fix = payload.get("fix_summary", "")[:60]
            return f"resolved · {sig} → {fix}"
    except Exception:
        pass
    return artifact_type


def _push_preview(payload: dict) -> dict:
    preview = {}
    for key in ("title", "author_msg", "message", "summary", "generated_summary"):
        v = payload.get(key)
        if v:
            preview[key] = str(v)[:200]
    for key in ("highlights", "today", "yesterday", "blockers", "files_touched"):
        v = payload.get(key)
        if isinstance(v, list) and v:
            preview[key] = [str(x)[:120] for x in v[:3]]
    return preview


def _artifact_type_to_channel(artifact_type: str) -> str:
    mapping = {
        "weekly_digest": "weekly_digest",
        "commit_report": "commit_report",
        "standup": "standup",
        "pr_draft": "pr_draft",
        "bug_resolution": "bug_resolution",
    }
    return mapping.get(artifact_type, artifact_type)
