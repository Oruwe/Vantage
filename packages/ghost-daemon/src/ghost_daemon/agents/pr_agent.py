from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from vantage.schemas import DraftPayload, PRDraft, RedactedContext
from ghost_daemon.config import Config
from ghost_daemon.models.registry import LLMProvider
from ghost_daemon.redaction.scrubber import scrub
from ghost_daemon.watchers.session_tail import SessionTailer

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a technical writing assistant generating a pull request description.

Given the branch history, diff statistics, and optional session context, produce a JSON object with exactly these keys:
{
  "title": "...",        // concise PR title (imperative mood, ≤72 chars)
  "summary": "...",      // 2-4 sentences describing the change
  "rationale": "...",    // why this change is needed
  "test_notes": "...",   // how to verify / what was tested
  "risk_notes": "..."    // potential risks or rollback considerations
}

Be specific. Reflect the actual commits and files changed.
Do not invent rationale not evidenced in the context.
"""


class PRState(TypedDict):
    developer_id: str
    repo: str
    branch: str
    base_branch: str
    commits_summary: str
    diff_stat: str
    files_changed: list[str]
    session_context: str
    redacted_context: RedactedContext | None
    llm_output: str | None
    draft: PRDraft | None
    error: str | None


class PRAgent:
    def __init__(self, cfg: Config, llm: LLMProvider, tailer: SessionTailer) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tailer = tailer
        self._graph = self._build()

    async def run(
        self,
        repo_path: Path,
        base_branch: str = "main",
    ) -> DraftPayload[PRDraft]:
        branch_info = _get_branch_history(repo_path, base_branch)
        initial: PRState = {
            "developer_id": self._cfg.developer_id,
            "repo": str(repo_path),
            "branch": branch_info["branch"],
            "base_branch": base_branch,
            "commits_summary": branch_info["commits_summary"],
            "diff_stat": branch_info["diff_stat"],
            "files_changed": branch_info["files_changed"],
            "session_context": "",
            "redacted_context": None,
            "llm_output": None,
            "draft": None,
            "error": None,
        }
        result = await self._graph.ainvoke(initial)
        if result.get("error"):
            raise RuntimeError(result["error"])
        draft: PRDraft = result["draft"]
        return DraftPayload(
            artifact_type="pr_draft",
            payload=draft,
            developer_id=self._cfg.developer_id,
        )

    # ── Graph nodes ────────────────────────────────────────────────────────────

    async def _collect_history(self, state: PRState) -> dict:
        recent_turns: list[str] = []
        for session_dir in self._cfg.resolved_session_dirs:
            for jsonl in session_dir.rglob("*.jsonl"):
                try:
                    turns = await self._tailer.tail_recent(jsonl, n=30)
                    recent_turns.extend(
                        f"[{t.role}] {t.content_excerpt}" for t in turns
                    )
                    if len(recent_turns) > 80:
                        break
                except Exception:
                    continue
            if len(recent_turns) > 80:
                break
        return {"session_context": "\n".join(recent_turns[-80:])}

    async def _scrub(self, state: PRState) -> dict:
        raw = (
            f"Branch: {state['branch']} → {state['base_branch']}\n"
            f"Commits:\n{state['commits_summary']}\n\n"
            f"Diff stats:\n{state['diff_stat']}\n\n"
            f"Session context:\n{state['session_context']}"
        )
        return {"redacted_context": scrub(raw)}

    async def _draft_description(self, state: PRState) -> dict:
        ctx = state["redacted_context"]
        if ctx is None or not ctx.scrubbed_text.strip():
            return {"error": "No context available for PR draft"}

        user_msg = (
            f"Branch: {state['branch']} → {state['base_branch']}\n\n"
            f"Commits:\n{state['commits_summary'][:1500]}\n\n"
            f"Files changed ({len(state['files_changed'])}): "
            f"{', '.join(state['files_changed'][:15])}\n\n"
            f"Session context (redacted):\n{ctx.scrubbed_text[:2500]}"
        )
        try:
            output = await self._llm.complete(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=600,
            )
        except Exception as exc:
            return {"error": f"LLM call failed: {exc}"}

        parsed = _parse_llm_json(output)
        draft = PRDraft(
            branch=state["branch"],
            base=state["base_branch"],
            title=parsed.get("title", f"{state['branch']} → {state['base_branch']}"),
            summary=parsed.get("summary", output[:400]),
            rationale=parsed.get("rationale", ""),
            test_notes=parsed.get("test_notes", ""),
            risk_notes=parsed.get("risk_notes", ""),
        )
        return {"llm_output": output, "draft": draft}

    # ── Graph wiring ───────────────────────────────────────────────────────────

    def _build(self) -> Any:
        g: StateGraph = StateGraph(PRState)
        g.add_node("collect_history", self._collect_history)
        g.add_node("scrub", self._scrub)
        g.add_node("draft_description", self._draft_description)
        g.add_edge(START, "collect_history")
        g.add_edge("collect_history", "scrub")
        g.add_edge("scrub", "draft_description")
        g.add_edge("draft_description", END)
        return g.compile()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_branch_history(repo_path: Path, base_branch: str) -> dict:
    def _git(*args) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=repo_path, text=True, timeout=15, stderr=subprocess.DEVNULL
        ).strip()

    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")

        # Find merge base — handles both when base exists locally or as origin/base
        try:
            merge_base = _git("merge-base", branch, base_branch)
        except subprocess.CalledProcessError:
            try:
                merge_base = _git("merge-base", branch, f"origin/{base_branch}")
                base_branch = f"origin/{base_branch}"
            except subprocess.CalledProcessError:
                merge_base = None

        if merge_base:
            commits_summary = _git(
                "log", f"{merge_base}..HEAD", "--oneline", "--no-merges"
            )
            diff_stat = _git("diff", "--stat", merge_base, "HEAD")
            files_raw = _git("diff", "--name-only", merge_base, "HEAD")
        else:
            commits_summary = _git("log", "-10", "--oneline")
            diff_stat = _git("show", "--stat", "--format=", "HEAD")
            files_raw = _git("show", "--name-only", "--format=", "HEAD")

        files_changed = [f for f in files_raw.splitlines() if f.strip()]
        return {
            "branch": branch,
            "commits_summary": commits_summary[:2000],
            "diff_stat": diff_stat[:2000],
            "files_changed": files_changed,
        }
    except Exception as exc:
        logger.warning("git branch history failed in %s: %s", repo_path, exc)
        return {
            "branch": "unknown",
            "commits_summary": "",
            "diff_stat": "",
            "files_changed": [],
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
        return {"title": "", "summary": text, "rationale": "", "test_notes": "", "risk_notes": ""}
