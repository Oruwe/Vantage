"""Semantic-collision detector.

Maintains a short sliding window of intent beacons from live ghosts. Each time
a new beacon arrives, it computes a Jaccard overlap between the arriving
beacon's `files_touched` and every other recent beacon in the same repo. If a
pair clears the configured threshold, the detector emits a `CollisionCandidate`
record so the hub can broadcast a COLLISION_ALERT envelope.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from vantage.schemas import CollisionCandidate, IntentBeacon

logger = logging.getLogger(__name__)

_BEACON_TTL = 300.0           # 5 minutes
_MIN_SHARED_FILES = 1         # require ≥1 shared file before computing jaccard
_DEFAULT_THRESHOLD = 0.4      # jaccard ≥ 0.4 = collision
_COOLDOWN = 180.0             # don't re-fire the same pair within 3 minutes


@dataclass
class _Entry:
    beacon: IntentBeacon
    arrived_at: float = field(default_factory=time.monotonic)


class CollisionDetector:
    def __init__(self, threshold: float = _DEFAULT_THRESHOLD) -> None:
        self._latest: dict[str, _Entry] = {}        # developer_id → latest beacon
        self._last_alert: dict[tuple[str, str], float] = {}
        self._threshold = threshold

    def ingest(self, beacon: IntentBeacon) -> list[CollisionCandidate]:
        """Store the beacon and return any newly-detected collisions.

        Returns an empty list when no collisions clear the threshold or when the
        same pair was already alerted inside the cooldown window.
        """
        self._prune()
        candidates: list[CollisionCandidate] = []
        now = time.monotonic()
        incoming = _Entry(beacon=beacon, arrived_at=now)
        self._latest[beacon.developer_id] = incoming

        if not beacon.files_touched:
            return candidates

        incoming_files = set(beacon.files_touched)
        for other_dev, entry in self._latest.items():
            if other_dev == beacon.developer_id:
                continue
            other = entry.beacon
            if not other.files_touched:
                continue
            if beacon.repo and other.repo and beacon.repo != other.repo:
                continue
            other_files = set(other.files_touched)
            shared = incoming_files & other_files
            if len(shared) < _MIN_SHARED_FILES:
                continue
            union = incoming_files | other_files
            jaccard = len(shared) / len(union) if union else 0.0
            if jaccard < self._threshold:
                continue
            pair = tuple(sorted((beacon.developer_id, other_dev)))
            last = self._last_alert.get(pair, 0.0)
            if now - last < _COOLDOWN:
                continue
            self._last_alert[pair] = now
            candidates.append(CollisionCandidate(
                repo=beacon.repo or other.repo,
                developer_a=pair[0],
                developer_b=pair[1],
                overlapping_files=sorted(shared),
                intent_a=(beacon.intent_hint if pair[0] == beacon.developer_id else other.intent_hint),
                intent_b=(other.intent_hint if pair[0] == beacon.developer_id else beacon.intent_hint),
                jaccard=round(jaccard, 3),
            ))
        return candidates

    def snapshot(self) -> list[IntentBeacon]:
        self._prune()
        return [e.beacon for e in self._latest.values()]

    def _prune(self) -> None:
        now = time.monotonic()
        stale = [k for k, e in self._latest.items() if now - e.arrived_at > _BEACON_TTL]
        for k in stale:
            self._latest.pop(k, None)
