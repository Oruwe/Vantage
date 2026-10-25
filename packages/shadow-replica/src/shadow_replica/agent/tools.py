"""Tool registry for the manager agent.

Each tool is a small async function that wraps an existing dashboard capability
and returns JSON-serializable data. The agent sees `anthropic_schemas()` and
dispatches via `invoke()`. All tools are read-only in Phase 1.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from shadow_replica.config import ReplicaConfig
    from shadow_replica.connections.ws_hub import WSHub
    from shadow_replica.storage.received_store import ReceivedStore
    from shadow_replica.agent.subscriber_agent import SubscriberAgent

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    store: "ReceivedStore"
    hub: "WSHub"
    cfg: "ReplicaConfig"
    agent: "SubscriberAgent"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[Any]]
    requires_confirmation: bool = False


# ── Individual tool handlers ──────────────────────────────────────────────────

async def _search_artifacts(ctx: ToolContext, args: dict) -> dict:
    query = str(args.get("query", "")).strip()
    developer_id = args.get("developer_id") or None
    channel = args.get("channel") or None
    limit = int(args.get("limit") or 8)
    limit = max(1, min(limit, 20))
    if not query:
        return {"error": "query is required"}
    results = await ctx.store.semantic_search(
        query=query,
        developer_id=developer_id,
        channel=channel,
        limit=limit,
    )
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    out = []
    for r in results:
        payload = r.get("payload", {}) or {}
        out.append({
            "approval_id": r.get("approval_id", ""),
            "developer_id": r.get("developer_id", ""),
            "developer_name": dev_name_map.get(r.get("developer_id", ""), r.get("developer_id", "")),
            "artifact_type": r.get("artifact_type", ""),
            "channel": r.get("channel", ""),
            "received_at": r.get("received_at", ""),
            "score": r.get("score"),
            "summary": _summary_from(r.get("channel", ""), payload),
        })
    return {"query": query, "count": len(out), "results": out}


async def _get_timeline(ctx: ToolContext, args: dict) -> dict:
    developer_id = str(args.get("developer_id", "")).strip()
    if not developer_id:
        return {"error": "developer_id is required"}
    channel = args.get("channel") or None
    limit = int(args.get("limit") or 20)
    limit = max(1, min(limit, 100))
    rows = ctx.store.list_for_developer(developer_id, channel=channel, limit=limit)
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    items = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            payload = {}
        items.append({
            "approval_id": row.approval_id,
            "artifact_type": row.artifact_type,
            "channel": row.channel,
            "received_at": row.received_at,
            "summary": _summary_from(row.channel, payload),
        })
    return {
        "developer_id": developer_id,
        "developer_name": dev_name_map.get(developer_id, developer_id),
        "count": len(items),
        "items": items,
    }


async def _get_brief(ctx: ToolContext, args: dict) -> dict:
    hours = int(args.get("hours") or 24)
    hours = max(1, min(hours, 168))
    from shadow_replica.api.brief import build_brief
    payload = await build_brief(ctx.store, ctx.agent, ctx.cfg, hours=hours, bus=None)
    # Strip collision envelopes to keep the tool-result compact; agent can call
    # get_collisions() for details.
    collisions = payload.get("collisions") or []
    return {
        "period_start": payload.get("period_start"),
        "period_end": payload.get("period_end"),
        "headline": payload.get("headline", ""),
        "shipped": payload.get("shipped", [])[:12],
        "in_flight": payload.get("in_flight", [])[:12],
        "bottlenecks": payload.get("bottlenecks", [])[:10],
        "unresolved_questions": payload.get("unresolved_questions", [])[:10],
        "collision_count": len(collisions),
        "developer_count": payload.get("developer_count", 0),
        "artifact_count": payload.get("artifact_count", 0),
    }


async def _list_developers(ctx: ToolContext, args: dict) -> dict:
    seen = set(ctx.store.list_developers())
    online = set(ctx.hub.online_developer_ids())
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    out = []
    for dev in ctx.cfg.trusted_devs:
        seen.add(dev.developer_id)
    for dev_id in sorted(seen):
        rows = ctx.store.list_for_developer(dev_id, limit=1)
        out.append({
            "developer_id": dev_id,
            "name": dev_name_map.get(dev_id, dev_id),
            "is_online": dev_id in online,
            "last_received_at": rows[0].received_at if rows else None,
        })
    return {"count": len(out), "developers": out}


async def _get_initiatives(ctx: ToolContext, args: dict) -> dict:
    from datetime import datetime, timedelta, timezone
    from shadow_replica.initiatives.clusterer import cluster_recent

    days = int(args.get("days") or 7)
    days = max(1, min(days, 60))
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=days)
    rows = ctx.store.list_all(limit=5000)
    kept = []
    for row in rows:
        ts_s = getattr(row, "received_at", "") or ""
        try:
            s2 = ts_s.replace("Z", "+00:00") if ts_s.endswith("Z") else ts_s
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt >= period_start:
            kept.append(row)
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    inits = await cluster_recent(
        kept, dev_name_map,
        period_start=period_start, period_end=period_end,
        embedder=ctx.store._embedder,
    )
    # Compress each initiative to a scannable summary for the agent.
    out = []
    for i in inits[:15]:
        out.append({
            "initiative_id": i.initiative_id,
            "label": i.label,
            "contributors": i.contributor_names or i.contributors,
            "artifact_count": i.artifact_count,
            "shipped": i.shipped[:3],
            "in_flight": i.in_flight[:3],
            "blockers": i.blockers[:3],
            "momentum_score": round(i.momentum_score, 2),
            "risk_flag": i.risk_flag,
        })
    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "artifact_count": len(kept),
        "count": len(out),
        "initiatives": out,
    }


async def _get_threads(ctx: ToolContext, args: dict) -> dict:
    from datetime import datetime, timedelta, timezone
    from shadow_replica.api.ui_api import _build_threads

    hours = int(args.get("hours") or 72)
    hours = max(1, min(hours, 24 * 30))
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 50))
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=hours)
    rows = ctx.store.list_all(limit=5000)
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    threads = _build_threads(rows, dev_name_map, period_start, limit)
    # Trim the item list per thread so the tool-result stays readable.
    for t in threads:
        t["items"] = t["items"][:5]
    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "count": len(threads),
        "threads": threads,
    }


async def _get_collisions(ctx: ToolContext, args: dict) -> dict:
    from shadow_replica.collision.semantic import SemanticCollisionDetector
    hours = int(args.get("hours") or 48)
    hours = max(1, min(hours, 24 * 14))
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 50))
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    det = SemanticCollisionDetector(ctx.store)
    items = await det.scan_recent(
        hours=hours,
        max_collisions=limit,
        dev_name_map=dev_name_map,
    )
    return {
        "hours": hours,
        "count": len(items),
        "collisions": [c.model_dump(mode="json") for c in items],
    }


async def _get_resolutions(ctx: ToolContext, args: dict) -> dict:
    q = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 50))
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    items = []
    if q:
        try:
            hits = await ctx.store.semantic_search(
                query=q, channel="bug_resolution", limit=limit,
            )
            for hit in hits:
                payload = hit.get("payload", {}) or {}
                items.append({
                    "approval_id": hit.get("approval_id", ""),
                    "developer_id": hit.get("developer_id", ""),
                    "developer_name": dev_name_map.get(hit.get("developer_id", ""), hit.get("developer_id", "")),
                    "received_at": hit.get("received_at", ""),
                    "score": hit.get("score"),
                    "error_signature": payload.get("error_signature", "")[:120],
                    "fix_summary": payload.get("fix_summary", "")[:200],
                    "repo": payload.get("repo", ""),
                })
            if items:
                return {"query": q, "count": len(items), "resolutions": items}
        except Exception as exc:
            logger.debug("resolutions semantic_search failed: %s", exc)
    rows = ctx.store.list_all(limit=500, channel="bug_resolution")
    q_low = q.lower()
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            continue
        if q_low:
            hay = " ".join([
                str(payload.get("error_signature", "")),
                str(payload.get("fix_summary", "")),
            ]).lower()
            if q_low not in hay:
                continue
        items.append({
            "approval_id": row.approval_id,
            "developer_id": row.developer_id,
            "developer_name": dev_name_map.get(row.developer_id, row.developer_id),
            "received_at": row.received_at,
            "error_signature": payload.get("error_signature", "")[:120],
            "fix_summary": payload.get("fix_summary", "")[:200],
            "repo": payload.get("repo", ""),
        })
        if len(items) >= limit:
            break
    return {"query": q, "count": len(items), "resolutions": items}


async def _probe_live(ctx: ToolContext, args: dict) -> dict:
    """Fire a live WebSocket probe at a developer's ghost. Phase-4 dangerous tool.

    Side-effect: emits probe envelopes on the Wiretap and may surface in the
    dev's local X-Ray Studio. Requires manager confirmation before execution.
    """
    from vantage.schemas import ProbeRequest
    from shadow_replica.resilience.fallback import generate_fallback

    developer_id = (args.get("developer_id") or "").strip()
    repo_hint = (args.get("repo_hint") or "").strip()
    if not developer_id:
        return {"error": "developer_id required"}

    matched = next((d for d in ctx.cfg.trusted_devs if d.developer_id == developer_id), None)
    sub_id = matched.subscriber_id if matched else ""
    req = ProbeRequest(subscriber_id=sub_id, repo_hint=repo_hint)
    try:
        resp = await ctx.hub.probe(developer_id, req)
    except Exception as exc:
        return {"error": f"probe failed: {exc}"}

    if resp is not None:
        if resp.snapshot and resp.snapshot.activity_signals:
            try:
                ctx.hub.cache_activity(
                    developer_id,
                    resp.snapshot.activity_signals,
                    resp.snapshot.captured_at,
                )
            except Exception:
                pass
        payload = resp.model_dump(mode="json")
        return {
            "developer_id": developer_id,
            "status": "probed",
            "policy_decision": payload.get("policy_decision"),
            "snapshot": payload.get("snapshot"),
            "fallback_summary": payload.get("fallback_summary", ""),
            "staleness_seconds": payload.get("staleness_seconds", 0),
        }

    try:
        fb = generate_fallback(ctx.store, developer_id, hub=ctx.hub)
        fb.request_id = req.request_id
        payload = fb.model_dump(mode="json")
    except Exception as exc:
        return {"developer_id": developer_id, "status": "offline", "error": f"fallback failed: {exc}"}
    return {
        "developer_id": developer_id,
        "status": "offline",
        "policy_decision": payload.get("policy_decision"),
        "fallback_summary": payload.get("fallback_summary", ""),
        "staleness_seconds": payload.get("staleness_seconds", 0),
    }


async def _ask_dev(ctx: ToolContext, args: dict) -> dict:
    """Fan a free-form question to one developer's ghost. Phase-4 dangerous tool.

    Side-effect: shows up in the dev's local UI as a discovery prompt from the
    manager. Requires confirmation.
    """
    developer_id = (args.get("developer_id") or "").strip()
    question = (args.get("question") or "").strip()
    if not developer_id:
        return {"error": "developer_id required"}
    if not question:
        return {"error": "question required"}
    try:
        results = await ctx.hub.converse(question, developer_ids=[developer_id])
    except Exception as exc:
        return {"error": f"ask_dev failed: {exc}"}
    answer = results[0] if results else {"developer_id": developer_id, "status": "offline", "answer": ""}
    return {
        "developer_id": developer_id,
        "question": question,
        "status": answer.get("status", ""),
        "answer": answer.get("answer", ""),
        "latency_ms": answer.get("latency_ms"),
    }


async def _get_github_events(ctx: ToolContext, args: dict) -> dict:
    from datetime import datetime, timedelta, timezone
    developer_id = (args.get("developer_id") or "").strip() or None
    days = int(args.get("days") or 7)
    days = max(1, min(days, 60))
    limit = int(args.get("limit") or 20)
    limit = max(1, min(limit, 100))
    period_start = datetime.now(timezone.utc) - timedelta(days=days)

    if developer_id:
        rows = ctx.store.list_for_developer(developer_id, channel="github_event", limit=limit * 2)
    else:
        rows = ctx.store.list_all(channel="github_event", limit=limit * 4)

    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    out = []
    for row in rows:
        ts_s = getattr(row, "received_at", "") or ""
        try:
            s2 = ts_s.replace("Z", "+00:00") if ts_s.endswith("Z") else ts_s
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < period_start:
            continue
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            continue
        out.append({
            "approval_id": row.approval_id,
            "developer_id": row.developer_id,
            "developer_name": dev_name_map.get(row.developer_id, row.developer_id),
            "event_type": payload.get("event_type", ""),
            "action": payload.get("action", ""),
            "repo": payload.get("repo", ""),
            "pr_number": payload.get("pr_number"),
            "title": payload.get("title", "")[:200],
            "summary": payload.get("summary", "")[:200],
            "url": payload.get("url", ""),
            "received_at": row.received_at,
            "followup_summary": payload.get("followup_summary", "")[:200],
            "followup_status": payload.get("followup_status", ""),
        })
        if len(out) >= limit:
            break
    return {"count": len(out), "events": out}


async def _get_friction(ctx: ToolContext, args: dict) -> dict:
    developer_id = (args.get("developer_id") or "").strip()
    reports = ctx.hub.friction_all()
    dev_name_map = {d.developer_id: d.name for d in ctx.cfg.trusted_devs}
    if developer_id:
        r = reports.get(developer_id)
        if r is None:
            return {"developer_id": developer_id, "report": None}
        return {
            "developer_id": developer_id,
            "developer_name": dev_name_map.get(developer_id, developer_id),
            "report": r.model_dump(mode="json"),
        }
    out = []
    for dev_id, r in reports.items():
        out.append({
            "developer_id": dev_id,
            "developer_name": dev_name_map.get(dev_id, dev_id),
            "score": r.score,
            "summary": r.summary,
            "minutes_since_commit": r.minutes_since_commit,
            "hot_files": r.hot_files[:5],
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"count": len(out), "friction": out}


# ── Registry ─────────────────────────────────────────────────────────────────

_TOOL_DEFS: list[Tool] = [
    Tool(
        name="search_artifacts",
        description=(
            "Semantic search over all approved artifacts (commits, standups, "
            "digests, PR drafts, bug resolutions, GitHub events). Use this for "
            "'why did we…' or 'what happened with…' questions. Returns ranked "
            "results with a short summary per hit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query"},
                "developer_id": {"type": "string", "description": "Optional — filter to one developer"},
                "channel": {
                    "type": "string",
                    "description": "Optional — one of commit_report, standup, pr_draft, weekly_digest, bug_resolution, github_event, quick_note",
                },
                "limit": {"type": "integer", "description": "Max results (1-20, default 8)"},
            },
            "required": ["query"],
        },
        handler=_search_artifacts,
    ),
    Tool(
        name="get_timeline",
        description=(
            "Return one developer's most recent artifacts in reverse-chronological "
            "order. Use this when the manager asks what a specific person has been "
            "doing. Optionally filter to one channel."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "developer_id": {"type": "string"},
                "channel": {"type": "string"},
                "limit": {"type": "integer", "description": "Max items (1-100, default 20)"},
            },
            "required": ["developer_id"],
        },
        handler=_get_timeline,
    ),
    Tool(
        name="get_brief",
        description=(
            "Synthesize a team daily brief covering the last N hours. Returns "
            "shipped/in-flight/bottlenecks/unresolved + a one-line headline. "
            "Use this when the manager wants a cross-team overview."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Lookback window (1-168, default 24)"},
            },
        },
        handler=_get_brief,
    ),
    Tool(
        name="list_developers",
        description=(
            "List all trusted developers with their online/offline status and "
            "last-received-at timestamp. Use at the start of broad investigations "
            "to find the right person to drill into."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_list_developers,
    ),
    Tool(
        name="get_friction",
        description=(
            "Return the latest AI-friction report per developer, or for one dev "
            "if `developer_id` is given. Score is 0-1 (higher = more stuck). "
            "Use this to find who may need help."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "developer_id": {"type": "string", "description": "Optional — single-dev lookup"},
            },
        },
        handler=_get_friction,
    ),
    Tool(
        name="get_initiatives",
        description=(
            "Return clustered team initiatives from the last N days — groups of "
            "artifacts that share semantic/lexical overlap (e.g. 'auth refactor', "
            "'payments migration'). Each initiative has contributors, momentum "
            "score, and shipped / in-flight / blockers. Use for cross-cutting "
            "themes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback window in days (1-60, default 7)"},
            },
        },
        handler=_get_initiatives,
    ),
    Tool(
        name="get_threads",
        description=(
            "Return 'engineering threads' from the last N hours — bundles of "
            "github_events, commit_reports, and pr_drafts grouped by shared "
            "branch or commit SHA. Use to see one story per branch (PR + commits "
            "+ CI result as a unit)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Lookback window in hours (1-720, default 72)"},
                "limit": {"type": "integer", "description": "Max threads (1-50, default 10)"},
            },
        },
        handler=_get_threads,
    ),
    Tool(
        name="get_collisions",
        description=(
            "Pre-commit semantic collision radar. Returns pairs of developers "
            "whose recent work is either semantically similar (embedding cosine) "
            "or shares file paths. Severity: high / medium / low. Use when the "
            "manager asks about overlapping work or merge-conflict risk."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Lookback window in hours (1-336, default 48)"},
                "limit": {"type": "integer", "description": "Max collisions returned (1-50, default 10)"},
            },
        },
        handler=_get_collisions,
    ),
    Tool(
        name="get_resolutions",
        description=(
            "Search bug_resolution artifacts — captured struggle-then-success "
            "patterns from developer sessions. With a query, performs semantic "
            "search; without, returns the latest. Each result has an error "
            "signature + a one-line fix summary. Use for 'has anyone hit this "
            "error before?' questions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional — error text or symptom to search for"},
                "limit": {"type": "integer", "description": "Max results (1-50, default 10)"},
            },
        },
        handler=_get_resolutions,
    ),
    Tool(
        name="get_github_events",
        description=(
            "Return recent GitHub events (PR open/close/review, pushes, CI "
            "results) over the last N days, optionally filtered to one developer. "
            "Each event includes event_type, PR number, summary, and a followup "
            "summary when the replica probed the dev's live session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "developer_id": {"type": "string", "description": "Optional — filter to one dev"},
                "days": {"type": "integer", "description": "Lookback window in days (1-60, default 7)"},
                "limit": {"type": "integer", "description": "Max events (1-100, default 20)"},
            },
        },
        handler=_get_github_events,
    ),
    Tool(
        name="probe_live",
        description=(
            "Fire a live WebSocket probe at a developer's ghost to see what "
            "files they're actively touching, their inferred current intent, "
            "and recent session turns. REQUIRES manager confirmation — this is "
            "visible to the developer in their local UI. Use when a stale "
            "timeline isn't enough and real-time context would unblock the "
            "manager's question."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "developer_id": {"type": "string"},
                "repo_hint": {"type": "string", "description": "Optional — scope the probe to one repo"},
            },
            "required": ["developer_id"],
        },
        handler=_probe_live,
        requires_confirmation=True,
    ),
    Tool(
        name="ask_dev",
        description=(
            "Ask one developer's ghost a free-form question. The question "
            "surfaces in their local X-Ray Studio and their ghost LLM composes "
            "an answer scoped to approved content. REQUIRES manager confirmation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "developer_id": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["developer_id", "question"],
        },
        handler=_ask_dev,
        requires_confirmation=True,
    ),
]


def tool_names() -> list[str]:
    return [t.name for t in _TOOL_DEFS]


def requires_confirmation(name: str) -> bool:
    """Is this tool dangerous enough to need manager confirmation before running?"""
    for t in _TOOL_DEFS:
        if t.name == name:
            return bool(t.requires_confirmation)
    return False


def anthropic_schemas() -> list[dict]:
    """Shape expected by `anthropic.messages.create(..., tools=[...])`."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in _TOOL_DEFS
    ]


async def invoke(ctx: ToolContext, name: str, args: dict) -> Any:
    """Dispatch a tool call. Returns a JSON-serializable value or an error dict."""
    for t in _TOOL_DEFS:
        if t.name == name:
            try:
                return await t.handler(ctx, args or {})
            except Exception as exc:
                logger.warning("Tool %s failed: %s", name, exc)
                return {"error": f"tool {name} failed: {exc}"}
    return {"error": f"unknown tool: {name}"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _summary_from(channel: str, payload: dict) -> str:
    try:
        ch = (channel or "").lower()
        if ch == "commit_report":
            msg = payload.get("author_msg") or payload.get("generated_summary", "")
            return str(msg)[:160]
        if ch == "weekly_digest":
            highlights = payload.get("highlights", [])
            return (highlights[0] if highlights else "Weekly digest")[:160]
        if ch == "standup":
            today = payload.get("today", [])
            return (today[0] if today else "Standup")[:160]
        if ch == "pr_draft":
            return str(payload.get("title", "PR draft"))[:160]
        if ch == "bug_resolution":
            sig = str(payload.get("error_signature", ""))[:80]
            fix = str(payload.get("fix_summary", ""))[:60]
            return f"{sig} → {fix}" if sig else "bug resolution"
        if ch == "github_event":
            base = str(payload.get("summary") or payload.get("title") or "")[:160]
            return base or "GitHub event"
        if ch == "live_probe":
            return str(payload.get("fallback_summary", "probe"))[:160]
        if ch == "quick_note":
            return str(payload.get("message", ""))[:160]
    except Exception:
        pass
    return channel
