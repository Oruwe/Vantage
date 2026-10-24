from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from shadow_replica.api.receive import make_router as make_receive_router
from shadow_replica.api.timeline import make_router as make_timeline_router
from shadow_replica.config import ReplicaConfig
from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def build_app(cfg: ReplicaConfig) -> FastAPI:
    store = ReceivedStore(cfg.sqlite_path)

    app = FastAPI(title="Vantage Shadow Replica", docs_url="/docs", redoc_url=None)

    app.include_router(make_receive_router(store, cfg.trusted_devs))
    app.include_router(make_timeline_router(store, cfg.trusted_devs))

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok", "trusted_devs": len(cfg.trusted_devs)})

    return app
