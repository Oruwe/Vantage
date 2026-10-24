from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from vantage.schemas import ProbeRequest

if TYPE_CHECKING:
    from vantage.schemas import SubscriberIdentity
    from ghost_daemon.agents.probe_agent import ProbeAgent

logger = logging.getLogger(__name__)

_PING_INTERVAL = 30.0  # seconds


class WSClient:
    """Maintains a single outbound WebSocket connection to one subscriber's replica.

    Opened only when Channel.LIVE_PROBE is enabled for that subscriber.
    Reconnects automatically with exponential backoff.
    """

    def __init__(
        self,
        subscriber: "SubscriberIdentity",
        developer_id: str,
        machine_id: str,
        probe_agent: "ProbeAgent",
    ) -> None:
        self._subscriber = subscriber
        self._developer_id = developer_id
        self._machine_id = machine_id
        self._probe_agent = probe_agent
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        backoff = 2.0
        base_url = self._subscriber.receive_url.rstrip("/")
        ws_url = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        ws_url += f"/v1/ws/{self._developer_id}?machine_id={self._machine_id}"

        while self._running:
            try:
                await self._connect(ws_url)
                backoff = 2.0
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "WS connection to %s (%s) failed: %s — retry in %.1fs",
                    self._subscriber.name,
                    self._subscriber.subscriber_id,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect(self, ws_url: str) -> None:
        import websockets

        headers = {
            "Authorization": f"Bearer {self._subscriber.token_value}",
            "X-Vantage-Developer-Id": self._developer_id,
            "X-Vantage-Subscriber-Id": self._subscriber.subscriber_id,
        }

        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            logger.info(
                "WS connected to %s (%s)",
                self._subscriber.name,
                self._subscriber.subscriber_id,
            )
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    await self._handle_message(ws, raw)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                break

    async def _handle_message(self, ws, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        msg_type = msg.get("type")
        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif msg_type == "probe_request":
            data = msg.get("data", {})
            try:
                request = ProbeRequest(**data)
                response = await self._probe_agent.run(request)
                await ws.send(json.dumps({
                    "type": "probe_response",
                    "data": response.model_dump(mode="json"),
                }))
            except Exception as exc:
                logger.warning("Probe handling failed: %s", exc)
