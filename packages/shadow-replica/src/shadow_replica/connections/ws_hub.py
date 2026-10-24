from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from shadow_replica.metrics import ws_connections_active
from vantage.schemas import PolicyDecision, ProbeRequest, ProbeResponse, RedactionLevel

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 2.5


class WSHub:
    """Tracks live WebSocket connections from dev daemons.

    Key structures:
    - _connections: developer_id → {machine_id → WebSocket}
    - _pending: request_id → Future[ProbeResponse]   (waiters for probe responses)
    """

    def __init__(self) -> None:
        self._connections: dict[str, dict[str, WebSocket]] = {}
        self._pending: dict[str, asyncio.Future[ProbeResponse]] = {}
        self._lock = asyncio.Lock()

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def handle_connection(
        self,
        developer_id: str,
        machine_id: str,
        ws: WebSocket,
    ) -> None:
        """Accept and serve a dev daemon connection until it closes."""
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(developer_id, {})[machine_id] = ws
        ws_connections_active.inc()
        logger.info("Dev %s connected (machine=%s)", developer_id, machine_id)

        try:
            while True:
                data = await ws.receive_text()
                await self._dispatch(data)
        except Exception as exc:
            logger.debug("WS disconnected for %s/%s: %s", developer_id, machine_id, exc)
        finally:
            async with self._lock:
                dev_conns = self._connections.get(developer_id, {})
                dev_conns.pop(machine_id, None)
                if not dev_conns:
                    self._connections.pop(developer_id, None)
            ws_connections_active.dec()
            logger.info("Dev %s disconnected (machine=%s)", developer_id, machine_id)

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        if msg.get("type") == "probe_response":
            data = msg.get("data", {})
            request_id = data.get("request_id", "")
            fut = self._pending.get(request_id)
            if fut and not fut.done():
                try:
                    fut.set_result(ProbeResponse(**data))
                except Exception as exc:
                    logger.warning("Bad probe_response payload: %s", exc)
        elif msg.get("type") == "pong":
            pass

    # ── Probe dispatch ────────────────────────────────────────────────────────

    def is_online(self, developer_id: str) -> bool:
        return bool(self._connections.get(developer_id))

    async def probe(
        self,
        developer_id: str,
        request: ProbeRequest,
        timeout: float = _PROBE_TIMEOUT,
    ) -> ProbeResponse | None:
        """Forward a probe request to the best live connection; return None on timeout/offline."""
        dev_conns = self._connections.get(developer_id, {})
        if not dev_conns:
            return None

        ws = self._pick(dev_conns, request.repo_hint)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ProbeResponse] = loop.create_future()
        self._pending[request.request_id] = fut

        try:
            await ws.send_text(json.dumps({
                "type": "probe_request",
                "data": request.model_dump(mode="json"),
            }))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Probe timeout for dev %s (request_id=%s)", developer_id, request.request_id)
            return None
        except Exception as exc:
            logger.warning("Probe failed for dev %s: %s", developer_id, exc)
            return None
        finally:
            self._pending.pop(request.request_id, None)

    def _pick(self, connections: dict[str, WebSocket], repo_hint: str) -> WebSocket:
        """Pick the best connection for the given repo hint."""
        if repo_hint:
            hint = repo_hint.lower()
            for machine_id, ws in connections.items():
                if hint in machine_id.lower():
                    return ws
        return next(iter(connections.values()))
