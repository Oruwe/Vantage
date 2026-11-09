"""Collision heads-up poller — pulls dev-facing collisions from each manager.

Every subscriber the dev pushes to has a shadow-replica running M12's semantic
collision detector. This module calls each one's ``GET /v1/collisions/for-me``
endpoint (Bearer-authed with the same subscriber token used for pushes) and
aggregates the results into a short-lived in-memory cache.

The ghost UI polls ``/api/collisions`` which reads this cache — so the browser
doesn't make N cross-origin requests and we throttle network traffic to one
batch every ``refresh_seconds``.

Nothing about this surface pushes the dev's data upstream; it's purely a pull
of "does anyone else on the team show up in my file space right now?"
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ghost_daemon.subscribers.registry import SubscriberRegistry

logger = logging.getLogger(__name__)


@dataclass
class CollisionRecord:
    """One collision pair, normalized for the ghost UI."""
    collision_id: str
    other_developer: str
    other_developer_name: str
    files: list[str] = field(default_factory=list)
    kind: str = "semantic"       # "semantic" or "file_overlap"
    score: float = 0.0
    detected_at: str = ""
    source_subscriber: str = ""  # which manager's replica reported this


@dataclass
class CollisionBundle:
    """Snapshot handed to the UI."""
    fetched_at: float = 0.0
    collisions: list[CollisionRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # per-subscriber failures

    @property
    def count(self) -> int:
        return len(self.collisions)


class CollisionPoller:
    """Short-lived cache + on-demand fetch across all active subscribers.

    The refresh interval is enforced on reads: if the last fetch is fresh,
    serve the cache; otherwise refresh. This keeps the cost at ~one burst
    per ``refresh_seconds``, regardless of how often the UI polls.
    """

    def __init__(
        self,
        *,
        subscribers: "SubscriberRegistry",
        developer_id: str,
        refresh_seconds: float = 90.0,
        http_timeout: float = 5.0,
        lookback_hours: int = 48,
    ) -> None:
        self._subscribers = subscribers
        self._developer_id = developer_id
        self._refresh_seconds = refresh_seconds
        self._http_timeout = http_timeout
        self._lookback_hours = lookback_hours
        self._lock = asyncio.Lock()
        self._cache = CollisionBundle()

    async def get(self, *, force: bool = False) -> CollisionBundle:
        """Return a CollisionBundle, refreshing from the network only when stale."""
        now = time.time()
        if not force and (now - self._cache.fetched_at) < self._refresh_seconds:
            return self._cache
        # Lock across refresh so concurrent /api/collisions callers share work.
        async with self._lock:
            if not force and (time.time() - self._cache.fetched_at) < self._refresh_seconds:
                return self._cache
            self._cache = await self._fetch_all()
            return self._cache

    async def _fetch_all(self) -> CollisionBundle:
        """Call every active subscriber's /v1/collisions/for-me in parallel."""
        rows = self._subscribers.list_active()
        if not rows:
            return CollisionBundle(fetched_at=time.time())

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            results = await asyncio.gather(
                *(self._fetch_one(client, row) for row in rows),
                return_exceptions=True,
            )

        bundle = CollisionBundle(fetched_at=time.time())
        seen_ids: set[str] = set()
        for row, res in zip(rows, results):
            if isinstance(res, Exception):
                bundle.errors.append(f"{row.name}: {res}")
                continue
            for c in res:
                if c.collision_id in seen_ids:
                    continue
                seen_ids.add(c.collision_id)
                bundle.collisions.append(c)
        # Newest first
        bundle.collisions.sort(key=lambda c: c.detected_at, reverse=True)
        return bundle

    async def _fetch_one(self, client: httpx.AsyncClient, row) -> list[CollisionRecord]:
        identity = self._subscribers.to_identity(row)
        base_url = row.receive_url.rstrip("/")
        url = f"{base_url}/v1/collisions/for-me"
        headers = {"Authorization": f"Bearer {identity.token_value}"}
        params = {"hours": self._lookback_hours}
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        return [
            self._normalize(c, source_subscriber=row.name)
            for c in data.get("collisions", [])
        ]

    def _normalize(self, raw: dict, *, source_subscriber: str) -> CollisionRecord:
        """Flatten a SemanticCollision dict into the dev-facing shape.

        Picks the "other" developer relative to self._developer_id so the UI
        always labels the collision by who the dev is colliding WITH.
        """
        a = raw.get("developer_a") or ""
        b = raw.get("developer_b") or ""
        if a == self._developer_id:
            other, other_name = b, raw.get("developer_b_name") or b
        else:
            other, other_name = a, raw.get("developer_a_name") or a
        return CollisionRecord(
            collision_id=str(raw.get("collision_id") or ""),
            other_developer=other,
            other_developer_name=other_name,
            files=list(raw.get("files_overlap") or raw.get("shared_files") or [])[:6],
            kind=str(raw.get("kind") or "semantic"),
            score=float(raw.get("cosine") or raw.get("score") or 0.0),
            detected_at=str(raw.get("detected_at") or raw.get("created_at") or ""),
            source_subscriber=source_subscriber,
        )
