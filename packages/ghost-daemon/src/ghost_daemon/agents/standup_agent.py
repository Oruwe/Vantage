from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from vantage.schemas import DraftPayload, RedactedContext, StandupBullets
from ghost_daemon.config import Config
from ghost_daemon.models.registry import LLMProvider
from ghost_daemon.redaction.scrubber import scrub
from ghost_daemon.watchers.project_map import ProjectMap
from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a technical writing assistant generating standup notes for a developer.

Given yesterday's session context and git activity, produce a JSON object with exactly these keys:
{
  "yesterday": ["..."],   // 2-5 bullets: what was accomplished yesterday
  "today": ["..."],       // 2-4 bullets: likely next steps based on context
  "blockers": ["..."]     // 0-3 bullets: open questions or impediments (omit if none)
}

Each bullet should be a single concrete sentence. Use first-person perspective.
Do not invent work not evidenced in the context.
"""


class StandupState(TypedDict):
    developer_id: str
    yesterday_start: datetime
    yesterday_end: datetime
    raw_turns: list[dict]
    git_activity: str
    redacted_context: RedactedContext | None
    llm_output: str | None
    draft: StandupBullets | None
    error: str | None


class StandupAgent:
    def __init__(
        self,
        cfg: Config,
        llm: LLMProvider,
        project_map: ProjectMap,
        tailer: SessionTailer,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._project_map = project_map
        self._tailer = tailer
        self._graph = self._build()

    async def run(self, since: datetime | None = None) -> DraftPayload[StandupBullets]:
        now = datetime.utcnow()
        yesterday_start = since or (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        yesterday_end = now
        initial: StandupState = {
            "developer_id": self._cfg.developer_id,
            "yesterday_start": yesterday_start,
            "yesterday_end": yesterday_end,
            "raw_turns": [],
            "git_activity": "",
            "redacted_context": None,
            "llm_output": None,
            "draft": None,
            "error": None,
        }
        result = await self._graph.ainvoke(initial)
        if result.get("error"):
            raise RuntimeError(result["error"])
        draft: StandupBullets = result["draft"]
        return DraftPayload(
            artifact_type="standup",
            payload=draft,
            developer_id=self._cfg.developer_id,
        )

    # ── Graph nodes ────────────────────────────────────────────────────────────

    async def _collect_yesterday(self, state: StandupState) -> dict:
        period_start = state["yesterday_start"]
        period_end = state["yesterday_end"]

        self._project_map.refresh()

        raw_turns: list[dict] = []
        for session_dir in self._cfg.resolved_session_dirs:
            for jsonl in session_dir.rglob("*.jsonl"):
                try:
                    turns = await self._tailer.tail_recent(jsonl, n=40)
                except Exception:
                    continue
                in_window = [
                    t for t in turns
                    if t.timestamp is None
                    or (
                        period_start.replace(tzinfo=None)
                        <= t.timestamp.replace(tzinfo=None)
                        <= period_end.replace(tzinfo=None)
                    )
                ]
                raw_turns.extend(t.model_dump() for t in in_window)

        git_activity = _collect_git_activity(period_start, self._cfg)
        return {"raw_turns": raw_turns, "git_activity": git_activity}

    async def _scrub(self, state: StandupState) -> dict:
        turns_text = "\n".join(
            f"[{t.get('role', '?')}] {t.get('content_excerpt', '')}"
            for t in state["raw_turns"]
        )
        combined = f"Git activity:\n{state['git_activity']}\n\nSession turns:\n{turns_text}"
        return {"redacted_context": scrub(combined)}

    async def _draft_bullets(self, state: StandupState) -> dict:
        ctx = state["redacted_context"]
        if ctx is None or not ctx.scrubbed_text.strip():
            draft = StandupBullets(
                yesterday=["No session activity found for this period."],
                today=[],
                blockers=[],
            )
            return {"draft": draft}

        user_msg = (
            f"Period: {state['yesterday_start'].date()} to {state['yesterday_end'].date()}\n\n"
            f"Context (redacted):\n{ctx.scrubbed_text[:4000]}"
        )
        try:
            output = await self._llm.complete(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=400,
            )
        except Exception as exc:
            return {"error": f"LLM call failed: {exc}"}

        parsed = _parse_llm_json(output)
        draft = StandupBullets(
            yesterday=parsed.get("yesterday", []),
            today=parsed.get("today", []),
            blockers=parsed.get("blockers", []),
        )
        return {"llm_output": output, "draft": draft}

    # ── Graph wiring ───────────────────────────────────────────────────────────

    def _build(self) -> Any:
        g: StateGraph = StateGraph(StandupState)
        g.add_node("collect_yesterday", self._collect_yesterday)
        g.add_node("scrub", self._scrub)
        g.add_node("draft_bullets", self._draft_bullets)
        g.add_edge(START, "collect_yesterday")
        g.add_edge("collect_yesterday", "scrub")
        g.add_edge("scrub", "draft_bullets")
        g.add_edge("draft_bullets", END)
        return g.compile()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _collect_git_activity(since: datetime, cfg: Config) -> str:
    since_str = since.strftime("%Y-%m-%d")
    results: list[str] = []
    roots = [Path(p) for p in cfg.project_roots] if cfg.project_roots else []
    if not roots:
        import os
        roots = [Path(os.getcwd())]
    for root in roots:
        if not (root / ".git").exists():
            continue
        try:
            out = subprocess.check_output(
                ["git", "log", f"--since={since_str}", "--oneline", "--stat"],
                cwd=root, text=True, timeout=10, stderr=subprocess.DEVNULL,
            )
            if out.strip():
                results.append(f"[{root.name}]\n{out.strip()[:1500]}")
        except Exception:
            continue
    return "\n\n".join(results)[:3000]


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"yesterday": [text[:300]], "today": [], "blockers": []}
