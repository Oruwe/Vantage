from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import time

from shadow_replica.auth.token_verify import verify_request
from shadow_replica.metrics import probe_latency, probe_requests
from shadow_replica.resilience.fallback import generate_fallback
from vantage.schemas import ProbeRequest

if TYPE_CHECKING:
    from shadow_replica.connections.ws_hub import WSHub
    from shadow_replica.storage.received_store import ReceivedStore

logger = logging.getLogger(__name__)


def make_router(hub: "WSHub", store: "ReceivedStore", trusted_devs: list, bus=None) -> APIRouter:
    # bus is accepted for symmetry; hub.probe() already publishes probe envelopes.
    r = APIRouter()

    @r.post("/v1/probe/{developer_id}")
    async def probe(developer_id: str, request: Request):
        raw = await request.body()
        dev = verify_request(request, raw, trusted_devs)

        if dev.developer_id != developer_id:
            raise HTTPException(status_code=403, detail="Token/developer_id mismatch")

        repo_hint = ""
        if raw:
            try:
                data = json.loads(raw)
                repo_hint = data.get("repo_hint", "") if isinstance(data, dict) else ""
            except Exception:
                pass

        probe_req = ProbeRequest(
            subscriber_id=dev.subscriber_id,
            repo_hint=repo_hint,
        )

        t0 = time.monotonic()
        response = await hub.probe(developer_id, probe_req)
        if response is not None:
            probe_latency.observe(time.monotonic() - t0)
            probe_requests.labels(developer_id=developer_id, decision=response.policy_decision.value).inc()
            if response.snapshot and response.snapshot.activity_signals:
                hub.cache_activity(
                    developer_id,
                    response.snapshot.activity_signals,
                    response.snapshot.captured_at,
                )
            return JSONResponse(response.model_dump(mode="json"))

        fallback = generate_fallback(store, developer_id, hub=hub)
        fallback.request_id = probe_req.request_id
        probe_requests.labels(developer_id=developer_id, decision="offline").inc()
        logger.info(
            "Probe for dev %s: offline fallback (staleness=%ds)",
            developer_id,
            fallback.staleness_seconds,
        )
        return JSONResponse(fallback.model_dump(mode="json"))

    return r
