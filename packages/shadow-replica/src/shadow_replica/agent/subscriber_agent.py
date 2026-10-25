from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shadow_replica.storage.received_store import ReceivedStore, ReceivedArtifactRow
    from shadow_replica.models.embedder import Embedder

logger = logging.getLogger(__name__)


@dataclass
class QueryRoute:
    """Classification of an incoming question used by the agent to route retrieval."""
    kind: str                     # "why" | "status" | "general"
    channel_hint: str | None = None


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
        route = classify_query(query)
        channel_hint = channel or route.channel_hint

        if self._embedder is not None:
            # Expert-routing queries ("why did we…") pull more context from
            # commits + digests. For those, run semantic search without a
            # channel filter — rely on commit_bias in synthesis to prioritize.
            search_channel = channel_hint if route.kind != "why" else None
            items = await self._store.semantic_search(
                query=query,
                developer_id=developer_id,
                channel=search_channel,
                limit=12 if route.kind == "why" else 8,
            )
            sources = "semantic"
            rows = [_dict_to_row(i) for i in items]
            if route.kind == "why":
                rows = _reweight_for_why(rows)
        else:
            rows = self._keyword_retrieve(query, developer_id, channel_hint, route)
            sources = "keyword"

        answer = await self._synthesise(query, rows, route)

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
            "route": route.kind,
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
        self,
        query: str,
        developer_id: str | None,
        channel: str | None,
        route: "QueryRoute | None" = None,
    ) -> list["ReceivedArtifactRow"]:
        if developer_id:
            rows = self._store.list_for_developer(developer_id, channel=channel, limit=10)
        else:
            rows = self._store.list_all(channel=channel, limit=20)
        if route and route.kind == "why":
            return _reweight_for_why(rows)
        return rows

    # ── Synthesis ──────────────────────────────────────────────────────────────

    async def _synthesise(self, query: str, rows: list, route: "QueryRoute | None" = None) -> str:
        if not rows:
            return "No matching artifacts found."

        if self._llm is not None:
            try:
                return await self._llm_answer(query, rows, route)
            except Exception as exc:
                logger.warning("LLM synthesis failed (%s) — using structured output", exc)

        return _format_rows(rows)

    async def _llm_answer(self, query: str, rows: list, route: "QueryRoute | None" = None) -> str:
        context_parts = []
        for i, row in enumerate(rows[:6], start=1):
            try:
                if hasattr(row, "payload_json"):
                    payload = json.loads(row.payload_json)
                else:
                    payload = row.get("payload", {})
                received = getattr(row, "received_at", row.get("received_at", ""))[:19]
                dev = getattr(row, "developer_id", row.get("developer_id", ""))
                atype = getattr(row, "artifact_type", row.get("artifact_type", ""))
                context_parts.append(
                    f"[S{i}] [{received}] {atype} from {dev}:\n{json.dumps(payload, indent=2)[:700]}"
                )
            except Exception:
                continue

        context = "\n\n---\n\n".join(context_parts)
        route_kind = (route.kind if route else "general")
        if route_kind == "why":
            system = (
                "You are an institutional-reasoning engine for an engineering team. "
                "Given approved commit reports, standups, and PR drafts, explain *why* a "
                "decision was made or an approach was chosen. Prefer evidence from commit "
                "rationales and PR 'rationale' / 'risk_notes' fields. Cite sources inline "
                "using the [S1], [S2]… markers. If the artifacts don't contain the rationale, "
                "say so explicitly rather than speculating."
            )
            user_tail = (
                f"\n\nQuestion: {query}\n\nAnswer in 3–6 sentences. Cite [S#] markers for every "
                "specific claim. If you cannot find rationale, say so clearly."
            )
        elif route_kind == "status":
            system = (
                "You are an engineering status reporter. Summarize what's currently in flight, "
                "shipped recently, and blocked. Prefer standups and recent commits. Cite [S#] inline."
            )
            user_tail = f"\n\nQuestion: {query}\n\nAnswer concisely. Cite [S#] inline."
        else:
            system = (
                "You are a helpful assistant answering questions about developer work. "
                "You have access only to artifacts the developer explicitly approved for sharing. "
                "Be concise and factual. Cite [S#] inline when stating specific facts. "
                "If the artifacts don't contain the answer, say so."
            )
            user_tail = f"\n\nQuestion: {query}"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Approved artifacts:\n\n{context}{user_tail}"},
        ]

        return await self._llm.complete(messages, max_tokens=600)


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


_WHY_PATTERNS = (
    "why did", "why do", "why does", "why not", "why we", "why is",
    "rationale", "reasoning behind", "reason for", "reason behind",
    "what motivated", "what led to", "the thinking behind",
    "why'd", "why has", "why have",
)
_STATUS_PATTERNS = (
    "what are we working", "what's in flight", "whats in flight",
    "what's shipped", "whats shipped", "what shipped",
    "any blockers", "who is blocked", "what blocked", "what is blocked",
    "status of", "current status",
)


def classify_query(query: str) -> QueryRoute:
    """Decide whether a question is seeking rationale, status, or general info.

    "why did we…" → rationale → weight commits + PR drafts, cite inline.
    "what's shipped / in flight / blocked" → status → weight standups + commits.
    Anything else → general.
    """
    q = (query or "").lower().strip()
    if not q:
        return QueryRoute(kind="general", channel_hint=None)
    if any(p in q for p in _WHY_PATTERNS):
        return QueryRoute(kind="why", channel_hint=None)
    if any(p in q for p in _STATUS_PATTERNS):
        return QueryRoute(kind="status", channel_hint=None)
    return QueryRoute(kind="general", channel_hint=_classify_channel(q))


def _reweight_for_why(rows: list) -> list:
    """Push commit reports and PR drafts to the front for rationale queries.

    These two artifact types carry the richest "why" fields (`author_msg`,
    `generated_summary`, `rationale`, `risk_notes`). Standups and digests are
    kept but demoted.
    """
    priority = {"commit_report": 0, "pr_draft": 1, "weekly_digest": 2, "standup": 3}
    def key(r):
        atype = getattr(r, "artifact_type", "") if hasattr(r, "artifact_type") else r.get("artifact_type", "")
        return priority.get(atype, 9)
    return sorted(rows, key=key)


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
