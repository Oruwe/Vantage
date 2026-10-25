from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import AsyncIterator

from vantage.a2a import A2AEnvelope

logger = logging.getLogger(__name__)

_RING_CAP = 500
_QUEUE_CAP = 256


class WiretapBus:
    """In-process pub/sub for A2A envelopes.

    - `publish(env)` is sync-safe for the hot path (receive handler, probe handler).
    - `subscribe()` yields envelopes to each live SSE connection with its own
      bounded queue; slow consumers drop messages rather than blocking the bus.
    - `recent(limit)` returns the latest N envelopes so the UI can scroll back
      on first paint.
    """

    def __init__(self, ring_cap: int = _RING_CAP) -> None:
        self._ring: deque[A2AEnvelope] = deque(maxlen=ring_cap)
        self._subscribers: set[asyncio.Queue[A2AEnvelope]] = set()
        self._lock = asyncio.Lock()

    def publish(self, env: A2AEnvelope) -> None:
        self._ring.append(env)
        for q in list(self._subscribers):
            try:
                q.put_nowait(env)
            except asyncio.QueueFull:
                # drop oldest to make room for newest — the UI prefers freshness
                try:
                    _ = q.get_nowait()
                    q.put_nowait(env)
                except Exception:
                    pass

    def recent(self, limit: int = 100) -> list[A2AEnvelope]:
        if limit <= 0:
            return []
        return list(self._ring)[-limit:]

    async def subscribe(self) -> AsyncIterator[A2AEnvelope]:
        q: asyncio.Queue[A2AEnvelope] = asyncio.Queue(maxsize=_QUEUE_CAP)
        async with self._lock:
            self._subscribers.add(q)
        try:
            while True:
                env = await q.get()
                yield env
        finally:
            async with self._lock:
                self._subscribers.discard(q)
