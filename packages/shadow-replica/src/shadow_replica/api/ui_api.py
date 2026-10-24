from __future__ import annotations

import hmac as _hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from shadow_replica.agent.subscriber_agent import SubscriberAgent
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
    except Exception:
        pass
    return channel


def make_ui_router(
    store: "ReceivedStore",
    hub: "WSHub",
    cfg: "ReplicaConfig",
    agent: "SubscriberAgent | None" = None,
) -> APIRouter:
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

        if hub.is_online(developer_id):
            t0 = time.monotonic()
            response = await hub.probe(developer_id, probe_req)
            if response is not None:
                # Cache activity signals for offline fallback
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
        result = await _agent.run_async(q, developer_id=developer_id, channel=channel)
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

    return r
