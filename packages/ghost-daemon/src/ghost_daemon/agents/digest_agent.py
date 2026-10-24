from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from vantage.schemas import DraftPayload, RedactedContext, Turn, WeeklyDigest
from ghost_daemon.config import Config
from ghost_daemon.models.registry import LLMProvider
from ghost_daemon.redaction.scrubber import scrub
from ghost_daemon.watchers.project_map import ProjectMap
from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a technical writing assistant. The developer cannot see your output — it goes to their manager dashboard only after the developer reviews and approves it.

Generate a concise, factual weekly engineering digest from the provided session context and git activity. Structure your response as valid JSON matching exactly this schema:
{
  "highlights": ["..."],        // 3-5 key accomplishments
  "commits": ["..."],           // brief summary of each commit
  "open_threads": ["..."],      // ongoing work, not yet committed
  "blockers": ["..."],          // explicit blockers or questions raised
  "next_week": ["..."],         // likely next steps based on context
  "redactions_summary": "..."   // one sentence describing what was redacted
}

Be specific and technical. Use the developer's own terminology. Do not invent work not evidenced in the context.
"""


class DigestState(TypedDict):
    developer_id: str
    period_start: datetime
    period_end: datetime
    repo_filter: str | None
    repo_paths: list[str]
    raw_turns: list[dict]
    redacted_context: RedactedContext | None
    llm_output: str | None
    draft: WeeklyDigest | None
    error: str | None


class DigestAgent:
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

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(
        self,
        period_start: datetime,
        period_end: datetime,
        repo_filter: str | None = None,
    ) -> DraftPayload[WeeklyDigest]:
        initial: DigestState = {
            "developer_id": self._cfg.developer_id,
            "period_start": period_start,
            "period_end": period_end,
            "repo_filter": repo_filter,
            "repo_paths": [],
            "raw_turns": [],
            "redacted_context": None,
            "llm_output": None,
            "draft": None,
            "error": None,
        }
        result = await self._graph.ainvoke(initial)

        if result.get("error"):
            raise RuntimeError(result["error"])

        digest: WeeklyDigest = result["draft"]
        return DraftPayload(
            artifact_type="weekly_digest",
            payload=digest,
            developer_id=self._cfg.developer_id,
        )

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    async def _collect_window(self, state: DigestState) -> dict:
        period_start = state["period_start"]
        period_end = state["period_end"]
        repo_filter = state["repo_filter"]

        self._project_map.refresh()

        repo_paths: list[str] = []
        raw_turns: list[dict] = []

        for session_dir in self._cfg.resolved_session_dirs:
            for jsonl in session_dir.rglob("*.jsonl"):
                try:
                    turns = await self._tailer.tail_recent(jsonl, n=60)
                except Exception as exc:
                    logger.warning("Could not tail %s: %s", jsonl.name, exc)
                    continue

                in_window = [
                    t for t in turns
                    if t.timestamp is None
                    or (period_start.replace(tzinfo=None) <= t.timestamp.replace(tzinfo=None) <= period_end.replace(tzinfo=None))
                ]
                if in_window:
                    raw_turns.extend(t.model_dump() for t in in_window)
                    repo_paths.append(str(jsonl))

        # Git commits in window
        git_context = _collect_git_commits(period_start, period_end, repo_filter)
        if git_context:
            raw_turns.insert(0, {"role": "system", "content_excerpt": git_context})

        if not raw_turns:
            logger.info("No session turns or commits found in window %s–%s", period_start.date(), period_end.date())

        return {"raw_turns": raw_turns, "repo_paths": repo_paths}

    async def _scrub(self, state: DigestState) -> dict:
        combined = "\n".join(
            f"[{t.get('role', '?')}] {t.get('content_excerpt', '')}"
            for t in state["raw_turns"]
        )
        redacted = scrub(combined)
        return {"redacted_context": redacted}

    async def _summarize(self, state: DigestState) -> dict:
        ctx = state["redacted_context"]
        if ctx is None or not ctx.scrubbed_text.strip():
            return {"llm_output": None}

        period_start = state["period_start"]
        period_end = state["period_end"]

        user_msg = (
            f"Period: {period_start.date()} to {period_end.date()}\n\n"
            f"Session context:\n{ctx.scrubbed_text[:6000]}\n\n"
            f"Redaction note: {len(ctx.redactions)} item(s) were redacted."
        )

        try:
            output = await self._llm.complete(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=800,
            )
        except Exception as exc:
            return {"error": f"LLM call failed: {exc}"}

        return {"llm_output": output}

    async def _draft_digest(self, state: DigestState) -> dict:
        llm_output = state.get("llm_output")
        period_start = state["period_start"]
        period_end = state["period_end"]
        ctx = state.get("redacted_context")

        if not llm_output:
            digest = WeeklyDigest(
                period_start=period_start,
                period_end=period_end,
                highlights=["No session activity found in this period."],
                commits=[],
                open_threads=[],
                blockers=[],
                next_week=[],
                redactions_summary="No redactions.",
            )
            return {"draft": digest}

        parsed = _parse_llm_json(llm_output)
        redactions_summary = (
            f"{len(ctx.redactions)} item(s) redacted (kinds: {_redaction_kinds(ctx)})."
            if ctx and ctx.redactions
            else "No redactions."
        )

        digest = WeeklyDigest(
            period_start=period_start,
            period_end=period_end,
            highlights=parsed.get("highlights", []),
            commits=parsed.get("commits", []),
            open_threads=parsed.get("open_threads", []),
            blockers=parsed.get("blockers", []),
            next_week=parsed.get("next_week", []),
            redactions_summary=parsed.get("redactions_summary", redactions_summary),
        )
        return {"draft": digest}

    # ------------------------------------------------------------------
    # Graph wiring
    # ------------------------------------------------------------------

    def _build(self) -> Any:
        g: StateGraph = StateGraph(DigestState)
        g.add_node("collect_window", self._collect_window)
        g.add_node("scrub", self._scrub)
        g.add_node("summarize", self._summarize)
        g.add_node("draft_digest", self._draft_digest)

        g.add_edge(START, "collect_window")
        g.add_edge("collect_window", "scrub")
        g.add_edge("scrub", "summarize")
        g.add_edge("summarize", "draft_digest")
        g.add_edge("draft_digest", END)

        return g.compile()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _collect_git_commits(start: datetime, end: datetime, repo_filter: str | None) -> str:
    cwd = Path(repo_filter) if repo_filter else Path.cwd()
    if not (cwd / ".git").exists():
        return ""
    try:
        since = start.strftime("%Y-%m-%d")
        until = end.strftime("%Y-%m-%d")
        out = subprocess.check_output(
            ["git", "log", f"--since={since}", f"--until={until}", "--oneline", "--stat"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
        return out[:3000]
    except Exception:
        return ""


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    # Extract JSON block if wrapped in markdown code fences
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"highlights": [text], "commits": [], "open_threads": [], "blockers": [], "next_week": []}


def _redaction_kinds(ctx: RedactedContext) -> str:
    kinds = sorted({r.kind for r in ctx.redactions})
    return ", ".join(kinds) if kinds else "none"
