from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from vantage.schemas import PolicyDecision, ProbeResponse, RedactionLevel

if TYPE_CHECKING:
    from shadow_replica.connections.ws_hub import WSHub
    from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def generate_fallback(
    store: "ReceivedStore",
    developer_id: str,
    hub: "WSHub | None" = None,
) -> ProbeResponse:
    """Return a staleness-stamped ProbeResponse from the most recent received artifact.

    Falls back to cached ActivitySignals from hub when no artifacts have been received yet.
    """
    recent = store.list_for_developer(developer_id, limit=1)

    if not recent:
        # Try cached activity signals from the last live probe
        if hub is not None:
            cached = hub.last_activity(developer_id)
            if cached is not None:
                signals, captured_at = cached
                try:
                    # Both sides use naive UTC (datetime.utcnow())
                    staleness = int((datetime.utcnow() - captured_at.replace(tzinfo=None)).total_seconds())
                    staleness = max(0, staleness)
                except Exception:
                    staleness = 0
                summary = _summarise_signals(signals, staleness)
                return ProbeResponse(
                    request_id="",
                    developer_id=developer_id,
                    policy_decision=PolicyDecision.OFFLINE,
                    fallback_summary=summary,
                    staleness_seconds=staleness,
                    redaction_level_applied=RedactionLevel.STANDARD,
                )

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


def _summarise_signals(signals, staleness: int) -> str:
    parts: list[str] = []

    if signals.primary_activity and signals.primary_activity != "unknown":
        parts.append(signals.primary_activity)

    if signals.files_recently_touched:
        files = ", ".join(signals.files_recently_touched[:3])
        parts.append(f"{files}")

    if signals.inferred_branch:
        parts.append(f"on {signals.inferred_branch}")

    if signals.active_repo:
        parts.append(f"in {signals.active_repo}")

    if not parts:
        if signals.last_user_intent:
            return f"Last seen: \"{signals.last_user_intent[:100]}\""
        return "Last seen active (no commit data yet)."

    age = ""
    if staleness > 0:
        if staleness < 3600:
            age = f" — {staleness // 60}m ago"
        else:
            age = f" — {staleness // 3600}h ago"

    return f"Last seen: {' '.join(parts)}{age}"


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
