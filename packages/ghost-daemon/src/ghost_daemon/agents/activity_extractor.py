from __future__ import annotations

import re
from datetime import datetime

from vantage.schemas import ActivitySignals, Turn

_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "List"})
_BASH_TOOLS = frozenset({"Bash", "Execute", "Shell", "RunCode", "Computer"})

_BRANCH_PATTERNS = [
    re.compile(r"\* (\S+)"),
    re.compile(r"On branch (\S+)"),
    re.compile(r"git checkout (?:-b )?(\S+)"),
    re.compile(r"Switched to(?: a new)? branch ['\"]?([^'\" \t\n\r]+)"),
    re.compile(r"HEAD detached at (\S+)"),
]
# Match "file_path": "..." or file_path = ... style in tool args
_FILE_PATH_RE = re.compile(
    r'"(?:file_path|path|filename|target|source)"\s*:\s*"([^"]+)"'
    r"|'(?:file_path|path|filename|target|source)'\s*:\s*'([^']+)'"
)
# Fallback: only absolute paths (conservative to avoid matching "1.2.3", "v0.1.0", etc.)
_BARE_PATH_RE = re.compile(r'(?:^|[\s,"\'(])(/[\w./\\:@\-]+\.\w{2,6})')

_BRANCH_IGNORE = frozenset({"HEAD", "branch", "--", "a", "the", "it", "to", "be"})

# Directory names that are unlikely to be the repo root
_NON_ROOT_DIRS = frozenset({
    "src", "lib", "libs", "tests", "test", "spec", "specs",
    "dist", "build", "out", "bin", "obj", "target",
    "node_modules", "vendor", ".git", "__pycache__",
    "site-packages", "packages",
})


def extract_activity_signals(turns: list[Turn], cwd: str = "") -> ActivitySignals:
    """Deterministically extract structured activity signals from session turns.

    Zero LLM tokens — pure regex/heuristic over Turn objects.
    """
    files: list[str] = []
    branch = ""
    edit_count = read_count = bash_count = 0
    last_user_intent = ""
    oldest_ts: datetime | None = None

    for t in turns:
        if t.timestamp and (oldest_ts is None or t.timestamp < oldest_ts):
            oldest_ts = t.timestamp

        if t.role == "user":
            raw = t.content_excerpt
            if raw.startswith("[user] "):
                raw = raw[7:]
            candidate = raw.strip()[:150]
            if candidate:
                last_user_intent = candidate

        tool = (t.tool_name or "").strip()
        content = t.content_excerpt

        if tool in _EDIT_TOOLS:
            edit_count += 1
            fp = _extract_path(content)
            if fp:
                files.append(fp)
        elif tool in _READ_TOOLS:
            read_count += 1
            fp = _extract_path(content)
            if fp:
                files.append(fp)
        elif tool in _BASH_TOOLS or "bash" in tool.lower() or "execute" in tool.lower():
            bash_count += 1
            if not branch:
                branch = _extract_branch(content)

    unique_files = _dedupe_basenames(files)
    active_repo = _infer_repo(files, cwd)
    primary_activity = _classify(edit_count, read_count, bash_count)

    return ActivitySignals(
        files_recently_touched=unique_files[:8],
        inferred_branch=branch,
        active_repo=active_repo,
        last_user_intent=last_user_intent,
        primary_activity=primary_activity,
        session_active_since=oldest_ts,
    )


def _extract_path(content: str) -> str | None:
    m = _FILE_PATH_RE.search(content)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    m = _BARE_PATH_RE.search(content)
    if m:
        return m.group(1).strip(" \"',")
    return None


def _extract_branch(content: str) -> str:
    for pat in _BRANCH_PATTERNS:
        m = pat.search(content)
        if m:
            candidate = m.group(1).rstrip(".,;:\"'")
            if candidate and candidate not in _BRANCH_IGNORE and len(candidate) < 80:
                return candidate
    return ""


def _dedupe_basenames(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        base = p.replace("\\", "/").rsplit("/", 1)[-1]
        if base and base not in seen:
            seen.add(base)
            result.append(base)
    return result


def _infer_repo(paths: list[str], cwd: str) -> str:
    for p in paths:
        parts = p.replace("\\", "/").split("/")
        # Walk directory components from deepest upward, skipping non-root dirs
        for i in range(len(parts) - 1, 0, -1):
            candidate = parts[i - 1]
            if candidate and not candidate.startswith(".") and candidate not in _NON_ROOT_DIRS:
                return candidate
    # Fall back to deepest non-standard component of cwd
    if cwd:
        for part in reversed(cwd.replace("\\", "/").rstrip("/").split("/")):
            if part and not part.startswith(".") and part not in _NON_ROOT_DIRS:
                return part
    return ""


def _classify(edit: int, read: int, bash: int) -> str:
    total = edit + read + bash
    if total == 0:
        return "unknown"
    if edit / total >= 0.35:
        return "editing"
    if bash / total >= 0.45:
        return "debugging"
    if read / total >= 0.50:
        return "reviewing"
    return "browsing"
