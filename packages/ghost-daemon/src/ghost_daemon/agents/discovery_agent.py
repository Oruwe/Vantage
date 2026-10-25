"""Ghost-side agent that answers manager broadcast questions.

Pulls from live activity signals + optional LLM for narrative summarization.
Policy-gated through the same SharingPolicy that governs probes — if LIVE_PROBE
is withheld, the agent returns a non-content answer (e.g. "I am active" / "idle"),
never raw session excerpts.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from vantage.schemas import Channel, PolicyDecision, Turn

from ghost_daemon.agents.activity_extractor import extract_activity_signals
from ghost_daemon.approval.policy import load_policy
from ghost_daemon.redaction.scrubber import scrub

if TYPE_CHECKING:
    from ghost_daemon.config import Config
    from ghost_daemon.models.registry import LLMProvider
    from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)


class DiscoveryAgent:
    def __init__(
        self,
        cfg: "Config",
        llm: "LLMProvider | None",
        tailer: "SessionTailer",
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tailer = tailer

    async def answer(self, prompt: str, subscriber_id: str) -> dict:
        """Return a compact dict ready to serialize into a DISCOVERY_RESPONSE envelope."""
        policy = load_policy(subscriber_id, self._cfg.vantage_dir)
        allowed = Channel.LIVE_PROBE in policy.channels

        turns = await self._collect_turns(limit=12 if allowed else 4)
        signals = extract_activity_signals(turns, cwd=str(Path.cwd()))

        if not allowed:
            # Terse answer — just presence/activity, no content excerpts.
            answer = self._non_content_answer(signals, prompt)
            return {
                "developer_id": self._cfg.developer_id,
                "policy_decision": PolicyDecision.DENIED_BY_POLICY.value,
                "answer": answer,
                "signals": {
                    "primary_activity": signals.primary_activity,
                    "active_repo": signals.active_repo,
                },
            }

        answer = self._content_answer(signals, prompt)
        return {
            "developer_id": self._cfg.developer_id,
            "policy_decision": PolicyDecision.ALLOWED.value,
            "answer": answer,
            "signals": signals.model_dump(mode="json"),
        }

    async def _collect_turns(self, limit: int = 12) -> list[Turn]:
        raw_turns: list[Turn] = []
        for session_dir in self._cfg.resolved_session_dirs:
            jsonl_files = sorted(
                session_dir.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for jsonl in jsonl_files[:3]:
                try:
                    turns = await self._tailer.tail_recent(jsonl, n=limit)
                    raw_turns.extend(turns)
                    if len(raw_turns) >= limit:
                        break
                except Exception:
                    continue
            if len(raw_turns) >= limit:
                break
        scrubbed: list[Turn] = []
        for t in raw_turns[:limit]:
            rc = scrub(f"[{t.role}] {t.content_excerpt}")
            scrubbed.append(Turn(
                role=t.role,
                content_excerpt=rc.scrubbed_text[:400],
                tool_name=t.tool_name,
                timestamp=t.timestamp,
            ))
        return scrubbed

    @staticmethod
    def _non_content_answer(signals, prompt: str) -> str:
        if signals.primary_activity == "unknown":
            return "idle (no active session)"
        return f"{signals.primary_activity} in {signals.active_repo or 'unknown repo'}"

    @staticmethod
    def _content_answer(signals, prompt: str) -> str:
        parts = []
        if signals.primary_activity and signals.primary_activity != "unknown":
            parts.append(signals.primary_activity)
        if signals.active_repo:
            parts.append(signals.active_repo)
        if signals.inferred_branch:
            parts.append(signals.inferred_branch)
        if signals.files_recently_touched:
            parts.append("files: " + ", ".join(signals.files_recently_touched[:3]))
        if signals.last_user_intent:
            parts.append('intent: "' + signals.last_user_intent[:100] + '"')
        return " · ".join(parts) or "no activity signals yet"
