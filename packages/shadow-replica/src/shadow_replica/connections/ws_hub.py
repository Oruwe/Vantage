from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import WebSocket

from shadow_replica.metrics import ws_connections_active
from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent
from vantage.schemas import (
    ActivitySignals, FrictionReport, IntentBeacon,
    PolicyDecision, ProbeRequest, ProbeResponse,
)

if TYPE_CHECKING:
    from shadow_replica.collision.detector import CollisionDetector
    from shadow_replica.wiretap.bus import WiretapBus

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 2.5
_DISCOVERY_TIMEOUT = 5.0

_REPLICA_REF = A2AAgentRef(kind="shadow-replica", label="Shadow Replica")


class WSHub:
    """Tracks live WebSocket connections from dev daemons.

    Key structures:
    - _connections: developer_id → {machine_id → WebSocket}
    - _pending: request_id → Future[ProbeResponse]   (waiters for probe responses)
    """

    def __init__(
        self,
        bus: "WiretapBus | None" = None,
        collision: "CollisionDetector | None" = None,
    ) -> None:
        self._connections: dict[str, dict[str, WebSocket]] = {}
        self._pending: dict[str, asyncio.Future[ProbeResponse]] = {}
        self._pending_disco: dict[str, asyncio.Future[dict]] = {}
        self._lock = asyncio.Lock()
        # In-memory cache: developer_id → (ActivitySignals, captured_at)
        self._activity: dict[str, tuple[ActivitySignals, datetime]] = {}
        # Rolling per-developer friction scores (last report wins)
        self._friction: dict[str, FrictionReport] = {}
        self._bus = bus
        self._collision = collision

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

        dev_ref = A2AAgentRef(kind="ghost-daemon", developer_id=developer_id, label=developer_id)
        self._emit(A2AEnvelope(
            intent=A2AIntent.SESSION_OPEN,
            from_agent=dev_ref,
            to_agent=_REPLICA_REF,
            summary=f"ghost {developer_id} connected (machine={machine_id})",
            payload={"machine_id": machine_id},
        ))

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
            self._emit(A2AEnvelope(
                intent=A2AIntent.SESSION_CLOSE,
                from_agent=dev_ref,
                to_agent=_REPLICA_REF,
                summary=f"ghost {developer_id} disconnected (machine={machine_id})",
                payload={"machine_id": machine_id},
                status="closed",
            ))

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        mtype = msg.get("type")
        data = msg.get("data", {}) if isinstance(msg.get("data"), dict) else {}

        if mtype == "probe_response":
            request_id = data.get("request_id", "")
            fut = self._pending.get(request_id)
            if fut and not fut.done():
                try:
                    fut.set_result(ProbeResponse(**data))
                except Exception as exc:
                    logger.warning("Bad probe_response payload: %s", exc)
        elif mtype == "discovery_response":
            request_id = data.get("request_id", "")
            fut = self._pending_disco.get(request_id)
            if fut and not fut.done():
                fut.set_result(data)
        elif mtype == "intent_beacon":
            await self._on_beacon(data)
        elif mtype == "friction_report":
            self._on_friction(data)
        elif mtype == "pong":
            pass

    # ── Probe dispatch ────────────────────────────────────────────────────────

    def is_online(self, developer_id: str) -> bool:
        return bool(self._connections.get(developer_id))

    def online_developer_ids(self) -> list[str]:
        return list(self._connections.keys())

    async def probe(
        self,
        developer_id: str,
        request: ProbeRequest,
        timeout: float = _PROBE_TIMEOUT,
    ) -> ProbeResponse | None:
        """Forward a probe request to the best live connection; return None on timeout/offline."""
        dev_conns = self._connections.get(developer_id, {})
        dev_ref = A2AAgentRef(kind="ghost-daemon", developer_id=developer_id, label=developer_id)
        req_env = A2AEnvelope(
            session_id=request.request_id,
            intent=A2AIntent.PROBE_REQUEST,
            from_agent=_REPLICA_REF,
            to_agent=dev_ref,
            summary=f"Probe → {developer_id}" + (f" (repo:{request.repo_hint})" if request.repo_hint else ""),
            payload={
                "request_id": request.request_id,
                "subscriber_id": request.subscriber_id,
                "repo_hint": request.repo_hint,
            },
        )
        self._emit(req_env)

        if not dev_conns:
            self._emit(A2AEnvelope(
                session_id=request.request_id,
                parent_turn_id=req_env.turn_id,
                intent=A2AIntent.PROBE_RESPONSE,
                from_agent=dev_ref,
                to_agent=_REPLICA_REF,
                summary=f"ghost {developer_id} offline — serving fallback",
                status="offline",
                payload={"reason": "no live connection"},
            ))
            return None

        ws = self._pick(dev_conns, request.repo_hint)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ProbeResponse] = loop.create_future()
        self._pending[request.request_id] = fut

        t0 = time.monotonic()
        try:
            await ws.send_text(json.dumps({
                "type": "probe_request",
                "data": request.model_dump(mode="json"),
            }))
            response = await asyncio.wait_for(fut, timeout=timeout)
            latency_ms = int((time.monotonic() - t0) * 1000)
            denied = response.policy_decision != PolicyDecision.ALLOWED
            resp_env = A2AEnvelope(
                session_id=request.request_id,
                parent_turn_id=req_env.turn_id,
                intent=A2AIntent.POLICY_CHALLENGE if denied else A2AIntent.PROBE_RESPONSE,
                from_agent=dev_ref,
                to_agent=_REPLICA_REF,
                latency_ms=latency_ms,
                status=(
                    "ok" if not denied else response.policy_decision.value
                ),
                summary=self._probe_summary(response),
                payload={
                    "policy_decision": response.policy_decision.value,
                    "redaction_level_applied": response.redaction_level_applied.value,
                    "turns": len(response.snapshot.last_turns) if response.snapshot else 0,
                    "activity": response.snapshot.activity_signals.model_dump(mode="json") if (response.snapshot and response.snapshot.activity_signals) else None,
                    "fallback_summary": response.fallback_summary,
                },
            )
            self._emit(resp_env)
            if denied and response.counter_offer:
                co = response.counter_offer
                self._emit(A2AEnvelope(
                    session_id=request.request_id,
                    parent_turn_id=resp_env.turn_id,
                    intent=A2AIntent.POLICY_COUNTER_OFFER,
                    from_agent=dev_ref,
                    to_agent=_REPLICA_REF,
                    status="offer",
                    summary=co.summary or "counter-offer",
                    payload={
                        "artifact_type": co.artifact_type,
                        "approval_id": co.approval_id,
                        "created_at": co.created_at.isoformat() if co.created_at else None,
                        "reason": co.reason,
                    },
                ))
            return response
        except asyncio.TimeoutError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("Probe timeout for dev %s (request_id=%s)", developer_id, request.request_id)
            self._emit(A2AEnvelope(
                session_id=request.request_id,
                parent_turn_id=req_env.turn_id,
                intent=A2AIntent.PROBE_RESPONSE,
                from_agent=dev_ref,
                to_agent=_REPLICA_REF,
                latency_ms=latency_ms,
                status="timeout",
                summary=f"ghost {developer_id} timed out after {latency_ms}ms",
            ))
            return None
        except Exception as exc:
            logger.warning("Probe failed for dev %s: %s", developer_id, exc)
            self._emit(A2AEnvelope(
                session_id=request.request_id,
                parent_turn_id=req_env.turn_id,
                intent=A2AIntent.PROBE_RESPONSE,
                from_agent=dev_ref,
                to_agent=_REPLICA_REF,
                status="error",
                summary=f"probe failed: {exc}",
            ))
            return None
        finally:
            self._pending.pop(request.request_id, None)

    @staticmethod
    def _probe_summary(response: ProbeResponse) -> str:
        if response.policy_decision != PolicyDecision.ALLOWED:
            return f"policy: {response.policy_decision.value}"
        if response.snapshot:
            sig = response.snapshot.activity_signals
            if sig:
                parts = []
                if sig.primary_activity and sig.primary_activity != "unknown":
                    parts.append(sig.primary_activity)
                if sig.active_repo:
                    parts.append(sig.active_repo)
                if sig.inferred_branch:
                    parts.append(sig.inferred_branch)
                if parts:
                    return "snapshot: " + " · ".join(parts)
            return f"snapshot: {len(response.snapshot.last_turns)} turns"
        return "snapshot received"

    def _emit(self, env: A2AEnvelope) -> None:
        if self._bus is not None:
            try:
                self._bus.publish(env)
            except Exception as exc:
                logger.debug("Wiretap publish failed: %s", exc)

    # ── Activity cache ────────────────────────────────────────────────────────

    def cache_activity(
        self,
        developer_id: str,
        signals: ActivitySignals,
        captured_at: datetime,
    ) -> None:
        self._activity[developer_id] = (signals, captured_at)

    def last_activity(self, developer_id: str) -> tuple[ActivitySignals, datetime] | None:
        return self._activity.get(developer_id)

    def _pick(self, connections: dict[str, WebSocket], repo_hint: str) -> WebSocket:
        """Pick the best connection for the given repo hint."""
        if repo_hint:
            hint = repo_hint.lower()
            for machine_id, ws in connections.items():
                if hint in machine_id.lower():
                    return ws
        return next(iter(connections.values()))

    # ── Converse (multi-turn broadcast) ───────────────────────────────────────

    async def converse(
        self,
        prompt: str,
        developer_ids: list[str] | None = None,
        timeout: float = _DISCOVERY_TIMEOUT,
    ) -> list[dict]:
        """Fan a discovery prompt to multiple ghosts in parallel; return their answers.

        Emits one INTENT_DISCOVERY envelope per target plus one DISCOVERY_RESPONSE
        envelope per answer (or timeout/error). The resulting answers are
        tagged with the same session_id so the Wiretap renders them as a thread.
        """
        if not developer_ids:
            developer_ids = self.online_developer_ids()
        session_id = f"converse-{int(time.monotonic() * 1000)}"

        async def ask(dev_id: str) -> dict:
            dev_conns = self._connections.get(dev_id, {})
            dev_ref = A2AAgentRef(kind="ghost-daemon", developer_id=dev_id, label=dev_id)
            req_env = A2AEnvelope(
                session_id=session_id,
                intent=A2AIntent.INTENT_DISCOVERY,
                from_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                to_agent=dev_ref,
                summary=f"ask → {dev_id}: {prompt[:80]}",
                payload={"prompt": prompt},
            )
            self._emit(req_env)
            if not dev_conns:
                self._emit(A2AEnvelope(
                    session_id=session_id,
                    parent_turn_id=req_env.turn_id,
                    intent=A2AIntent.DISCOVERY_RESPONSE,
                    from_agent=dev_ref,
                    to_agent=_REPLICA_REF,
                    status="offline",
                    summary=f"{dev_id} offline",
                ))
                return {"developer_id": dev_id, "status": "offline", "answer": ""}
            ws = self._pick(dev_conns, "")
            request_id = f"{session_id}:{dev_id}"
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[dict] = loop.create_future()
            self._pending_disco[request_id] = fut
            t0 = time.monotonic()
            try:
                await ws.send_text(json.dumps({
                    "type": "discovery_request",
                    "data": {"request_id": request_id, "prompt": prompt},
                }))
                data = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self._emit(A2AEnvelope(
                    session_id=session_id,
                    parent_turn_id=req_env.turn_id,
                    intent=A2AIntent.DISCOVERY_RESPONSE,
                    from_agent=dev_ref,
                    to_agent=_REPLICA_REF,
                    status="timeout",
                    summary=f"{dev_id} timed out",
                ))
                return {"developer_id": dev_id, "status": "timeout", "answer": ""}
            except Exception as exc:
                return {"developer_id": dev_id, "status": "error", "answer": str(exc)}
            finally:
                self._pending_disco.pop(request_id, None)

            latency_ms = int((time.monotonic() - t0) * 1000)
            policy = data.get("policy_decision", "ok")
            status = "ok" if policy == "allowed" else policy
            self._emit(A2AEnvelope(
                session_id=session_id,
                parent_turn_id=req_env.turn_id,
                intent=A2AIntent.DISCOVERY_RESPONSE,
                from_agent=dev_ref,
                to_agent=_REPLICA_REF,
                latency_ms=latency_ms,
                status=status,
                summary=str(data.get("answer", ""))[:140],
                payload=data,
            ))
            return {
                "developer_id": dev_id,
                "status": status,
                "answer": data.get("answer", ""),
                "signals": data.get("signals"),
                "latency_ms": latency_ms,
            }

        results = await asyncio.gather(*(ask(d) for d in developer_ids), return_exceptions=False)
        return list(results)

    # ── Beacon + friction handling ────────────────────────────────────────────

    async def _on_beacon(self, data: dict) -> None:
        try:
            beacon = IntentBeacon(**data)
        except Exception as exc:
            logger.debug("Bad beacon payload: %s", exc)
            return
        dev_ref = A2AAgentRef(kind="ghost-daemon", developer_id=beacon.developer_id, label=beacon.developer_id)
        self._emit(A2AEnvelope(
            intent=A2AIntent.INTENT_BEACON,
            from_agent=dev_ref,
            to_agent=_REPLICA_REF,
            summary=self._beacon_summary(beacon),
            payload=beacon.model_dump(mode="json"),
        ))
        if self._collision is None:
            return
        candidates = self._collision.ingest(beacon)
        for cand in candidates:
            summary = (
                f"collision! {cand.developer_a} ↔ {cand.developer_b} "
                f"on {cand.repo} ({len(cand.overlapping_files)} shared, jaccard={cand.jaccard})"
            )
            logger.warning(summary)
            self._emit(A2AEnvelope(
                intent=A2AIntent.COLLISION_ALERT,
                from_agent=_REPLICA_REF,
                to_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                status="offer",
                summary=summary,
                payload=cand.model_dump(mode="json"),
            ))
            await self._notify_ghosts_of_collision(cand)

    async def _notify_ghosts_of_collision(self, cand) -> None:
        """Push a collision_alert message to both involved ghosts (fire-and-forget)."""
        for dev_id, other_id in ((cand.developer_a, cand.developer_b), (cand.developer_b, cand.developer_a)):
            conns = self._connections.get(dev_id)
            if not conns:
                continue
            ws = next(iter(conns.values()))
            payload = {
                "other_developer_id": other_id,
                "repo": cand.repo,
                "overlapping_files": cand.overlapping_files,
                "jaccard": cand.jaccard,
            }
            try:
                await ws.send_text(json.dumps({"type": "collision_alert", "data": payload}))
            except Exception:
                pass

    def _on_friction(self, data: dict) -> None:
        try:
            report = FrictionReport(**data)
        except Exception as exc:
            logger.debug("Bad friction payload: %s", exc)
            return
        self._friction[report.developer_id] = report
        self._emit(A2AEnvelope(
            intent=A2AIntent.FRICTION_REPORT,
            from_agent=A2AAgentRef(kind="ghost-daemon", developer_id=report.developer_id, label=report.developer_id),
            to_agent=_REPLICA_REF,
            summary=f"friction={report.score:.2f} · {report.summary}",
            payload=report.model_dump(mode="json"),
        ))

    def friction_for(self, developer_id: str) -> FrictionReport | None:
        return self._friction.get(developer_id)

    def friction_all(self) -> dict[str, FrictionReport]:
        return dict(self._friction)

    @staticmethod
    def _beacon_summary(b: IntentBeacon) -> str:
        parts = [b.developer_id]
        if b.repo:
            parts.append(b.repo)
        if b.files_touched:
            parts.append(f"{len(b.files_touched)} files")
        return "beacon: " + " · ".join(parts)
