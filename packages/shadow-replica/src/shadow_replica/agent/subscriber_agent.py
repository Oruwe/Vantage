from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shadow_replica.storage.received_store import ReceivedStore, ReceivedArtifactRow

logger = logging.getLogger(__name__)


@dataclass
class AgentState:
    query: str
    developer_id: str | None
    channel_filter: str | None
    intent: str = ""
    retrieved_items: list = field(default_factory=list)
    answer: str = ""


class SubscriberAgent:
    """Simple question-answering agent that reasons over received (already-approved) artifacts.

    Implements classify_intent → retrieve_received → respond steps. Operates purely
    on data already approved by the developer — no access to raw session content.

    LLM is optional: if not configured the agent falls back to structured summaries.
    """

    def __init__(self, store: "ReceivedStore", llm=None) -> None:
        self._store = store
        self._llm = llm

    def run(
        self,
        query: str,
        developer_id: str | None = None,
        channel: str | None = None,
    ) -> dict:
        state = AgentState(query=query, developer_id=developer_id, channel_filter=channel)
        state = self._classify_intent(state)
        state = self._retrieve_received(state)
        state = self._respond(state)
        return {
            "query": state.query,
            "intent": state.intent,
            "developer_id": state.developer_id,
            "answer": state.answer,
            "sources_used": len(state.retrieved_items),
        }

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _classify_intent(self, state: AgentState) -> AgentState:
        q = state.query.lower()
        if any(w in q for w in ("last week", "this week", "weekly", "digest", "summary")):
            state.intent = "weekly_digest"
            if not state.channel_filter:
                state.channel_filter = "weekly_digest"
        elif any(w in q for w in ("standup", "yesterday", "today", "bullets")):
            state.intent = "standup"
            if not state.channel_filter:
                state.channel_filter = "standup"
        elif any(w in q for w in ("commit", "change", "diff", "merge")):
            state.intent = "commit_report"
            if not state.channel_filter:
                state.channel_filter = "commit_report"
        elif any(w in q for w in ("pr", "pull request", "description")):
            state.intent = "pr_draft"
            if not state.channel_filter:
                state.channel_filter = "pr_draft"
        else:
            state.intent = "general"
        return state

    def _retrieve_received(self, state: AgentState) -> AgentState:
        if state.developer_id:
            rows = self._store.list_for_developer(
                state.developer_id, channel=state.channel_filter, limit=10
            )
        else:
            rows = self._store.list_all(channel=state.channel_filter, limit=20)
        state.retrieved_items = rows
        return state

    def _respond(self, state: AgentState) -> AgentState:
        if not state.retrieved_items:
            state.answer = (
                f"No {state.channel_filter or 'artifact'}s found"
                + (f" for developer {state.developer_id}" if state.developer_id else "")
                + "."
            )
            return state

        if self._llm is not None:
            state.answer = self._llm_respond(state)
        else:
            state.answer = self._format_items(state)
        return state

    def _format_items(self, state: AgentState) -> str:
        lines = [f"Found {len(state.retrieved_items)} item(s) matching your query.\n"]
        for row in state.retrieved_items[:5]:
            payload = json.loads(row.payload_json)
            received = row.received_at[:19].replace("T", " ")
            lines.append(f"— [{received}] {row.artifact_type} from {row.developer_id}")
            if row.artifact_type == "weekly_digest":
                highlights = payload.get("highlights", [])
                for h in highlights[:3]:
                    lines.append(f"    • {h}")
            elif row.artifact_type == "standup":
                yesterday = payload.get("yesterday", [])
                for y in yesterday[:3]:
                    lines.append(f"    • {y}")
            elif row.artifact_type == "commit_report":
                lines.append(f"    SHA: {payload.get('commit_sha', '?')[:8]}  {payload.get('generated_summary', '')[:120]}")
            elif row.artifact_type == "pr_draft":
                lines.append(f"    {payload.get('title', '?')}")
        if len(state.retrieved_items) > 5:
            lines.append(f"  … and {len(state.retrieved_items) - 5} more.")
        return "\n".join(lines)

    def _llm_respond(self, state: AgentState) -> str:
        import asyncio

        context_parts = []
        for row in state.retrieved_items[:5]:
            payload = json.loads(row.payload_json)
            received = row.received_at[:19]
            context_parts.append(
                f"[{received}] {row.artifact_type} from {row.developer_id}:\n{json.dumps(payload, indent=2)[:800]}"
            )
        context = "\n\n---\n\n".join(context_parts)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant that answers questions about a developer's work. "
                    "You have access only to artifacts the developer has explicitly approved for sharing. "
                    "Be concise and factual."
                ),
            },
            {
                "role": "user",
                "content": f"Based on these approved artifacts:\n\n{context}\n\nQuestion: {state.query}",
            },
        ]

        try:
            return asyncio.run(self._llm.complete(messages, max_tokens=400))
        except Exception as exc:
            logger.warning("LLM respond failed: %s — falling back to formatted output", exc)
            return self._format_items(state)
