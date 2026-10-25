from __future__ import annotations

import re
from datetime import datetime

from vantage.schemas import ActivitySignals, FrictionReport, IntentBeacon, Turn

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


_ERROR_RE = re.compile(
    r"\b("
    r"error|traceback|exception|failed|fatal|segfault|panic|"
    r"cannot|unable|not found|undefined|unresolved|refused|"
    r"econnrefused|enotfound|timeout|timed out"
    r")\b", re.IGNORECASE,
)

_FAILED_RESULT_HINTS = frozenset({"error", "failed", "false", "non-zero"})


def _is_failure_result(content: str) -> bool:
    low = content.lower()
    if "exit code" in low and ("non-zero" in low or re.search(r"exit code\s*[1-9]", low)):
        return True
    if "command failed" in low or "command errored" in low:
        return True
    return False


def compute_friction(
    turns: list[Turn],
    developer_id: str,
    minutes_since_commit: int = 0,
) -> FrictionReport:
    """Score how hard the dev's coding agent is struggling.

    Pure heuristic — no LLM calls. Looks at:
      - failed tool-call indicators (exit codes, "command failed")
      - repeated error strings in tool output
      - turn count without an edit (reading-in-circles)
      - time since the last commit
    """
    edit_count = failed_calls = repeated_errors = 0
    seen_error_signatures: dict[str, int] = {}
    files: list[str] = []

    for t in turns:
        tool = (t.tool_name or "")
        content = t.content_excerpt or ""
        if tool in _EDIT_TOOLS:
            edit_count += 1
            fp = _extract_path(content)
            if fp:
                files.append(fp)
        if tool in _BASH_TOOLS and _is_failure_result(content):
            failed_calls += 1
        for m in _ERROR_RE.finditer(content[:800]):
            sig = m.group(1).lower()
            seen_error_signatures[sig] = seen_error_signatures.get(sig, 0) + 1

    for count in seen_error_signatures.values():
        if count >= 2:
            repeated_errors += count - 1

    turns_scanned = len(turns)
    # Composite score — clipped to [0, 1]. Weights tuned for responsiveness.
    score = 0.0
    if turns_scanned:
        score += min(0.5, failed_calls / max(5, turns_scanned))
        score += min(0.3, repeated_errors / max(10, turns_scanned))
        if edit_count == 0 and turns_scanned >= 8:
            score += 0.15  # lots of activity, zero edits = circling
    if minutes_since_commit >= 60:
        score += min(0.2, (minutes_since_commit - 60) / 300)
    score = max(0.0, min(1.0, score))

    hot = _dedupe_basenames(files)[:5]
    summary = _friction_summary(score, failed_calls, repeated_errors, minutes_since_commit, hot)

    return FrictionReport(
        developer_id=developer_id,
        score=round(score, 3),
        turns_scanned=turns_scanned,
        failed_tool_calls=failed_calls,
        repeated_errors=repeated_errors,
        minutes_since_commit=minutes_since_commit,
        hot_files=hot,
        summary=summary,
    )


def _friction_summary(score: float, failed: int, repeated: int, mins: int, hot: list[str]) -> str:
    if score < 0.15:
        return "flow"
    label = "high friction" if score >= 0.6 else "elevated friction"
    parts = [label]
    if failed:
        parts.append(f"{failed} failed tool call{'s' if failed != 1 else ''}")
    if repeated:
        parts.append(f"{repeated} repeated error{'s' if repeated != 1 else ''}")
    if mins >= 60:
        parts.append(f"{mins}m since last commit")
    if hot:
        parts.append("hot: " + ", ".join(hot[:3]))
    return " · ".join(parts)


def build_beacon(
    signals: ActivitySignals,
    developer_id: str,
    turn_count: int = 0,
) -> IntentBeacon:
    """Package the current activity snapshot into a lightweight collision beacon."""
    return IntentBeacon(
        developer_id=developer_id,
        repo=signals.active_repo,
        branch=signals.inferred_branch,
        files_touched=signals.files_recently_touched[:8],
        intent_hint=(signals.last_user_intent or "")[:140],
        turn_count=turn_count,
    )
