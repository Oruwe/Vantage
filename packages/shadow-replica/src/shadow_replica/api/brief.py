"""Daily executive brief synthesizer.

Replaces the "wall of AI summaries" problem by aggregating the last 24h of
artifacts across all developers into a single structured brief. Uses the LLM
only when configured; otherwise falls back to deterministic bullet extraction.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from vantage.schemas import DailyBrief

if TYPE_CHECKING:
    from shadow_replica.agent.subscriber_agent import SubscriberAgent
    from shadow_replica.config import ReplicaConfig
    from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


async def build_brief(
    store: "ReceivedStore",
    agent: "SubscriberAgent",
    cfg: "ReplicaConfig",
    hours: int = 24,
    bus=None,
) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    rows = store.list_all(limit=5000)
    recent = []
    for row in rows:
        if not row.received_at:
            continue
        try:
            ts = _parse_iso(row.received_at)
        except Exception:
            continue
        if ts >= cutoff:
            recent.append((ts, row))

    shipped: list[str] = []
    in_flight: list[str] = []
    bottlenecks: list[str] = []
    unresolved: list[str] = []

    dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
    seen_devs: set[str] = set()

    for ts, row in recent:
        seen_devs.add(row.developer_id)
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            continue
        name = dev_name_map.get(row.developer_id, row.developer_id)
        ch = row.channel
        if ch == "commit_report":
            msg = (payload.get("author_msg") or payload.get("generated_summary") or "").strip()
            if msg:
                shipped.append(f"{name}: {msg[:140]}")
        elif ch == "weekly_digest":
            for line in payload.get("highlights", [])[:3]:
                shipped.append(f"{name}: {str(line)[:140]}")
            for line in payload.get("open_threads", [])[:3]:
                in_flight.append(f"{name}: {str(line)[:140]}")
            for line in payload.get("blockers", [])[:3]:
                bottlenecks.append(f"{name}: {str(line)[:140]}")
        elif ch == "standup":
            for line in payload.get("today", [])[:3]:
                in_flight.append(f"{name}: {str(line)[:140]}")
            for line in payload.get("blockers", [])[:2]:
                bottlenecks.append(f"{name}: {str(line)[:140]}")
        elif ch == "pr_draft":
            title = (payload.get("title") or "").strip()
            if title:
                in_flight.append(f"{name}: PR · {title[:120]}")
            risks = (payload.get("risk_notes") or "").strip()
            if risks:
                unresolved.append(f"{name}: {risks[:140]}")
        elif ch == "github_event":
            event_type = (payload.get("event_type") or "")
            summary = (payload.get("summary") or "").strip()
            if not summary:
                continue
            if event_type == "pull_request.closed":
                # Merged PRs are "shipped"; unmerged closes are not noteworthy.
                excerpt = (payload.get("raw_payload_excerpt") or "").lower()
                if "merged" in summary.lower() or "merged" in excerpt:
                    shipped.append(f"{name}: {summary[:140]}")
            elif event_type in ("pull_request.opened", "pull_request.reopened",
                                "pull_request.ready_for_review", "pull_request.review_requested",
                                "pull_request_review.submitted", "pull_request_review_comment.created"):
                in_flight.append(f"{name}: {summary[:140]}")
            elif event_type.startswith("check_run.") or event_type.startswith("check_suite."):
                bottlenecks.append(f"{name}: {summary[:140]}")
            elif event_type == "push":
                shipped.append(f"{name}: {summary[:140]}")

    # ── Semantic collisions in the same window ─────────────────────────────
    collisions = []
    try:
        from shadow_replica.collision.semantic import SemanticCollisionDetector
        from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent
        detector = SemanticCollisionDetector(store)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        collisions = await detector.scan_recent(
            hours=hours,
            max_collisions=10,
            dev_name_map=dev_name_map,
        )
        if bus is not None and collisions:
            _ref = A2AAgentRef(kind="shadow-replica", label="Shadow Replica")
            _mgr = A2AAgentRef(kind="manager-ui", label="Manager")
            for c in collisions:
                try:
                    bus.publish(A2AEnvelope(
                        intent=A2AIntent.SEMANTIC_COLLISION_ALERT,
                        from_agent=_ref,
                        to_agent=_mgr,
                        status="offer",
                        summary=(
                            f"collision ({c.severity}): {c.developer_a_name} ↔ {c.developer_b_name} · "
                            f"cos={c.cosine_similarity:.2f}"
                            + (f" · files {','.join(c.file_overlap[:2])}" if c.file_overlap else "")
                        ),
                        payload=c.model_dump(mode="json"),
                    ))
                except Exception as exc:
                    logger.debug("Wiretap publish failed for collision: %s", exc)
    except Exception as exc:
        logger.debug("Collision scan skipped: %s", exc)

    brief = DailyBrief(
        period_start=cutoff,
        period_end=now,
        shipped=_dedupe_truncate(shipped, 15),
        in_flight=_dedupe_truncate(in_flight, 15),
        bottlenecks=_dedupe_truncate(bottlenecks, 10),
        unresolved_questions=_dedupe_truncate(unresolved, 10),
        collisions=collisions,
        headline=await _generate_headline(agent, shipped, in_flight, bottlenecks),
        developer_count=len(seen_devs),
        artifact_count=len(recent),
    )
    return brief.model_dump(mode="json")


def _dedupe_truncate(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _generate_headline(
    agent: "SubscriberAgent",
    shipped: list[str],
    in_flight: list[str],
    bottlenecks: list[str],
) -> str:
    if not (shipped or in_flight or bottlenecks):
        return "Quiet day — no significant artifacts received."
    # If the agent has no LLM, fall back to a terse ratio-based line.
    llm = getattr(agent, "_llm", None)
    deterministic = (
        f"{len(shipped)} shipped · {len(in_flight)} in flight"
        + (f" · {len(bottlenecks)} bottlenecks" if bottlenecks else "")
    )
    if llm is None:
        return deterministic
    try:
        prompt = (
            "Write a single-sentence engineering-manager headline (max 24 words) that captures "
            "today's team activity. Reference momentum and risk if present. No emoji. "
            "Shipped:\n- "
            + "\n- ".join(shipped[:10])
            + "\nIn flight:\n- "
            + "\n- ".join(in_flight[:10])
            + "\nBottlenecks:\n- "
            + "\n- ".join(bottlenecks[:10])
        )
        text = await llm.complete([{"role": "user", "content": prompt}], max_tokens=120)
        return (text or "").strip().strip('"').split("\n", 1)[0] or deterministic
    except Exception as exc:
        logger.debug("Brief headline LLM failed, using deterministic: %s", exc)
        return deterministic
