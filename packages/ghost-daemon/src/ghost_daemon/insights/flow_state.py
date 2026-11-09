"""Flow-state analyzer — the dev's rear-view mirror on their own week.

Reads timestamps from:
  - session JSONL files (Claude Code / Codex turn timestamps)
  - the SQLite approval ledger (approval + review times)

Produces three views:
  - hourly heatmap:   7 days × 24 hours of session density
  - deep-work windows: runs of sustained activity with no large gaps
  - approval timeline: recent approvals + rejects

Everything is computed locally on demand. No network, no LLM, no manager
visibility. The dev opens /flow and sees their own week more clearly than
the manager ever can — that asymmetry is on purpose.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Data types for the UI ────────────────────────────────────────────────────


@dataclass
class HourCell:
    day_offset: int   # 0 = today, 1 = yesterday, ...
    hour: int         # 0–23 local
    turn_count: int


@dataclass
class DeepWindow:
    start: datetime
    end: datetime
    turn_count: int

    @property
    def duration_minutes(self) -> int:
        return max(1, int((self.end - self.start).total_seconds() // 60))


@dataclass
class ApprovalEvent:
    draft_id: str
    artifact_type: str
    reviewed_at: datetime
    status: str       # "approved" | "rejected"


@dataclass
class StuckPattern:
    """A struggle→success window detected by the Dejavu scanner.

    Dejavu writes these as ``BugResolution`` drafts; we surface them on /flow
    so the dev can see "here's what tripped me up this week."
    """
    resolution_id: str
    error_signature: str
    fix_summary: str
    attempt_count: int
    files_touched: list[str]
    created_at: datetime


@dataclass
class FlowSnapshot:
    days: int
    heatmap: list[HourCell] = field(default_factory=list)
    deep_windows: list[DeepWindow] = field(default_factory=list)
    recent_approvals: list[ApprovalEvent] = field(default_factory=list)
    stuck_patterns: list[StuckPattern] = field(default_factory=list)
    total_turns: int = 0
    active_hours: int = 0  # distinct day×hour buckets with any activity
    total_stuck_attempts: int = 0  # sum of attempt_counts in window


# ── Public analyzer ──────────────────────────────────────────────────────────


class FlowAnalyzer:
    """Compute a flow snapshot from local sources on demand."""

    def __init__(
        self,
        *,
        session_dirs: list[Path],
        sqlite_path: Path | None = None,
    ) -> None:
        self._session_dirs = [Path(p) for p in session_dirs if Path(p).exists()]
        self._sqlite_path = sqlite_path

    def snapshot(
        self,
        *,
        days: int = 7,
        min_window_minutes: int = 25,
        max_gap_minutes: int = 10,
        now: datetime | None = None,
    ) -> FlowSnapshot:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = now - timedelta(days=days)

        timestamps = sorted(self._collect_turn_timestamps(cutoff))

        heatmap = _build_heatmap(timestamps, now=now, days=days)
        deep = _find_deep_windows(
            timestamps,
            min_minutes=min_window_minutes,
            max_gap_minutes=max_gap_minutes,
        )
        approvals = self._collect_approvals(cutoff)
        stuck = self._collect_stuck_patterns(cutoff)

        return FlowSnapshot(
            days=days,
            heatmap=heatmap,
            deep_windows=deep,
            recent_approvals=approvals,
            stuck_patterns=stuck,
            total_turns=len(timestamps),
            active_hours=sum(1 for c in heatmap if c.turn_count > 0),
            total_stuck_attempts=sum(p.attempt_count for p in stuck),
        )

    # ── Data collection ───────────────────────────────────────────────────

    def _collect_turn_timestamps(self, cutoff: datetime) -> list[datetime]:
        """Scan every session JSONL in each session_dir, yield timestamps
        after ``cutoff``. Silent on individual file failures — a corrupt
        session must never block the /flow view."""
        out: list[datetime] = []
        for session_dir in self._session_dirs:
            try:
                files = list(session_dir.rglob("*.jsonl"))
            except OSError:
                continue
            for p in files:
                try:
                    if p.stat().st_mtime < cutoff.timestamp():
                        continue
                except OSError:
                    continue
                out.extend(_read_timestamps_from_jsonl(p, cutoff))
        return out

    def _collect_stuck_patterns(self, cutoff: datetime) -> list[StuckPattern]:
        """Pull bug_resolution drafts from the local approval ledger.

        Returns newest-first, capped at 20. A corrupt payload is skipped rather
        than killing the whole /flow page — defensive by design because this
        row joins a user-generated JSON blob to a UI render path.
        """
        if self._sqlite_path is None or not self._sqlite_path.exists():
            return []
        import sqlite3
        cutoff_iso = cutoff.isoformat()
        patterns: list[StuckPattern] = []
        try:
            conn = sqlite3.connect(self._sqlite_path, timeout=2.0)
            cur = conn.execute(
                "SELECT id, payload_json, edited_payload_json, created_at "
                "FROM digest_drafts "
                "WHERE artifact_type = 'bug_resolution' AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 20",
                (cutoff_iso,),
            )
            for draft_id, payload_json, edited_json, created_at in cur.fetchall():
                raw = edited_json or payload_json or ""
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                try:
                    ts = datetime.fromisoformat(created_at)
                except ValueError:
                    continue
                patterns.append(StuckPattern(
                    resolution_id=str(data.get("resolution_id") or draft_id),
                    error_signature=str(data.get("error_signature") or "")[:160],
                    fix_summary=str(data.get("fix_summary") or "")[:280],
                    attempt_count=int(data.get("attempt_count") or 0),
                    files_touched=list(data.get("files_touched") or [])[:8],
                    created_at=ts,
                ))
            conn.close()
        except sqlite3.Error as exc:
            logger.debug("FlowAnalyzer: stuck-pattern query failed: %s", exc)
        return patterns

    def _collect_approvals(self, cutoff: datetime) -> list[ApprovalEvent]:
        if self._sqlite_path is None or not self._sqlite_path.exists():
            return []
        import sqlite3
        cutoff_iso = cutoff.isoformat()
        events: list[ApprovalEvent] = []
        try:
            conn = sqlite3.connect(self._sqlite_path, timeout=2.0)
            cur = conn.execute(
                "SELECT id, artifact_type, reviewed_at, status "
                "FROM digest_drafts "
                "WHERE reviewed_at IS NOT NULL AND reviewed_at >= ? "
                "ORDER BY reviewed_at DESC LIMIT 100",
                (cutoff_iso,),
            )
            for draft_id, artifact_type, reviewed_at, status in cur.fetchall():
                try:
                    ts = datetime.fromisoformat(reviewed_at)
                except ValueError:
                    continue
                events.append(ApprovalEvent(
                    draft_id=draft_id,
                    artifact_type=artifact_type,
                    reviewed_at=ts,
                    status=status,
                ))
            conn.close()
        except sqlite3.Error as exc:
            logger.debug("FlowAnalyzer: approval query failed: %s", exc)
        return events


# ── Private helpers ──────────────────────────────────────────────────────────


def _read_timestamps_from_jsonl(path: Path, cutoff: datetime) -> Iterable[datetime]:
    """Yield just the timestamp from each turn line, filtered by cutoff.

    We only parse the minimum JSON we need — no content, no role check —
    because /flow may touch hundreds of files.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw = data.get("timestamp") or data.get("ts") or data.get("created_at")
                if not isinstance(raw, str):
                    continue
                try:
                    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                else:
                    ts = ts.astimezone(timezone.utc)
                if ts >= cutoff:
                    yield ts
    except OSError:
        return


def _build_heatmap(
    timestamps: list[datetime], *, now: datetime, days: int,
) -> list[HourCell]:
    """Return a ``days * 24`` grid of HourCells; zero-filled where no activity."""
    counts: dict[tuple[int, int], int] = {}
    for ts in timestamps:
        day_offset = (now.date() - ts.date()).days
        if 0 <= day_offset < days:
            key = (day_offset, ts.hour)
            counts[key] = counts.get(key, 0) + 1

    cells: list[HourCell] = []
    for d in range(days):
        for h in range(24):
            cells.append(HourCell(
                day_offset=d,
                hour=h,
                turn_count=counts.get((d, h), 0),
            ))
    return cells


def _find_deep_windows(
    timestamps: list[datetime],
    *,
    min_minutes: int,
    max_gap_minutes: int,
) -> list[DeepWindow]:
    """Group timestamps into continuous windows — new window when the gap to
    the previous timestamp exceeds ``max_gap_minutes``. Keep only windows
    that span at least ``min_minutes``.

    Returns newest-first, limited to the top 10 by duration.
    """
    if not timestamps:
        return []

    ordered = sorted(timestamps)
    max_gap = timedelta(minutes=max_gap_minutes)
    min_span = timedelta(minutes=min_minutes)

    windows: list[DeepWindow] = []
    win_start = ordered[0]
    win_end = ordered[0]
    count = 1

    for ts in ordered[1:]:
        if ts - win_end > max_gap:
            if win_end - win_start >= min_span:
                windows.append(DeepWindow(start=win_start, end=win_end, turn_count=count))
            win_start = ts
            count = 1
        else:
            count += 1
        win_end = ts

    if win_end - win_start >= min_span:
        windows.append(DeepWindow(start=win_start, end=win_end, turn_count=count))

    # Newest first; cap at 10.
    windows.sort(key=lambda w: w.start, reverse=True)
    return windows[:10]
