"""Deja-Vu detector: finds struggle-then-success patterns in a session.

A struggle-then-success pattern looks like:

    turn N:   Bash — fails ("error: cannot find module 'foo'")
    turn N+1: Bash — fails ("error: cannot find module 'foo'")
    turn N+2: Bash — fails ("error: cannot find module 'foo'")
    turn N+3: Edit — modifies package.json
    turn N+4: Bash — succeeds ("installed foo@1.2.3")

The detector normalizes the failing error signature (strips paths, line
numbers, PIDs, uuids) so the same error wearing different hats collapses into
one key. The resolving turn is captured as the "fix". The result is a
`BugResolution` suitable for pushing to shadow-replica for team-wide reuse.

Pure deterministic — no LLM calls. If a session LLM is available later, it
could post-process `fix_summary`.
"""
from __future__ import annotations

import re
from pathlib import Path

from vantage.schemas import BugResolution, Turn

from ghost_daemon.redaction.scrubber import scrub

_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
_BASH_TOOLS = frozenset({"Bash", "Execute", "Shell", "RunCode"})

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[/\\][\w./\\\-]+")
_LINENO_RE = re.compile(r"\bline\s+\d+\b", re.IGNORECASE)
_LINENO_RE2 = re.compile(r":\d+(?::\d+)?\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_PID_RE = re.compile(r"\b(?:pid|PID)[=:\s]+\d+")
_PORT_RE = re.compile(r":\d{4,5}\b")
_WS_RE = re.compile(r"\s+")

# What counts as an "error" signal in a tool-result turn
_FAIL_MARKERS = (
    "error", "failed", "traceback", "exception", "cannot", "not found",
    "undefined", "exit code", "panic", "fatal",
)

_MIN_ATTEMPTS = 2        # need at least 2 failing attempts to qualify
_MAX_LOOKBACK = 60       # scan at most 60 recent turns


def _is_failure(content: str) -> bool:
    low = content.lower()
    if "exit code" in low:
        m = re.search(r"exit code[:\s]*(\d+)", low)
        if m and m.group(1) != "0":
            return True
    return any(mk in low for mk in _FAIL_MARKERS)


def _is_success(tool: str, content: str) -> bool:
    """A turn counts as a success if it edits a file, or a bash call runs cleanly."""
    if tool in _EDIT_TOOLS:
        return True
    if tool in _BASH_TOOLS:
        return not _is_failure(content)
    return False


def normalize_error(text: str) -> str:
    """Produce a stable key for grouping similar error messages."""
    if not text:
        return ""
    t = text[:600]
    t = _UUID_RE.sub("<uuid>", t)
    t = _HEX_RE.sub("<hex>", t)
    t = _PATH_RE.sub("<path>", t)
    t = _LINENO_RE.sub("line <n>", t)
    t = _LINENO_RE2.sub(":<n>", t)
    t = _PID_RE.sub("pid=<n>", t)
    t = _PORT_RE.sub(":<port>", t)
    t = _WS_RE.sub(" ", t).strip().lower()
    # Grab the first useful error-ish sentence
    for sep in ("\n", ". "):
        if sep in t:
            first = t.split(sep, 1)[0]
            if any(mk in first for mk in _FAIL_MARKERS):
                return first[:200]
    return t[:200]


def _extract_file_path(content: str) -> str | None:
    m = re.search(r'"(?:file_path|path|filename)"\s*:\s*"([^"]+)"', content)
    if m:
        return m.group(1)
    m = re.search(r"'(?:file_path|path|filename)'\s*:\s*'([^']+)'", content)
    if m:
        return m.group(1)
    return None


def detect_resolutions(
    turns: list[Turn],
    developer_id: str,
    repo: str = "",
) -> list[BugResolution]:
    """Scan recent turns for struggle-then-success patterns.

    Walks forward from oldest to newest, keeping a rolling bucket of similar
    failing turns. When a success is observed and the bucket meets the
    minimum-attempts threshold, emits a BugResolution and clears the bucket.
    """
    if not turns:
        return []
    turns = turns[-_MAX_LOOKBACK:]
    resolutions: list[BugResolution] = []

    # Bucket keyed by normalized error signature → list[Turn]
    buckets: dict[str, list[Turn]] = {}

    for t in turns:
        tool = (t.tool_name or "")
        content = t.content_excerpt or ""
        if tool in _BASH_TOOLS and _is_failure(content):
            sig = normalize_error(content)
            if not sig:
                continue
            buckets.setdefault(sig, []).append(t)
            continue
        if _is_success(tool, content):
            if not buckets:
                continue
            # Match this success to the largest open bucket (most-struggled error).
            best_sig, best_turns = max(buckets.items(), key=lambda kv: len(kv[1]))
            if len(best_turns) < _MIN_ATTEMPTS:
                continue
            # Gather files touched in the resolving turn + prior failing turns
            files: list[str] = []
            tools_used: list[str] = []
            for src in best_turns + [t]:
                fp = _extract_file_path(src.content_excerpt or "")
                if fp and fp not in files:
                    files.append(fp)
                tool_name = (src.tool_name or "").strip()
                if tool_name and tool_name not in tools_used:
                    tools_used.append(tool_name)

            sample_fail = scrub(best_turns[0].content_excerpt or "").scrubbed_text[:400]
            fix_excerpt = scrub(t.content_excerpt or "").scrubbed_text[:400]
            summary = _fix_summary(t, files, fix_excerpt)
            resolutions.append(BugResolution(
                developer_id=developer_id,
                error_signature=best_sig,
                error_sample=sample_fail,
                attempt_count=len(best_turns),
                fix_summary=summary,
                fix_details=fix_excerpt,
                files_touched=[_basename(f) for f in files[:5]],
                tools_used=tools_used[:5],
                repo=repo,
            ))
            # The resolved error is now closed; drop that bucket but keep others
            buckets.pop(best_sig, None)
    return resolutions


def _basename(p: str) -> str:
    return Path(p.replace("\\", "/")).name or p


def _fix_summary(resolver: Turn, files: list[str], fix_excerpt: str) -> str:
    tool = (resolver.tool_name or "").strip() or "fix"
    if files:
        base = _basename(files[0])
        return f"{tool} on {base}"
    first_line = fix_excerpt.strip().split("\n", 1)[0][:120]
    return f"{tool}: {first_line}" if first_line else tool
