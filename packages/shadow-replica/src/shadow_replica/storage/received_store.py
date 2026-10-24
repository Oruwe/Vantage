from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from shadow_replica.models.embedder import Embedder

logger = logging.getLogger(__name__)

_COLLECTION = "vantage_received"
_VECTOR_SIZE = 768


@dataclass
class ReceivedArtifactRow:
    approval_id: str
    developer_id: str
    artifact_type: str
    draft_id: str
    channel: str
    payload_json: str
    approved_at: str
    received_at: str


class ReceivedStore:
    """Qdrant-backed store for received artifacts.

    Primary storage is a Qdrant collection.  Falls back to an in-memory list so
    the service stays running when Qdrant is unavailable — the manager sees
    whatever arrived before the outage.

    Semantic search is available via `semantic_search()` when Qdrant is up.
    """

    def __init__(
        self,
        qdrant_url: str = "http://localhost:6333",
        embedder: "Embedder | None" = None,
    ) -> None:
        self._url = qdrant_url
        self._embedder = embedder
        self._client: object | None = None
        self._qdrant_ok = False
        self._fallback: list[ReceivedArtifactRow] = []
        self._ensure_client()

    # ── Connection ─────────────────────────────────────────────────────────────

    def _ensure_client(self) -> bool:
        if self._qdrant_ok and self._client is not None:
            return True
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            client = QdrantClient(url=self._url, timeout=5)
            existing = {c.name for c in client.get_collections().collections}
            if _COLLECTION not in existing:
                client.create_collection(
                    collection_name=_COLLECTION,
                    vectors_config=VectorParams(size=_VECTOR_SIZE, distance=Distance.COSINE),
                )
            self._client = client
            self._qdrant_ok = True
            return True
        except Exception as exc:
            logger.warning("Qdrant unavailable (%s) — using in-memory fallback", exc)
            self._qdrant_ok = False
            return False

    # ── Write ──────────────────────────────────────────────────────────────────

    async def save_async(
        self,
        approval_id: str,
        developer_id: str,
        artifact_type: str,
        draft_id: str,
        channel: str,
        payload_json: str,
        approved_at: str,
    ) -> None:
        """Persist a received artifact (async — embeds and upserts to Qdrant)."""
        received_at = datetime.utcnow().isoformat()
        row = ReceivedArtifactRow(
            approval_id=approval_id,
            developer_id=developer_id,
            artifact_type=artifact_type,
            draft_id=draft_id,
            channel=channel,
            payload_json=payload_json,
            approved_at=approved_at,
            received_at=received_at,
        )

        if not self._ensure_client():
            if not any(r.approval_id == approval_id for r in self._fallback):
                self._fallback.append(row)
            return

        try:
            vector = await self._embed_row(row)
            self._upsert_point(row, vector)
        except Exception as exc:
            logger.warning("Qdrant save failed (%s) — buffering in memory", exc)
            if not any(r.approval_id == approval_id for r in self._fallback):
                self._fallback.append(row)

    def save(
        self,
        approval_id: str,
        developer_id: str,
        artifact_type: str,
        draft_id: str,
        channel: str,
        payload_json: str,
        approved_at: str,
    ) -> None:
        """Sync save — stores with zero-vector if embedder unavailable. Use save_async when possible."""
        received_at = datetime.utcnow().isoformat()
        row = ReceivedArtifactRow(
            approval_id=approval_id,
            developer_id=developer_id,
            artifact_type=artifact_type,
            draft_id=draft_id,
            channel=channel,
            payload_json=payload_json,
            approved_at=approved_at,
            received_at=received_at,
        )
        if not self._ensure_client():
            if not any(r.approval_id == approval_id for r in self._fallback):
                self._fallback.append(row)
            return
        try:
            self._upsert_point(row, [0.0] * _VECTOR_SIZE)
        except Exception as exc:
            logger.warning("Qdrant sync save failed (%s)", exc)
            if not any(r.approval_id == approval_id for r in self._fallback):
                self._fallback.append(row)

    def _upsert_point(self, row: ReceivedArtifactRow, vector: list[float]) -> None:
        from qdrant_client.models import PointStruct

        point_id = _to_uuid(row.approval_id)
        payload = {
            "approval_id": row.approval_id,
            "developer_id": row.developer_id,
            "artifact_type": row.artifact_type,
            "draft_id": row.draft_id,
            "channel": row.channel,
            "payload_json": row.payload_json,
            "approved_at": row.approved_at,
            "received_at": row.received_at,
        }
        self._client.upsert(  # type: ignore[union-attr]
            collection_name=_COLLECTION,
            points=[PointStruct(id=point_id, vector=_fit(vector), payload=payload)],
        )

    async def _embed_row(self, row: ReceivedArtifactRow) -> list[float]:
        if self._embedder is None:
            return [0.0] * _VECTOR_SIZE
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            payload = {}
        text = _artifact_text(row.artifact_type, payload)
        return await self._embedder.embed(text)

    # ── Read ───────────────────────────────────────────────────────────────────

    def list_for_developer(
        self,
        developer_id: str,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[ReceivedArtifactRow]:
        if not self._ensure_client():
            rows = [r for r in self._fallback if r.developer_id == developer_id]
            if channel:
                rows = [r for r in rows if r.channel == channel]
            return sorted(rows, key=lambda r: r.received_at, reverse=True)[:limit]

        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            must = [FieldCondition(key="developer_id", match=MatchValue(value=developer_id))]
            if channel:
                must.append(FieldCondition(key="channel", match=MatchValue(value=channel)))

            results, _ = self._client.scroll(  # type: ignore[union-attr]
                collection_name=_COLLECTION,
                scroll_filter=Filter(must=must),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            rows = [_point_to_row(r) for r in results]
            return sorted(rows, key=lambda r: r.received_at, reverse=True)
        except Exception as exc:
            logger.warning("Qdrant list_for_developer failed: %s", exc)
            return []

    def list_all(
        self,
        channel: str | None = None,
        limit: int = 100,
    ) -> list[ReceivedArtifactRow]:
        if not self._ensure_client():
            rows = list(self._fallback)
            if channel:
                rows = [r for r in rows if r.channel == channel]
            return sorted(rows, key=lambda r: r.received_at, reverse=True)[:limit]

        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            scroll_filter = None
            if channel:
                scroll_filter = Filter(
                    must=[FieldCondition(key="channel", match=MatchValue(value=channel))]
                )

            results, _ = self._client.scroll(  # type: ignore[union-attr]
                collection_name=_COLLECTION,
                scroll_filter=scroll_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            rows = [_point_to_row(r) for r in results]
            return sorted(rows, key=lambda r: r.received_at, reverse=True)
        except Exception as exc:
            logger.warning("Qdrant list_all failed: %s", exc)
            return []

    def list_developers(self) -> list[str]:
        if not self._ensure_client():
            return sorted({r.developer_id for r in self._fallback})

        try:
            # Scroll all points and collect unique developer_ids
            seen: set[str] = set()
            offset = None
            while True:
                results, next_offset = self._client.scroll(  # type: ignore[union-attr]
                    collection_name=_COLLECTION,
                    limit=1000,
                    offset=offset,
                    with_payload=["developer_id"],
                    with_vectors=False,
                )
                for r in results:
                    dev_id = (r.payload or {}).get("developer_id", "")
                    if dev_id:
                        seen.add(dev_id)
                if next_offset is None or not results:
                    break
                offset = next_offset
            return sorted(seen)
        except Exception as exc:
            logger.warning("Qdrant list_developers failed: %s", exc)
            return []

    # ── Semantic search ────────────────────────────────────────────────────────

    async def semantic_search(
        self,
        query: str,
        developer_id: str | None = None,
        channel: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Return artifacts most semantically similar to query. Includes score."""
        if not self._ensure_client() or self._embedder is None:
            return []

        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            vec = await self._embedder.embed(query)

            must = []
            if developer_id:
                must.append(FieldCondition(key="developer_id", match=MatchValue(value=developer_id)))
            if channel:
                must.append(FieldCondition(key="channel", match=MatchValue(value=channel)))

            hits = self._client.search(  # type: ignore[union-attr]
                collection_name=_COLLECTION,
                query_vector=_fit(vec),
                query_filter=Filter(must=must) if must else None,
                limit=limit,
            )

            results = []
            for h in hits:
                p = dict(h.payload or {})
                p["score"] = round(h.score, 4)
                try:
                    p["payload"] = json.loads(p.pop("payload_json", "{}"))
                except Exception:
                    p["payload"] = {}
                results.append(p)
            return results
        except Exception as exc:
            logger.warning("Qdrant semantic_search failed: %s", exc)
            return []

    # ── Stats ──────────────────────────────────────────────────────────────────

    def count(self) -> int:
        if not self._ensure_client():
            return len(self._fallback)
        try:
            info = self._client.get_collection(_COLLECTION)  # type: ignore[union-attr]
            return info.points_count or 0
        except Exception:
            return 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _point_to_row(point) -> ReceivedArtifactRow:
    p = point.payload or {}
    return ReceivedArtifactRow(
        approval_id=p.get("approval_id", ""),
        developer_id=p.get("developer_id", ""),
        artifact_type=p.get("artifact_type", ""),
        draft_id=p.get("draft_id", ""),
        channel=p.get("channel", ""),
        payload_json=p.get("payload_json", "{}"),
        approved_at=p.get("approved_at", ""),
        received_at=p.get("received_at", ""),
    )


def _artifact_text(artifact_type: str, payload: dict) -> str:
    try:
        if artifact_type == "weekly_digest":
            highlights = payload.get("highlights", [])
            commits = payload.get("commits", [])
            return " ".join(highlights[:5] + commits[:3])
        if artifact_type == "commit_report":
            return (
                f"{payload.get('author_msg', '')} "
                f"{payload.get('generated_summary', '')} "
                f"{' '.join(payload.get('files_touched', [])[:5])}"
            )
        if artifact_type == "standup":
            return " ".join(payload.get("yesterday", [])[:3] + payload.get("today", [])[:3])
        if artifact_type == "pr_draft":
            return (
                f"{payload.get('title', '')} "
                f"{payload.get('summary', '')} "
                f"{payload.get('rationale', '')}"
            )
    except Exception:
        pass
    return str(payload)[:400]


def _to_uuid(s: str) -> str:
    try:
        UUID(s)
        return s
    except ValueError:
        return str(uuid4())


def _fit(v: list[float]) -> list[float]:
    if len(v) == _VECTOR_SIZE:
        return v
    if len(v) < _VECTOR_SIZE:
        return v + [0.0] * (_VECTOR_SIZE - len(v))
    return v[:_VECTOR_SIZE]
