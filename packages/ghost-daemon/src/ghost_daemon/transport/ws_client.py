from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from vantage.schemas import ProbeRequest

if TYPE_CHECKING:
    from vantage.schemas import SubscriberIdentity
    from ghost_daemon.agents.discovery_agent import DiscoveryAgent
    from ghost_daemon.agents.probe_agent import ProbeAgent
    from ghost_daemon.config import Config
    from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)

_PING_INTERVAL = 30.0  # seconds
_BEACON_INTERVAL = 45.0  # seconds — collision-detection cadence
_FRICTION_INTERVAL = 600.0  # 10 minutes


class WSClient:
    """Maintains a single outbound WebSocket connection to one subscriber's replica.

    Opened only when Channel.LIVE_PROBE is enabled for that subscriber.
    Reconnects automatically with exponential backoff.

    While connected, periodically emits an `intent_beacon` message (for
    collision detection) and a `friction_report` message (for the Friction
    Radar). Both are policy-aware — if LIVE_PROBE is not enabled, beacons stay
    content-free (no `intent_hint`).
    """

    def __init__(
        self,
        subscriber: "SubscriberIdentity",
        developer_id: str,
        machine_id: str,
        probe_agent: "ProbeAgent",
        discovery_agent: "DiscoveryAgent | None" = None,
        tailer: "SessionTailer | None" = None,
        cfg: "Config | None" = None,
        on_connect=None,
        on_disconnect=None,
    ) -> None:
        self._subscriber = subscriber
        self._developer_id = developer_id
        self._machine_id = machine_id
        self._probe_agent = probe_agent
        self._discovery_agent = discovery_agent
        self._tailer = tailer
        self._cfg = cfg
        self._running = False
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

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
                if self._on_disconnect is not None:
                    try:
                        self._on_disconnect(error=str(exc))
                    except TypeError:
                        # Caller supplied a zero-arg callback
                        try:
                            self._on_disconnect()
                        except Exception:
                            pass
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
            if self._on_connect is not None:
                try:
                    self._on_connect()
                except Exception as exc:
                    logger.debug("on_connect callback raised: %s", exc)
            ping_task = asyncio.create_task(self._ping_loop(ws))
            beacon_task = asyncio.create_task(self._beacon_loop(ws))
            friction_task = asyncio.create_task(self._friction_loop(ws))
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    await self._handle_message(ws, raw)
            finally:
                for t in (ping_task, beacon_task, friction_task):
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                if self._on_disconnect is not None:
                    try:
                        self._on_disconnect()
                    except Exception as exc:
                        logger.debug("on_disconnect callback raised: %s", exc)

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
        elif msg_type == "discovery_request" and self._discovery_agent is not None:
            data = msg.get("data", {})
            request_id = data.get("request_id", "")
            prompt = data.get("prompt", "")
            try:
                answer = await self._discovery_agent.answer(
                    prompt, self._subscriber.subscriber_id,
                )
                answer["request_id"] = request_id
                await ws.send(json.dumps({"type": "discovery_response", "data": answer}))
            except Exception as exc:
                logger.warning("Discovery handling failed: %s", exc)
                await ws.send(json.dumps({
                    "type": "discovery_response",
                    "data": {
                        "request_id": request_id,
                        "developer_id": self._developer_id,
                        "policy_decision": "offline",
                        "answer": f"error: {exc}",
                    },
                }))
        elif msg_type == "collision_alert":
            # Replica is telling us two ghosts overlap. For v1 we just log it;
            # a future upgrade can surface this as a toast in the ghost UI.
            data = msg.get("data", {})
            logger.warning(
                "Collision alert: dev %s overlaps on %s (files=%s)",
                data.get("other_developer_id"),
                data.get("repo"),
                data.get("overlapping_files"),
            )

    async def _beacon_loop(self, ws) -> None:
        """Fire periodic intent_beacon messages for collision detection."""
        if self._tailer is None or self._cfg is None:
            return
        from ghost_daemon.agents.activity_extractor import (
            build_beacon, extract_activity_signals,
        )
        while True:
            try:
                await asyncio.sleep(_BEACON_INTERVAL)
                turns = await self._quick_tail(limit=10)
                if not turns:
                    continue
                signals = extract_activity_signals(turns, cwd=str(Path.cwd()))
                # Content-free when policy would deny: strip intent_hint
                from ghost_daemon.approval.policy import load_policy
                from vantage.schemas import Channel
                policy = load_policy(self._subscriber.subscriber_id, self._cfg.vantage_dir)
                beacon = build_beacon(signals, self._developer_id, turn_count=len(turns))
                if Channel.LIVE_PROBE not in policy.channels:
                    beacon.intent_hint = ""
                await ws.send(json.dumps({
                    "type": "intent_beacon",
                    "data": beacon.model_dump(mode="json"),
                }))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Beacon loop blip: %s", exc)

    async def _friction_loop(self, ws) -> None:
        """Fire periodic friction_report messages."""
        if self._tailer is None or self._cfg is None:
            return
        from ghost_daemon.agents.activity_extractor import compute_friction
        while True:
            try:
                await asyncio.sleep(_FRICTION_INTERVAL)
                turns = await self._quick_tail(limit=30)
                report = compute_friction(
                    turns, self._developer_id,
                    minutes_since_commit=self._minutes_since_commit(),
                )
                await ws.send(json.dumps({
                    "type": "friction_report",
                    "data": report.model_dump(mode="json"),
                }))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Friction loop blip: %s", exc)

    async def _quick_tail(self, limit: int = 10) -> list:
        """Grab the N newest turns across the configured session dirs."""
        if self._cfg is None or self._tailer is None:
            return []
        raw = []
        for session_dir in self._cfg.resolved_session_dirs:
            jsonl_files = sorted(
                session_dir.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for jsonl in jsonl_files[:3]:
                try:
                    raw.extend(await self._tailer.tail_recent(jsonl, n=limit))
                    if len(raw) >= limit:
                        break
                except Exception:
                    continue
            if len(raw) >= limit:
                break
        return raw[:limit]

    @staticmethod
    def _minutes_since_commit() -> int:
        """Best-effort: 0 if we can't determine. Avoids subprocess cost by default."""
        return 0
