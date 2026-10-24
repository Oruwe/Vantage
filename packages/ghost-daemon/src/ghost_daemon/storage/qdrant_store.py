from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID, uuid4

from vantage.schemas import IncrementalSummary

logger = logging.getLogger(__name__)

# nomic-embed-text dimensions; Anthropic provider falls back to zeros at this size
_VECTOR_SIZE = 768

_COL_SUMMARIES = "vantage_summaries"
_COL_APPROVED  = "vantage_approved"


class QdrantStore:
    """Qdrant-backed vector store for summaries and approved artifacts.

    Two collections:
      vantage_summaries  — IncrementalSummary objects (context snippets)
      vantage_approved   — Fully approved artifacts (historical context for agents)

    Both degrade gracefully: if Qdrant is unavailable all operations are no-ops.
    """

    def __init__(self, url: str = "http://localhost:6333") -> None:
        self._url = url
        self._client: object | None = None

    # ── Connection ─────────────────────────────────────────────────────────────

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            client = QdrantClient(url=self._url, timeout=5)
            existing = {c.name for c in client.get_collections().collections}
            for col in (_COL_SUMMARIES, _COL_APPROVED):
                if col not in existing:
                    client.create_collection(
                        collection_name=col,
                        vectors_config=VectorParams(size=_VECTOR_SIZE, distance=Distance.COSINE),
                    )
            self._client = client
            return True
        except Exception as exc:
            logger.warning("Qdrant unavailable (%s) — vector ops skipped", exc)
            return False

    # ── Summaries (IncrementalSummary) ─────────────────────────────────────────

    async def upsert(self, summary: IncrementalSummary, vector: list[float]) -> None:
        if not self._ensure_client():
            return
        try:
            from qdrant_client.models import PointStruct

            payload = summary.model_dump(exclude={"summary_id"})
            payload["created_at"] = payload["created_at"].isoformat()
            self._client.upsert(  # type: ignore[union-attr]
                collection_name=_COL_SUMMARIES,
                points=[PointStruct(
                    id=str(UUID(summary.summary_id)),
                    vector=_fit(vector),
                    payload=payload,
                )],
            )
        except Exception as exc:
            logger.warning("Qdrant upsert (summaries) failed: %s", exc)

    async def search(
        self, vector: list[float], developer_id: str, top_k: int = 6
    ) -> list[dict]:
        if not self._ensure_client():
            return []
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            hits = self._client.search(  # type: ignore[union-attr]
                collection_name=_COL_SUMMARIES,
                query_vector=_fit(vector),
                query_filter=Filter(
                    must=[FieldCondition(key="developer_id", match=MatchValue(value=developer_id))]
                ),
                limit=top_k,
            )
            return [h.payload for h in hits]
        except Exception as exc:
            logger.warning("Qdrant search (summaries) failed: %s", exc)
            return []

    # ── Approved artifacts ─────────────────────────────────────────────────────

    async def upsert_approved(
        self,
        approved_id: str,
        developer_id: str,
        artifact_type: str,
        payload: dict,
        text: str,
        vector: list[float],
        approved_at: str | None = None,
    ) -> None:
        """Index an approved artifact so agents can retrieve historical context."""
        if not self._ensure_client():
            return
        try:
            from qdrant_client.models import PointStruct

            point_id = str(UUID(approved_id)) if _is_uuid(approved_id) else str(uuid4())
            doc = {
                "approved_id": approved_id,
                "developer_id": developer_id,
                "artifact_type": artifact_type,
                "payload_json": json.dumps(payload),
                "text_excerpt": text[:500],
                "approved_at": approved_at or datetime.utcnow().isoformat(),
            }
            self._client.upsert(  # type: ignore[union-attr]
                collection_name=_COL_APPROVED,
                points=[PointStruct(id=point_id, vector=_fit(vector), payload=doc)],
            )
        except Exception as exc:
            logger.warning("Qdrant upsert (approved) failed: %s", exc)

    async def search_approved(
        self,
        vector: list[float],
        developer_id: str,
        artifact_type: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """Return the top-k most similar previously approved artifacts."""
        if not self._ensure_client():
            return []
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            must = [FieldCondition(key="developer_id", match=MatchValue(value=developer_id))]
            if artifact_type:
                must.append(FieldCondition(key="artifact_type", match=MatchValue(value=artifact_type)))

            hits = self._client.search(  # type: ignore[union-attr]
                collection_name=_COL_APPROVED,
                query_vector=_fit(vector),
                query_filter=Filter(must=must),
                limit=top_k,
            )
            results = []
            for h in hits:
                p = dict(h.payload)
                p["score"] = h.score
                try:
                    p["payload"] = json.loads(p.pop("payload_json", "{}"))
                except Exception:
                    p["payload"] = {}
                results.append(p)
            return results
        except Exception as exc:
            logger.warning("Qdrant search (approved) failed: %s", exc)
            return []

    async def list_approved(
        self,
        developer_id: str,
        artifact_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Scroll through approved artifacts without a query vector (for listing)."""
        if not self._ensure_client():
            return []
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            must = [FieldCondition(key="developer_id", match=MatchValue(value=developer_id))]
            if artifact_type:
                must.append(FieldCondition(key="artifact_type", match=MatchValue(value=artifact_type)))

            results, _ = self._client.scroll(  # type: ignore[union-attr]
                collection_name=_COL_APPROVED,
                scroll_filter=Filter(must=must),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            out = []
            for r in results:
                p = dict(r.payload)
                try:
                    p["payload"] = json.loads(p.pop("payload_json", "{}"))
                except Exception:
                    p["payload"] = {}
                out.append(p)
            out.sort(key=lambda x: x.get("approved_at", ""), reverse=True)
            return out
        except Exception as exc:
            logger.warning("Qdrant scroll (approved) failed: %s", exc)
            return []


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fit(v: list[float]) -> list[float]:
    if len(v) == _VECTOR_SIZE:
        return v
    if len(v) < _VECTOR_SIZE:
        return v + [0.0] * (_VECTOR_SIZE - len(v))
    return v[:_VECTOR_SIZE]


def _is_uuid(s: str) -> bool:
    try:
        UUID(s)
        return True
    except ValueError:
        return False
