from __future__ import annotations

import logging

import json
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from shadow_replica.agent.subscriber_agent import SubscriberAgent
from shadow_replica.api.dashboard import make_router as make_dashboard_router
from shadow_replica.api.probe import make_router as make_probe_router
from shadow_replica.api.receive import make_router as make_receive_router
from shadow_replica.api.timeline import make_router as make_timeline_router
from shadow_replica.api.ui_api import make_ui_router
from shadow_replica.auth.token_verify import verify_request
from shadow_replica.config import ReplicaConfig
from shadow_replica.connections.ws_hub import WSHub
from shadow_replica.models.embedder import Embedder
from shadow_replica.storage.received_store import ReceivedStore

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


class _LLMWrapper:
    """Thin async LLM wrapper exposing a uniform complete() interface."""

    def __init__(self, client, model: str, is_anthropic: bool = False) -> None:
        self._client = client
        self._model = model
        self._is_anthropic = is_anthropic

    async def complete(self, messages: list[dict], max_tokens: int = 500) -> str:
        if self._is_anthropic:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=max_tokens, messages=messages
            )
            return resp.content[0].text
        resp = await self._client.chat.completions.create(
            model=self._model, messages=messages, max_tokens=max_tokens
        )
        return resp.choices[0].message.content or ""


def _build_llm(cfg: "ReplicaConfig") -> "_LLMWrapper | None":
    """Build a lightweight LLM wrapper for the subscriber agent, or None."""
    try:
        import os
        from openai import AsyncOpenAI

        provider = cfg.llm.provider.lower()
        model = cfg.llm.model or "gpt-4o-mini"
        api_key = os.environ.get(cfg.llm.api_key_env, "") if cfg.llm.api_key_env else ""

        if provider == "ollama":
            client = AsyncOpenAI(base_url=f"{cfg.llm.endpoint}/v1", api_key="ollama")
            return _LLMWrapper(client, model)
        if provider in ("openrouter", "openai_compat", "groq", "grok"):
            endpoint = cfg.llm.endpoint or {
                "groq": "https://api.groq.com/openai/v1",
                "grok": "https://api.x.ai/v1",
                "openrouter": "https://openrouter.ai/api/v1",
            }.get(provider, cfg.llm.endpoint)
            client = AsyncOpenAI(base_url=endpoint, api_key=api_key or "x")
            return _LLMWrapper(client, model)
        if provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            return _LLMWrapper(client, model or "claude-haiku-4-5-20251001", is_anthropic=True)
        client = AsyncOpenAI(api_key=api_key)
        return _LLMWrapper(client, model)
    except Exception as exc:
        logger.warning("Could not build replica LLM client: %s", exc)
        return None


def build_app(cfg: ReplicaConfig) -> FastAPI:
    embedder = Embedder(
        provider=cfg.embeddings.provider,
        model=cfg.embeddings.model,
        endpoint=cfg.embeddings.endpoint,
    ) if cfg.qdrant.enabled else None

    store = ReceivedStore(
        qdrant_url=cfg.qdrant.url,
        embedder=embedder,
    )
    hub = WSHub()

    llm = _build_llm(cfg) if cfg.llm.provider.strip() else None
    agent = SubscriberAgent(store, llm=llm, embedder=embedder)

    app = FastAPI(title="Vantage Shadow Replica", docs_url="/docs", redoc_url=None)

    app.include_router(make_receive_router(store, cfg.trusted_devs))
    app.include_router(make_timeline_router(store, cfg.trusted_devs))
    app.include_router(make_probe_router(hub, store, cfg.trusted_devs))
    app.include_router(make_dashboard_router(store, cfg.trusted_devs))
    app.include_router(make_ui_router(store, hub, cfg, agent=agent))

    @app.get("/", include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(_STATIC_DIR / "index.html")

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
        result = await agent.run_async(query, developer_id=developer_id, channel=channel)
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
