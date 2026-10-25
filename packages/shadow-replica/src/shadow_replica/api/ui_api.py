from __future__ import annotations

import hmac as _hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

if TYPE_CHECKING:
    from shadow_replica.agent.subscriber_agent import SubscriberAgent
    from shadow_replica.agent.manager_agent import ManagerAgent
    from shadow_replica.connections.ws_hub import WSHub
    from shadow_replica.storage.received_store import ReceivedStore
    from shadow_replica.config import ReplicaConfig

logger = logging.getLogger(__name__)


def _require_manager(request: Request, manager_token: str) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Manager token required")
    provided = auth.removeprefix("Bearer ").strip()
    if not manager_token or not _hmac.compare_digest(provided, manager_token):
        raise HTTPException(status_code=401, detail="Invalid manager token")


def _extract_summary(channel: str, payload: dict) -> str:
    try:
        ch = channel.lower()
        if ch == "commit_report":
            return payload.get("author_msg", "")[:120]
        elif ch == "weekly_digest":
            highlights = payload.get("highlights", [])
            return highlights[0][:120] if highlights else "Weekly digest"
        elif ch == "standup":
            today = payload.get("today", [])
            return today[0][:120] if today else "Standup update"
        elif ch == "pr_draft":
            return payload.get("title", "PR draft")[:120]
        elif ch == "live_probe":
            summary = payload.get("fallback_summary", "")
            return summary[:120] if summary else f"Probe: {payload.get('policy_decision', 'unknown')}"
        elif ch == "bug_resolution":
            sig = payload.get("error_signature", "")[:80]
            fix = payload.get("fix_summary", "")[:40]
            return f"{sig} → {fix}" if sig else "bug resolution"
        elif ch == "github_event":
            base = (payload.get("summary") or payload.get("title") or "")[:120]
            followup = (payload.get("followup_summary") or "").strip()
            if followup:
                return f"{base} · {followup[:120]}"
            return base or "GitHub event"
    except Exception:
        pass
    return channel


def make_ui_router(
    store: "ReceivedStore",
    hub: "WSHub",
    cfg: "ReplicaConfig",
    agent: "SubscriberAgent | None" = None,
    bus=None,
    manager_agent: "ManagerAgent | None" = None,
) -> APIRouter:
    # `bus` accepted so manager-initiated probes/queries can emit envelopes.
    r = APIRouter(prefix="/ui/api")

    @r.get("/status")
    async def get_status(request: Request):
        _require_manager(request, cfg.manager_token)
        online = hub.online_developer_ids()
        devs = store.list_developers()
        all_rows = store.list_all(limit=10000)
        today = datetime.now(timezone.utc).date().isoformat()
        today_count = sum(1 for row in all_rows if row.received_at and row.received_at[:10] == today)
        return JSONResponse({
            "online_devs": online,
            "total_devs": len(devs),
            "total_artifacts": len(all_rows),
            "artifacts_today": today_count,
        })

    @r.get("/developers")
    async def get_developers(request: Request):
        _require_manager(request, cfg.manager_token)
        dev_ids = store.list_developers()
        online = set(hub.online_developer_ids())
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        result = []
        seen = set()
        for dev_id in dev_ids:
            seen.add(dev_id)
            rows = store.list_for_developer(dev_id, limit=1000)
            channel_counts: dict[str, int] = {}
            for row in rows:
                channel_counts[row.channel] = channel_counts.get(row.channel, 0) + 1
            result.append({
                "developer_id": dev_id,
                "name": dev_name_map.get(dev_id, dev_id),
                "is_online": dev_id in online,
                "last_received_at": rows[0].received_at if rows else None,
                "artifact_count": len(rows),
                "recent_channels": list(dict.fromkeys(row.channel for row in rows[:20]))[:5],
                "channel_counts": channel_counts,
            })
        for dev in cfg.trusted_devs:
            if dev.developer_id not in seen:
                result.append({
                    "developer_id": dev.developer_id,
                    "name": dev.name,
                    "is_online": dev.developer_id in online,
                    "last_received_at": None,
                    "artifact_count": 0,
                    "recent_channels": [],
                    "channel_counts": {},
                })
        return JSONResponse(result)

    @r.get("/activity")
    async def get_activity(request: Request, limit: int = 30):
        _require_manager(request, cfg.manager_token)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        rows = store.list_all(limit=limit)
        items = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except Exception:
                payload = {}
            items.append({
                "developer_id": row.developer_id,
                "developer_name": dev_name_map.get(row.developer_id, row.developer_id),
                "approval_id": row.approval_id,
                "artifact_type": row.artifact_type,
                "channel": row.channel,
                "received_at": row.received_at,
                "summary": _extract_summary(row.channel, payload),
            })
        return JSONResponse(items)

    @r.get("/timeline/{developer_id}")
    async def get_timeline(
        request: Request,
        developer_id: str,
        channel: str | None = None,
        limit: int = 50,
    ):
        _require_manager(request, cfg.manager_token)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        online = set(hub.online_developer_ids())
        rows = store.list_for_developer(developer_id, channel=channel, limit=limit)
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
                "approved_at": row.approved_at,
                "summary": _extract_summary(row.channel, payload),
                "payload": payload,
            })
        return JSONResponse({
            "developer_id": developer_id,
            "developer_name": dev_name_map.get(developer_id, developer_id),
            "is_online": developer_id in online,
            "items": items,
        })

    @r.post("/probe/{developer_id}")
    async def probe_developer(request: Request, developer_id: str):
        _require_manager(request, cfg.manager_token)
        body = await request.body()
        repo_hint = ""
        if body:
            try:
                data = json.loads(body)
                repo_hint = data.get("repo_hint", "") if isinstance(data, dict) else ""
            except Exception:
                pass

        from vantage.schemas import ProbeRequest
        from shadow_replica.resilience.fallback import generate_fallback

        matched = next((d for d in cfg.trusted_devs if d.developer_id == developer_id), None)
        sub_id = matched.subscriber_id if matched else ""
        probe_req = ProbeRequest(subscriber_id=sub_id, repo_hint=repo_hint)

        # Always call hub.probe — it emits a wiretap envelope and returns None
        # cleanly when the dev is offline, so we fall through to the fallback.
        response = await hub.probe(developer_id, probe_req)
        if response is not None:
            if response.snapshot and response.snapshot.activity_signals:
                hub.cache_activity(
                    developer_id,
                    response.snapshot.activity_signals,
                    response.snapshot.captured_at,
                )
            return JSONResponse(response.model_dump(mode="json"))

        fallback = generate_fallback(store, developer_id, hub=hub)
        fallback.request_id = probe_req.request_id
        return JSONResponse(fallback.model_dump(mode="json"))

    @r.get("/sparklines")
    async def get_sparklines(request: Request, days: int = 14):
        _require_manager(request, cfg.manager_token)
        from datetime import date as _date
        today = _date.today()
        all_rows = store.list_all(limit=50000)
        result: dict[str, list[int]] = {dev_id: [0] * days for dev_id in store.list_developers()}
        for dev in cfg.trusted_devs:
            if dev.developer_id not in result:
                result[dev.developer_id] = [0] * days
        for row in all_rows:
            if not row.received_at:
                continue
            try:
                row_date = _date.fromisoformat(row.received_at[:10])
                idx = (today - row_date).days
                if 0 <= idx < days:
                    dev = row.developer_id
                    if dev not in result:
                        result[dev] = [0] * days
                    result[dev][days - 1 - idx] += 1
            except Exception:
                pass
        return JSONResponse(result)

    @r.post("/query")
    async def query(request: Request):
        _require_manager(request, cfg.manager_token)
        body = await request.body()
        try:
            data = json.loads(body)
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        q = data.get("query", "")
        developer_id = data.get("developer_id") or None
        channel = data.get("channel") or None
        if not q:
            raise HTTPException(400, "query required")
        _agent = agent
        if _agent is None:
            from shadow_replica.agent.subscriber_agent import SubscriberAgent
            _agent = SubscriberAgent(store, embedder=store._embedder)

        req_env = None
        if bus is not None:
            from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent
            req_env = A2AEnvelope(
                intent=A2AIntent.INTENT_DISCOVERY,
                from_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                to_agent=A2AAgentRef(kind="subscriber-agent", label="Subscriber Agent"),
                summary=f"query: {q[:120]}",
                payload={"query": q, "developer_id": developer_id, "channel": channel},
            )
            try:
                bus.publish(req_env)
            except Exception:
                pass

        t0 = time.monotonic()
        result = await _agent.run_async(q, developer_id=developer_id, channel=channel)
        if bus is not None and req_env is not None:
            from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent
            latency_ms = int((time.monotonic() - t0) * 1000)
            ans = result.get("answer", "") if isinstance(result, dict) else ""
            try:
                bus.publish(A2AEnvelope(
                    session_id=req_env.turn_id,
                    parent_turn_id=req_env.turn_id,
                    intent=A2AIntent.SYNTHESIS_ACK,
                    from_agent=A2AAgentRef(kind="subscriber-agent", label="Subscriber Agent"),
                    to_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                    latency_ms=latency_ms,
                    summary=f"answer: {str(ans)[:120]}",
                    payload={"answer_preview": str(ans)[:400]},
                ))
            except Exception:
                pass
        return JSONResponse(result)

    @r.get("/search")
    async def search(
        request: Request,
        q: str,
        developer_id: str | None = None,
        channel: str | None = None,
        limit: int = 10,
    ):
        """Semantic search over received artifacts."""
        _require_manager(request, cfg.manager_token)
        if not q:
            raise HTTPException(400, "q required")
        results = await store.semantic_search(
            query=q,
            developer_id=developer_id or None,
            channel=channel or None,
            limit=min(limit, 50),
        )
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        for r in results:
            r["developer_name"] = dev_name_map.get(r.get("developer_id", ""), r.get("developer_id", ""))
            r["summary"] = _extract_summary(r.get("channel", ""), r.get("payload", {}))
        return JSONResponse({"query": q, "results": results})

    @r.get("/stats")
    async def stats(request: Request):
        """Storage statistics."""
        _require_manager(request, cfg.manager_token)
        total = store.count()
        devs = store.list_developers()
        return JSONResponse({
            "total_artifacts": total,
            "total_developers": len(devs),
            "storage_backend": "qdrant",
        })

    @r.post("/converse")
    async def converse(request: Request):
        """Broadcast a question to online ghosts in parallel; return their answers."""
        _require_manager(request, cfg.manager_token)
        body = await request.body()
        try:
            data = json.loads(body)
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        prompt = (data.get("prompt") or "").strip()
        dev_ids = data.get("developer_ids") or None
        if not prompt:
            raise HTTPException(400, "prompt required")
        results = await hub.converse(prompt, developer_ids=dev_ids)
        return JSONResponse({"prompt": prompt, "results": results})

    @r.get("/friction")
    async def friction(request: Request):
        """Latest friction report for each developer the hub has heard from."""
        _require_manager(request, cfg.manager_token)
        reports = hub.friction_all()
        return JSONResponse({
            dev_id: r.model_dump(mode="json") for dev_id, r in reports.items()
        })

    @r.get("/resolutions")
    async def resolutions(
        request: Request,
        q: str = "",
        limit: int = 50,
    ):
        """List or search bug_resolution artifacts shared across the team.

        - `q=""` → latest resolutions regardless of signature.
        - `q=<text>` → semantic search over resolutions when embeddings available;
          otherwise plain substring match on error_signature + fix_summary.
        """
        _require_manager(request, cfg.manager_token)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        limit = max(1, min(limit, 200))
        items = []
        if q.strip():
            try:
                hits = await store.semantic_search(
                    query=q.strip(), channel="bug_resolution", limit=limit,
                )
                for hit in hits:
                    payload = hit.get("payload", {}) or {}
                    items.append({
                        "approval_id": hit.get("approval_id"),
                        "developer_id": hit.get("developer_id"),
                        "developer_name": dev_name_map.get(hit.get("developer_id", ""), hit.get("developer_id", "")),
                        "received_at": hit.get("received_at"),
                        "score": hit.get("score"),
                        "resolution": payload,
                    })
                if items:
                    return JSONResponse({"query": q, "items": items})
            except Exception as exc:
                logger.debug("semantic_search for resolutions failed: %s", exc)
        rows = store.list_all(limit=1000, channel="bug_resolution")
        q_low = q.strip().lower()
        for row in rows[:limit if not q_low else 1000]:
            try:
                payload = json.loads(row.payload_json)
            except Exception:
                continue
            if q_low:
                hay = " ".join([
                    str(payload.get("error_signature", "")),
                    str(payload.get("fix_summary", "")),
                    str(payload.get("error_sample", "")),
                ]).lower()
                if q_low not in hay:
                    continue
            items.append({
                "approval_id": row.approval_id,
                "developer_id": row.developer_id,
                "developer_name": dev_name_map.get(row.developer_id, row.developer_id),
                "received_at": row.received_at,
                "resolution": payload,
            })
            if len(items) >= limit:
                break
        return JSONResponse({"query": q, "items": items})

    @r.get("/initiatives")
    async def initiatives(request: Request, days: int = 7, limit: int = 500):
        """List clustered initiatives across the whole team."""
        _require_manager(request, cfg.manager_token)
        from datetime import datetime, timedelta, timezone
        from shadow_replica.initiatives.clusterer import cluster_recent
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=max(1, min(days, 60)))
        rows = store.list_all(limit=max(1, min(limit, 5000)))
        # Keep only rows inside the window (store.list_all is newest-first)
        kept = []
        for row in rows:
            ts_s = getattr(row, "received_at", "")
            if not ts_s:
                continue
            try:
                s2 = ts_s.replace("Z", "+00:00") if ts_s.endswith("Z") else ts_s
                dt = datetime.fromisoformat(s2)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if dt >= period_start:
                kept.append(row)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        inits = await cluster_recent(
            kept, dev_name_map,
            period_start=period_start, period_end=period_end,
            embedder=store._embedder,
        )
        return JSONResponse({
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "artifact_count": len(kept),
            "initiatives": [i.model_dump(mode="json") for i in inits],
        })

    @r.get("/initiatives/{initiative_id}")
    async def initiative_detail(request: Request, initiative_id: str, days: int = 14):
        """Detail view for one initiative (re-runs clustering so the ID stays stable."""
        _require_manager(request, cfg.manager_token)
        from datetime import datetime, timedelta, timezone
        from shadow_replica.initiatives.clusterer import cluster_recent
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=max(1, min(days, 60)))
        rows = store.list_all(limit=5000)
        kept = []
        for row in rows:
            ts_s = getattr(row, "received_at", "")
            if not ts_s:
                continue
            try:
                s2 = ts_s.replace("Z", "+00:00") if ts_s.endswith("Z") else ts_s
                dt = datetime.fromisoformat(s2)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if dt >= period_start:
                kept.append(row)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        inits = await cluster_recent(
            kept, dev_name_map,
            period_start=period_start, period_end=period_end,
            embedder=store._embedder,
        )
        for i in inits:
            if i.initiative_id == initiative_id:
                return JSONResponse(i.model_dump(mode="json"))
        raise HTTPException(404, "Initiative not found (may have fallen out of window)")

    @r.get("/brief")
    async def brief(request: Request, hours: int = 24):
        """Synthesize a cross-team daily briefing over the last `hours` window."""
        _require_manager(request, cfg.manager_token)
        from shadow_replica.api.brief import build_brief
        _agent = agent
        if _agent is None:
            from shadow_replica.agent.subscriber_agent import SubscriberAgent
            _agent = SubscriberAgent(store, embedder=store._embedder)
        payload = await build_brief(store, _agent, cfg, hours=max(1, min(hours, 168)), bus=bus)
        return JSONResponse(payload)

    @r.get("/collisions")
    async def collisions(request: Request, hours: int = 48, limit: int = 20):
        """Pre-commit semantic collision radar.

        Returns pairs of developers whose recent artifacts are either:
            - semantically similar (cosine ≥ threshold) + share key terms, or
            - share one or more file paths (regardless of similarity)
        Catches overlapping AI-assisted refactors before merge conflicts land.
        """
        _require_manager(request, cfg.manager_token)
        from shadow_replica.collision.semantic import SemanticCollisionDetector
        detector = SemanticCollisionDetector(store)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        items = await detector.scan_recent(
            hours=max(1, min(hours, 24 * 14)),
            max_collisions=max(1, min(limit, 100)),
            dev_name_map=dev_name_map,
        )
        return JSONResponse({
            "hours": hours,
            "count": len(items),
            "collisions": [c.model_dump(mode="json") for c in items],
        })

    # ── Manager agent (M13 Phase 1) ──────────────────────────────────────────

    @r.post("/chat")
    async def chat(request: Request):
        """Send a message to the tool-calling manager agent.

        Body: {"message": str, "conversation_id": str?}
        Returns: {"conversation_id", "assistant_message", "tool_calls": [...], ...}
        """
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            raise HTTPException(503, "Manager agent not configured. Set [llm] provider=anthropic in replica.toml and export ANTHROPIC_API_KEY.")
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        message = (data.get("message") or "").strip()
        conversation_id = (data.get("conversation_id") or "").strip() or None
        if not message:
            raise HTTPException(400, "message required")

        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        try:
            result = await manager_agent.run_turn(
                manager_token=auth_token,
                message=message,
                conversation_id=conversation_id,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        return JSONResponse({
            "conversation_id": result.conversation_id,
            "assistant_message": result.assistant_message,
            "tool_calls": [
                {
                    "tool_use_id": tc.tool_use_id,
                    "tool_name": tc.tool_name,
                    "tool_input": tc.tool_input,
                    "tool_output_preview": tc.tool_output_preview,
                    "latency_ms": tc.latency_ms,
                }
                for tc in result.tool_calls
            ],
            "stop_reason": result.stop_reason,
            "loops": result.loops,
            "model": result.model,
            "budget_exhausted": result.budget_exhausted,
        })

    @r.post("/chat/stream")
    async def chat_stream(request: Request):
        """SSE variant of /chat. Yields chunks per round of the agent loop.

        Response is `text/event-stream`. Each event is a JSON object:
            event: started    data: {"conversation_id": "..."}
            event: tool_call  data: {"id": "...", "tool": "...", "args": {...}}
            event: tool_result data: {"id": "...", "preview": "...", "latency_ms": N}
            event: message_delta data: {"content": "..."}
            event: done       data: {"conversation_id": "...", "loops": N, ...}
            event: error      data: {"detail": "..."}
        """
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            raise HTTPException(503, "Manager agent not configured. Set [llm] provider=anthropic in replica.toml and export ANTHROPIC_API_KEY.")
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        message = (data.get("message") or "").strip()
        conversation_id = (data.get("conversation_id") or "").strip() or None
        if not message:
            raise HTTPException(400, "message required")
        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()

        async def _event_stream():
            try:
                async for chunk in manager_agent.run_turn_stream(
                    manager_token=auth_token,
                    message=message,
                    conversation_id=conversation_id,
                ):
                    event_name = chunk.get("type", "message")
                    payload = {k: v for k, v in chunk.items() if k != "type"}
                    yield f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"
            except Exception as exc:
                logger.exception("chat stream failed")
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @r.post("/chat/approve")
    async def chat_approve(request: Request):
        """Resume a paused agent run after the manager says yes/no on a
        dangerous tool call. Streams the continuation as SSE."""
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            raise HTTPException(503, "Manager agent not configured.")
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        tool_use_id = (data.get("tool_use_id") or "").strip()
        if not tool_use_id:
            raise HTTPException(400, "tool_use_id required")
        approved = bool(data.get("approved"))
        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()

        async def _event_stream():
            try:
                async for chunk in manager_agent.resume_after_approval(
                    manager_token=auth_token,
                    tool_use_id=tool_use_id,
                    approved=approved,
                ):
                    event_name = chunk.get("type", "message")
                    payload = {k: v for k, v in chunk.items() if k != "type"}
                    yield f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"
            except Exception as exc:
                logger.exception("chat approve stream failed")
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @r.get("/chat/pending/{tool_use_id}")
    async def chat_pending(request: Request, tool_use_id: str):
        """Peek at a pending approval so the UI can show the full args to the manager."""
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            raise HTTPException(503, "Manager agent not configured")
        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        pending = manager_agent.pending_approval(auth_token, tool_use_id)
        if pending is None:
            raise HTTPException(404, "No pending approval with that id")
        return JSONResponse(pending)

    @r.get("/chat/conversations")
    async def list_chat_conversations(request: Request, limit: int = 20):
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            return JSONResponse({"conversations": []})
        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return JSONResponse({
            "conversations": manager_agent.list_conversations(auth_token, limit=limit),
        })

    @r.get("/chat/conversations/{conversation_id}")
    async def get_chat_conversation(request: Request, conversation_id: str):
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            raise HTTPException(503, "Manager agent not configured")
        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        turns = manager_agent.list_turns(auth_token, conversation_id)
        return JSONResponse({"conversation_id": conversation_id, "turns": turns})

    @r.delete("/chat/conversations/{conversation_id}")
    async def delete_chat_conversation(request: Request, conversation_id: str):
        _require_manager(request, cfg.manager_token)
        if manager_agent is None:
            raise HTTPException(503, "Manager agent not configured")
        auth_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        ok = manager_agent.delete_conversation(auth_token, conversation_id)
        if not ok:
            raise HTTPException(404, "Conversation not found")
        return JSONResponse({"ok": True, "conversation_id": conversation_id})

    # ── Developer management (hot-add / remove, no restart) ──────────────────

    @r.post("/pair/decode")
    async def decode_pairing(request: Request):
        """Preview what a pairing code contains before confirming add."""
        _require_manager(request, cfg.manager_token)
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        code = (data.get("pairing_code") or "").strip()
        if not code:
            raise HTTPException(400, "pairing_code required")
        parsed = _decode_pairing_code(code)
        if parsed is None:
            raise HTTPException(400, "Could not decode pairing code")
        return JSONResponse(parsed)

    @r.post("/developers/add")
    async def add_developer(request: Request):
        """Add or update a trusted developer. Accepts a pairing code OR raw fields.

        Both the receive router and the WebSocket endpoint read `cfg.trusted_devs`
        by reference, so appending here takes effect immediately — no restart.
        """
        _require_manager(request, cfg.manager_token)
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        pairing_code = (data.get("pairing_code") or "").strip()
        if pairing_code:
            parsed = _decode_pairing_code(pairing_code)
            if parsed is None:
                raise HTTPException(400, "Could not decode pairing code")
            developer_id = parsed.get("developer_id", "")
            name = (data.get("name") or parsed.get("name") or "").strip()
            token = parsed.get("token", "")
            subscriber_id = parsed.get("subscriber_id", "")
            github_login = (data.get("github_login") or parsed.get("github_login") or "").strip()
        else:
            developer_id = (data.get("developer_id") or "").strip()
            name = (data.get("name") or developer_id).strip()
            token = (data.get("token") or "").strip()
            subscriber_id = (data.get("subscriber_id") or "").strip()
            github_login = (data.get("github_login") or "").strip()

        if not developer_id:
            raise HTTPException(400, "developer_id is required")
        if not token or not token.startswith("vsub_"):
            raise HTTPException(400, "A valid vsub_… token is required")
        if not name:
            name = developer_id

        from shadow_replica.config import TrustedDev, add_trusted_dev

        add_trusted_dev(
            developer_id=developer_id,
            name=name,
            token_value=token,
            subscriber_id=subscriber_id,
            github_login=github_login,
        )

        # Mutate the live list so running routers accept this dev immediately.
        existing = next((d for d in cfg.trusted_devs if d.developer_id == developer_id), None)
        if existing is not None:
            existing.name = name
            existing.token_value = token
            if subscriber_id:
                existing.subscriber_id = subscriber_id
            if github_login:
                existing.github_login = github_login
        else:
            cfg.trusted_devs.append(TrustedDev(
                developer_id=developer_id,
                name=name,
                token_value=token,
                subscriber_id=subscriber_id,
                github_login=github_login,
            ))

        # Retroactively claim any GitHub events that arrived before this dev was paired.
        reassigned = 0
        if github_login:
            reassigned = store.reassign_developer_id(f"unmapped:{github_login}", developer_id)

        return JSONResponse({
            "ok": True,
            "developer_id": developer_id,
            "name": name,
            "subscriber_id": subscriber_id,
            "github_login": github_login,
            "reassigned_artifacts": reassigned,
        })

    @r.delete("/developers/{developer_id}")
    async def remove_developer(request: Request, developer_id: str):
        """Remove a trusted developer. Their stored artifacts are kept."""
        _require_manager(request, cfg.manager_token)
        before = len(cfg.trusted_devs)
        cfg.trusted_devs[:] = [d for d in cfg.trusted_devs if d.developer_id != developer_id]
        if len(cfg.trusted_devs) == before:
            raise HTTPException(404, "Developer not found")

        # Persist by rewriting the TOML (reuse the shared writer).
        from shadow_replica.config import _write, _replica_config_dir
        from pathlib import Path as _P
        _write(cfg, _P(_replica_config_dir()) / "replica.toml")

        return JSONResponse({"ok": True, "developer_id": developer_id})

    # ── Watched GitHub repos ─────────────────────────────────────────────────

    @r.get("/repos")
    async def list_repos(request: Request):
        """List watched GitHub repos with heartbeat + counters."""
        _require_manager(request, cfg.manager_token)
        return JSONResponse([
            {
                "slug": r_.slug,
                "created_at": r_.created_at,
                "last_event_at": r_.last_event_at,
                "event_count": r_.event_count,
            }
            for r_ in cfg.watched_repos
        ])

    @r.post("/repos/add")
    async def add_repo(request: Request):
        """Register a GitHub repo for webhook ingestion.

        Returns the webhook URL + generated secret the manager pastes into
        GitHub Settings → Webhooks. Secret is NOT retrievable later; the manager
        either saves it in GitHub immediately or removes-and-readds the repo.
        """
        import secrets as _secrets
        _require_manager(request, cfg.manager_token)
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        slug = (data.get("slug") or "").strip()
        if not slug or "/" not in slug:
            raise HTTPException(400, "slug must be 'owner/repo'")
        # Reject obvious URL pastes by stripping github.com prefix if present.
        for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
            if slug.startswith(prefix):
                slug = slug.removeprefix(prefix).rstrip("/")
                break

        secret = (data.get("secret") or "").strip() or ("whsec_" + _secrets.token_urlsafe(24))

        from shadow_replica.config import WatchedRepo, add_watched_repo
        add_watched_repo(slug, secret)

        # Mutate live cfg so the webhook router accepts deliveries immediately.
        existing = next((r_ for r_ in cfg.watched_repos if r_.slug == slug), None)
        if existing is not None:
            existing.secret = secret
        else:
            cfg.watched_repos.append(WatchedRepo(
                slug=slug,
                secret=secret,
                created_at=datetime.now(timezone.utc).isoformat(),
            ))

        # Compose the webhook URL the manager will paste into GitHub.
        base = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
        webhook_url = f"{base}/v1/github/webhook"
        settings_url = f"https://github.com/{slug}/settings/hooks/new"

        return JSONResponse({
            "ok": True,
            "slug": slug,
            "webhook_url": webhook_url,
            "secret": secret,
            "settings_url": settings_url,
            "content_type": "application/json",
            "recommended_events": [
                "pull_request", "pull_request_review", "pull_request_review_comment",
                "push", "check_run", "check_suite", "issues",
            ],
        })

    @r.get("/threads")
    async def engineering_threads(request: Request, hours: int = 72, limit: int = 20):
        """Group recent artifacts by shared commit_sha / branch into Engineering Threads.

        Each thread bundles github_events, commit_reports, and pr_drafts that
        touch the same work so the manager sees one story per branch — "the PR,
        the AI reasoning behind it, and the CI result" as one unified card.
        """
        from datetime import datetime, timedelta, timezone
        _require_manager(request, cfg.manager_token)
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(hours=max(1, min(hours, 24 * 30)))
        rows = store.list_all(limit=5000)
        dev_name_map = {d.developer_id: d.name for d in cfg.trusted_devs}
        threads = _build_threads(rows, dev_name_map, period_start, max(1, min(limit, 100)))
        return JSONResponse({
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "threads": threads,
        })

    @r.delete("/repos/{owner}/{name}")
    async def remove_repo(request: Request, owner: str, name: str):
        """Stop accepting webhooks from this repo. GitHub-side webhook must be deleted separately."""
        _require_manager(request, cfg.manager_token)
        slug = f"{owner}/{name}"
        from shadow_replica.config import remove_watched_repo
        before = len(cfg.watched_repos)
        cfg.watched_repos[:] = [r_ for r_ in cfg.watched_repos if r_.slug != slug]
        if len(cfg.watched_repos) == before:
            raise HTTPException(404, "Repo not watched")
        remove_watched_repo(slug)
        return JSONResponse({"ok": True, "slug": slug})

    return r


def _build_threads(rows, dev_name_map: dict, period_start, limit: int) -> list[dict]:
    """Group artifacts by (branch, commit_sha). An artifact is included if it
    has one or both; those with neither are skipped. Threading rule:
        - commit_sha prefix (first 10 chars) OR branch is the join key
        - artifacts with only a branch join the thread that already has that branch
        - the thread's primary label is the latest github_event's title or the
          latest commit_report's message or the branch itself
    """
    from datetime import datetime, timezone
    import json as _json

    def _ts(row) -> datetime | None:
        s = getattr(row, "received_at", "") or ""
        if not s:
            return None
        try:
            s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    # (sha_key, branch) → list of items. We'll later merge on branch.
    threads: dict[str, dict] = {}
    sha_to_key: dict[str, str] = {}
    branch_to_key: dict[str, str] = {}

    def _thread_key(sha: str, branch: str) -> str:
        if sha:
            sha_short = sha[:10]
            if sha_short in sha_to_key:
                return sha_to_key[sha_short]
            if branch and branch in branch_to_key:
                sha_to_key[sha_short] = branch_to_key[branch]
                return branch_to_key[branch]
            key = f"sha:{sha_short}"
            sha_to_key[sha_short] = key
            if branch:
                branch_to_key[branch] = key
            return key
        if branch:
            if branch in branch_to_key:
                return branch_to_key[branch]
            key = f"br:{branch}"
            branch_to_key[branch] = key
            return key
        return ""

    for row in rows:
        ts = _ts(row)
        if ts is None or ts < period_start:
            continue
        atype = getattr(row, "artifact_type", "")
        if atype not in ("github_event", "commit_report", "pr_draft"):
            continue
        try:
            payload = _json.loads(getattr(row, "payload_json", "") or "{}")
        except Exception:
            continue

        branch = (payload.get("branch") or "").strip()
        sha = (payload.get("commit_sha") or payload.get("head_sha") or "").strip()
        key = _thread_key(sha, branch)
        if not key:
            continue

        t = threads.setdefault(key, {
            "thread_key": key,
            "branch": branch,
            "commit_sha": sha,
            "repos": set(),
            "contributors": set(),
            "artifact_types": set(),
            "items": [],
            "latest_at": None,
        })
        # Preserve the first non-empty values; let later entries fill blanks.
        if branch and not t["branch"]:
            t["branch"] = branch
        if sha and not t["commit_sha"]:
            t["commit_sha"] = sha
        repo = payload.get("repo") or ""
        if repo:
            t["repos"].add(repo)
        t["contributors"].add(getattr(row, "developer_id", ""))
        t["artifact_types"].add(atype)
        t["items"].append({
            "approval_id": getattr(row, "approval_id", ""),
            "developer_id": getattr(row, "developer_id", ""),
            "developer_name": dev_name_map.get(getattr(row, "developer_id", ""), getattr(row, "developer_id", "")),
            "artifact_type": atype,
            "channel": getattr(row, "channel", atype),
            "received_at": getattr(row, "received_at", ""),
            "summary": payload.get("summary") or payload.get("title") or payload.get("author_msg") or "",
            "url": payload.get("url", ""),
            "pr_number": payload.get("pr_number"),
            "followup_summary": payload.get("followup_summary", ""),
            "followup_status": payload.get("followup_status", ""),
        })
        if t["latest_at"] is None or ts > t["latest_at"]:
            t["latest_at"] = ts

    out = []
    for t in threads.values():
        if not t["items"]:
            continue
        t["items"].sort(key=lambda it: it["received_at"], reverse=True)
        label = _thread_label(t)
        out.append({
            "thread_key": t["thread_key"],
            "label": label,
            "branch": t["branch"],
            "commit_sha": t["commit_sha"],
            "repos": sorted(t["repos"]),
            "contributors": sorted(t["contributors"]),
            "artifact_types": sorted(t["artifact_types"]),
            "latest_at": t["latest_at"].isoformat() if t["latest_at"] else "",
            "items": t["items"],
        })
    out.sort(key=lambda th: th["latest_at"], reverse=True)
    return out[:limit]


def _thread_label(t: dict) -> str:
    # Prefer the newest github_event or commit_report summary; fall back to branch.
    best_github = ""
    best_commit = ""
    for it in sorted(t["items"], key=lambda i: i["received_at"], reverse=True):
        if it["artifact_type"] == "github_event" and it["summary"] and not best_github:
            best_github = it["summary"]
        elif it["artifact_type"] == "commit_report" and it["summary"] and not best_commit:
            best_commit = it["summary"]
    if best_github:
        return best_github[:140]
    if best_commit:
        return best_commit[:140]
    if t["branch"]:
        return f"branch · {t['branch']}"
    return t["thread_key"]


def _decode_pairing_code(code: str) -> dict | None:
    """Decode a base64url(json) pairing blob produced by `vantage-ghost pair`."""
    import base64
    import json as _json

    s = code.strip().replace("\n", "").replace(" ", "")
    # Accept both plain base64 and base64url, with or without padding.
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    try:
        raw = base64.b64decode(s, validate=False)
        obj = _json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    required = ("developer_id", "token")
    if not all(k in obj for k in required):
        return None
    return {
        "developer_id": str(obj.get("developer_id", "")),
        "name": str(obj.get("name", "")),
        "token": str(obj.get("token", "")),
        "subscriber_id": str(obj.get("subscriber_id", "")),
        "receive_url": str(obj.get("receive_url", "")),
        "github_login": str(obj.get("github_login", "")),
    }
