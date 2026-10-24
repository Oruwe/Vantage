from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from vantage.schemas import CommitReport, DraftPayload, RedactedContext
from ghost_daemon.config import Config
from ghost_daemon.models.registry import LLMProvider
from ghost_daemon.redaction.scrubber import scrub
from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a technical writing assistant generating a commit report for a developer's internal review queue.

Given the commit metadata and optional session context, produce a JSON object with exactly these keys:
{
  "generated_summary": "...",   // 2-4 sentences: what changed and why
  "open_questions": ["..."]     // 0-3 questions the reviewer might have
}

Be specific about what changed. Mention file counts and the nature of the change.
Do not invent detail not evidenced in the diff or context.
"""


class CommitState(TypedDict):
    developer_id: str
    repo: str
    commit_sha: str
    branch: str
    author_msg: str
    diff_stat: str
    files_touched: list[str]
    session_context: str
    redacted_context: RedactedContext | None
    llm_output: str | None
    draft: CommitReport | None
    error: str | None


class CommitAgent:
    def __init__(self, cfg: Config, llm: LLMProvider, tailer: SessionTailer) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tailer = tailer
        self._graph = self._build()

    async def run(
        self,
        repo_path: Path,
        commit_sha: str | None = None,
    ) -> DraftPayload[CommitReport]:
        meta = _get_commit_meta(repo_path, commit_sha)
        initial: CommitState = {
            "developer_id": self._cfg.developer_id,
            "repo": str(repo_path),
            "commit_sha": meta["sha"],
            "branch": meta["branch"],
            "author_msg": meta["message"],
            "diff_stat": meta["diff_stat"],
            "files_touched": meta["files_touched"],
            "session_context": "",
            "redacted_context": None,
            "llm_output": None,
            "draft": None,
            "error": None,
        }
        result = await self._graph.ainvoke(initial)
        if result.get("error"):
            raise RuntimeError(result["error"])
        draft: CommitReport = result["draft"]
        return DraftPayload(
            artifact_type="commit_report",
            payload=draft,
            developer_id=self._cfg.developer_id,
        )

    # ── Graph nodes ────────────────────────────────────────────────────────────

    async def _collect_context(self, state: CommitState) -> dict:
        recent_turns: list[str] = []
        for session_dir in self._cfg.resolved_session_dirs:
            for jsonl in session_dir.rglob("*.jsonl"):
                try:
                    turns = await self._tailer.tail_recent(jsonl, n=20)
                    recent_turns.extend(
                        f"[{t.role}] {t.content_excerpt}" for t in turns
                    )
                    if len(recent_turns) > 60:
                        break
                except Exception:
                    continue
            if len(recent_turns) > 60:
                break
        return {"session_context": "\n".join(recent_turns[-60:])}

    async def _scrub(self, state: CommitState) -> dict:
        raw = (
            f"Commit: {state['commit_sha'][:12]} on {state['branch']}\n"
            f"Message: {state['author_msg']}\n"
            f"Diff stats:\n{state['diff_stat']}\n\n"
            f"Recent session:\n{state['session_context']}"
        )
        return {"redacted_context": scrub(raw)}

    async def _summarize(self, state: CommitState) -> dict:
        ctx = state["redacted_context"]
        if ctx is None:
            return {"llm_output": None}
        user_msg = (
            f"Commit {state['commit_sha'][:12]} on branch '{state['branch']}'\n"
            f"Message: {state['author_msg']}\n"
            f"Files touched ({len(state['files_touched'])}): "
            f"{', '.join(state['files_touched'][:10])}\n\n"
            f"Diff stats:\n{state['diff_stat'][:1500]}\n\n"
            f"Recent session context (redacted):\n{ctx.scrubbed_text[:2000]}"
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
        return {"llm_output": output}

    async def _draft_report(self, state: CommitState) -> dict:
        llm_output = state.get("llm_output") or ""
        parsed = _parse_llm_json(llm_output)
        report = CommitReport(
            developer_id=state["developer_id"],
            repo=state["repo"],
            commit_sha=state["commit_sha"],
            branch=state["branch"],
            diff_stat=state["diff_stat"],
            files_touched=state["files_touched"],
            author_msg=state["author_msg"],
            generated_summary=parsed.get("generated_summary", llm_output[:500] or "No summary generated."),
            open_questions=parsed.get("open_questions", []),
        )
        return {"draft": report}

    # ── Graph wiring ───────────────────────────────────────────────────────────

    def _build(self) -> Any:
        g: StateGraph = StateGraph(CommitState)
        g.add_node("collect_context", self._collect_context)
        g.add_node("scrub", self._scrub)
        g.add_node("summarize", self._summarize)
        g.add_node("draft_report", self._draft_report)
        g.add_edge(START, "collect_context")
        g.add_edge("collect_context", "scrub")
        g.add_edge("scrub", "summarize")
        g.add_edge("summarize", "draft_report")
        g.add_edge("draft_report", END)
        return g.compile()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_commit_meta(repo_path: Path, sha: str | None) -> dict:
    def _git(*args) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=repo_path, text=True, timeout=10, stderr=subprocess.DEVNULL
        ).strip()

    target = sha or "HEAD"
    try:
        resolved_sha = _git("rev-parse", target)
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        message = _git("log", "-1", "--format=%s", resolved_sha)
        diff_stat = _git("show", "--stat", "--format=", resolved_sha)
        files_raw = _git("show", "--name-only", "--format=", resolved_sha)
        files = [f for f in files_raw.splitlines() if f.strip()]
        return {
            "sha": resolved_sha,
            "branch": branch,
            "message": message,
            "diff_stat": diff_stat[:2000],
            "files_touched": files,
        }
    except Exception as exc:
        logger.warning("git metadata failed for %s in %s: %s", target, repo_path, exc)
        return {
            "sha": target,
            "branch": "unknown",
            "message": "",
            "diff_stat": "",
            "files_touched": [],
        }


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
        return {"generated_summary": text, "open_questions": []}
