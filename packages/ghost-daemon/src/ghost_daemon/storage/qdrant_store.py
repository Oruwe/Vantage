from __future__ import annotations

import logging
from uuid import UUID

from vantage.schemas import IncrementalSummary

logger = logging.getLogger(__name__)

_COLLECTION = "vantage_summaries"
_VECTOR_SIZE = 1536  # matches anthropic zero-vector stub; ollama nomic is 768


class QdrantStore:
    def __init__(self, url: str = "http://localhost:6333") -> None:
        self._url = url
        self._client: object | None = None

    def _ensure_client(self) -> bool:
        if self._client is not None:
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
            return True
        except Exception as exc:
            logger.warning("Qdrant unavailable (%s) — embeddings skipped", exc)
            return False

    async def upsert(self, summary: IncrementalSummary, vector: list[float]) -> None:
        if not self._ensure_client():
            return
        try:
            from qdrant_client.models import PointStruct

            client = self._client  # type: ignore[assignment]
            payload = summary.model_dump(exclude={"summary_id"})
            payload["created_at"] = payload["created_at"].isoformat()
            client.upsert(
                collection_name=_COLLECTION,
                points=[PointStruct(
                    id=str(UUID(summary.summary_id)),
                    vector=_pad_or_trim(vector),
                    payload=payload,
                )],
            )
        except Exception as exc:
            logger.warning("Qdrant upsert failed: %s", exc)

    async def search(
        self, vector: list[float], developer_id: str, top_k: int = 6
    ) -> list[dict]:
        if not self._ensure_client():
            return []
        try:
            client = self._client  # type: ignore[assignment]
            hits = client.search(
                collection_name=_COLLECTION,
                query_vector=_pad_or_trim(vector),
                query_filter={"must": [{"key": "developer_id", "match": {"value": developer_id}}]},
                limit=top_k,
            )
            return [h.payload for h in hits]
        except Exception as exc:
            logger.warning("Qdrant search failed: %s", exc)
            return []


def _pad_or_trim(v: list[float]) -> list[float]:
    if len(v) == _VECTOR_SIZE:
        return v
    if len(v) < _VECTOR_SIZE:
        return v + [0.0] * (_VECTOR_SIZE - len(v))
    return v[:_VECTOR_SIZE]
