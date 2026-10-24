from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from vantage.schemas import PolicyDecision, ProbeResponse, RedactionLevel

if TYPE_CHECKING:
    from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def generate_fallback(store: "ReceivedStore", developer_id: str) -> ProbeResponse:
    """Return a staleness-stamped ProbeResponse from the most recent received artifact."""
    recent = store.list_for_developer(developer_id, limit=1)

    if not recent:
        return ProbeResponse(
            request_id="",
            developer_id=developer_id,
            policy_decision=PolicyDecision.OFFLINE,
            fallback_summary="No recent artifacts available for this developer.",
            staleness_seconds=0,
            redaction_level_applied=RedactionLevel.STANDARD,
        )

    row = recent[0]
    try:
        received_dt = datetime.fromisoformat(row.received_at)
        staleness = int((datetime.utcnow() - received_dt).total_seconds())
    except Exception:
        staleness = 0

    summary = _summarise_artifact(row)

    return ProbeResponse(
        request_id="",
        developer_id=developer_id,
        policy_decision=PolicyDecision.OFFLINE,
        fallback_summary=summary,
        staleness_seconds=staleness,
        redaction_level_applied=RedactionLevel.STANDARD,
    )


def _summarise_artifact(row) -> str:
    try:
        payload = json.loads(row.payload_json)
    except Exception:
        return f"Last {row.artifact_type.replace('_', ' ')} received (payload unreadable)."

    artifact_type = row.artifact_type
    if artifact_type == "weekly_digest":
        highlights = payload.get("highlights", [])
        if highlights:
            return "Last digest highlights: " + "; ".join(highlights[:3])
        return "Last weekly digest received."
    if artifact_type == "standup":
        yesterday = payload.get("yesterday", [])
        if yesterday:
            return "Last standup (yesterday): " + "; ".join(yesterday[:3])
        return "Last standup received."
    if artifact_type == "commit_report":
        msg = payload.get("author_msg", "")
        branch = payload.get("branch", "")
        return f"Last commit on {branch!r}: {msg[:120]}" if msg else "Last commit report received."
    if artifact_type == "pr_draft":
        title = payload.get("title", "")
        return f"Last PR draft: {title[:120]}" if title else "Last PR draft received."

    return f"Last {artifact_type.replace('_', ' ')} received."
