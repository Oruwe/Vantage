from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shadow_replica.storage.received_store import ReceivedStore, ReceivedArtifactRow
    from shadow_replica.models.embedder import Embedder

logger = logging.getLogger(__name__)


class SubscriberAgent:
    """Question-answering agent over approved artifacts stored in Qdrant.

    Steps:
      1. Semantic search (Qdrant) if embedder is available; otherwise keyword filter
      2. Synthesise answer with LLM if configured; otherwise structured summary
    """

    def __init__(
        self,
        store: "ReceivedStore",
        llm=None,
        embedder: "Embedder | None" = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._embedder = embedder

    async def run_async(
        self,
        query: str,
        developer_id: str | None = None,
        channel: str | None = None,
    ) -> dict:
        channel_hint = _classify_channel(query) if not channel else channel

        if self._embedder is not None:
            items = await self._store.semantic_search(
                query=query,
                developer_id=developer_id,
                channel=channel_hint,
                limit=8,
            )
            sources = "semantic"
            rows = [_dict_to_row(i) for i in items]
        else:
            rows = self._keyword_retrieve(query, developer_id, channel_hint)
            sources = "keyword"

        answer = await self._synthesise(query, rows)

        sources_list = [
            {
                "developer_id": r.developer_id,
                "artifact_type": r.artifact_type,
                "received_at": r.received_at,
                "approved_at": r.approved_at,
            }
            for r in rows
        ]

        return {
            "query": query,
            "retrieval": sources,
            "developer_id": developer_id,
            "answer": answer,
            "sources_used": len(rows),
            "sources": sources_list,
        }

    def run(
        self,
        query: str,
        developer_id: str | None = None,
        channel: str | None = None,
    ) -> dict:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Inside an async context (FastAPI); caller should use run_async instead
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, self.run_async(query, developer_id, channel))
                    return future.result(timeout=30)
            return loop.run_until_complete(self.run_async(query, developer_id, channel))
        except Exception:
            return asyncio.run(self.run_async(query, developer_id, channel))

    # ── Retrieval ──────────────────────────────────────────────────────────────

    def _keyword_retrieve(
        self, query: str, developer_id: str | None, channel: str | None
    ) -> list["ReceivedArtifactRow"]:
        if developer_id:
            rows = self._store.list_for_developer(developer_id, channel=channel, limit=10)
        else:
            rows = self._store.list_all(channel=channel, limit=20)
        return rows

    # ── Synthesis ──────────────────────────────────────────────────────────────

    async def _synthesise(self, query: str, rows: list) -> str:
        if not rows:
            return "No matching artifacts found."

        if self._llm is not None:
            try:
                return await self._llm_answer(query, rows)
            except Exception as exc:
                logger.warning("LLM synthesis failed (%s) — using structured output", exc)

        return _format_rows(rows)

    async def _llm_answer(self, query: str, rows: list) -> str:
        context_parts = []
        for row in rows[:6]:
            try:
                if hasattr(row, "payload_json"):
                    payload = json.loads(row.payload_json)
                else:
                    payload = row.get("payload", {})
                received = getattr(row, "received_at", row.get("received_at", ""))[:19]
                dev = getattr(row, "developer_id", row.get("developer_id", ""))
                atype = getattr(row, "artifact_type", row.get("artifact_type", ""))
                context_parts.append(
                    f"[{received}] {atype} from {dev}:\n{json.dumps(payload, indent=2)[:600]}"
                )
            except Exception:
                continue

        context = "\n\n---\n\n".join(context_parts)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant answering questions about developer work. "
                    "You have access only to artifacts the developer explicitly approved for sharing. "
                    "Be concise and factual. If the artifacts don't contain the answer, say so."
                ),
            },
            {
                "role": "user",
                "content": f"Based on these approved artifacts:\n\n{context}\n\nQuestion: {query}",
            },
        ]

        return await self._llm.complete(messages, max_tokens=500)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _classify_channel(query: str) -> str | None:
    q = query.lower()
    if any(w in q for w in ("last week", "this week", "weekly", "digest", "summary")):
        return "weekly_digest"
    if any(w in q for w in ("standup", "yesterday", "today", "bullets")):
        return "standup"
    if any(w in q for w in ("commit", "change", "diff", "merge")):
        return "commit_report"
    if any(w in q for w in ("pr", "pull request", "description")):
        return "pr_draft"
    return None


def _format_rows(rows: list) -> str:
    lines = [f"Found {len(rows)} item(s).\n"]
    for row in rows[:6]:
        try:
            if hasattr(row, "payload_json"):
                payload = json.loads(row.payload_json)
                received = row.received_at[:19].replace("T", " ")
                atype = row.artifact_type
                dev = row.developer_id
            else:
                payload = row.get("payload", {})
                received = row.get("received_at", "")[:19].replace("T", " ")
                atype = row.get("artifact_type", "")
                dev = row.get("developer_id", "")
                if "score" in row:
                    received = f"{received} (score: {row['score']:.3f})"

            lines.append(f"— [{received}] {atype} from {dev}")

            if atype == "weekly_digest":
                for h in payload.get("highlights", [])[:2]:
                    lines.append(f"    • {h}")
            elif atype == "standup":
                for y in payload.get("yesterday", [])[:2]:
                    lines.append(f"    • {y}")
            elif atype == "commit_report":
                sha = payload.get("commit_sha", "?")[:8]
                summ = payload.get("generated_summary", "")[:100]
                lines.append(f"    {sha}  {summ}")
            elif atype == "pr_draft":
                lines.append(f"    {payload.get('title', '?')}")
        except Exception:
            continue

    if len(rows) > 6:
        lines.append(f"  … and {len(rows) - 6} more.")
    return "\n".join(lines)


def _dict_to_row(d: dict):
    """Wrap a search result dict so it's duck-type compatible with ReceivedArtifactRow."""
    from shadow_replica.storage.received_store import ReceivedArtifactRow
    payload = d.get("payload", {})
    return ReceivedArtifactRow(
        approval_id=d.get("approval_id", ""),
        developer_id=d.get("developer_id", ""),
        artifact_type=d.get("artifact_type", ""),
        draft_id=d.get("draft_id", ""),
        channel=d.get("channel", ""),
        payload_json=json.dumps(payload),
        approved_at=d.get("approved_at", ""),
        received_at=d.get("received_at", ""),
    )
