from __future__ import annotations

import logging

import json

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from shadow_replica.agent.subscriber_agent import SubscriberAgent
from shadow_replica.api.dashboard import make_router as make_dashboard_router
from shadow_replica.api.probe import make_router as make_probe_router
from shadow_replica.api.receive import make_router as make_receive_router
from shadow_replica.api.timeline import make_router as make_timeline_router
from shadow_replica.auth.token_verify import verify_request
from shadow_replica.config import ReplicaConfig
from shadow_replica.connections.ws_hub import WSHub
from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def build_app(cfg: ReplicaConfig) -> FastAPI:
    store = ReceivedStore(cfg.sqlite_path)
    hub = WSHub()
    agent = SubscriberAgent(store)

    app = FastAPI(title="Vantage Shadow Replica", docs_url="/docs", redoc_url=None)

    app.include_router(make_receive_router(store, cfg.trusted_devs))
    app.include_router(make_timeline_router(store, cfg.trusted_devs))
    app.include_router(make_probe_router(hub, store, cfg.trusted_devs))
    app.include_router(make_dashboard_router(store, cfg.trusted_devs))

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok", "trusted_devs": len(cfg.trusted_devs)})

    @app.get("/metrics")
    async def metrics():
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/agent/query")
    async def agent_query(request: Request):
        body = await request.body()
        verify_request(request, body, cfg.trusted_devs)
        try:
            data = json.loads(body)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        query = data.get("query", "")
        developer_id = data.get("developer_id")
        channel = data.get("channel")
        if not query:
            return JSONResponse({"error": "query required"}, status_code=400)
        result = agent.run(query, developer_id=developer_id, channel=channel)
        return JSONResponse(result)

    # ── Dev-daemon WebSocket endpoint ─────────────────────────────────────────

    @app.websocket("/v1/ws/{developer_id}")
    async def ws_endpoint(developer_id: str, websocket: WebSocket, machine_id: str = ""):
        auth = websocket.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            await websocket.close(code=4001)
            return

        token = auth.removeprefix("Bearer ").strip()
        matched = None
        for dev in cfg.trusted_devs:
            import hmac as _hmac
            if _hmac.compare_digest(dev.token_value, token) and dev.developer_id == developer_id:
                matched = dev
                break

        if matched is None:
            logger.warning("WS auth failed for developer_id=%s", developer_id)
            await websocket.close(code=4001)
            return

        effective_machine_id = machine_id or websocket.headers.get("X-Vantage-Developer-Id", developer_id)
        await hub.handle_connection(developer_id, effective_machine_id, websocket)

    return app
