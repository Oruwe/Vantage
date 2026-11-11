"""Rolling weekly stats for `vantage-ghost stats`.

Pulls draft counts + status transitions out of the local SQLite store and
scrubber hits out of the `scrubber_hits` table. Returns a dataclass the CLI
renders as markdown or JSON. Never reads or returns artifact content — only
counts, statuses, timestamps, and the `edited_payload_json IS NOT NULL` flag.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class WindowStats:
    week_start: str
    week_end: str
    drafts_created: int
    approved: int
    rejected: int
    approved_with_edit: int

    @property
    def edit_rate(self) -> float | None:
        return (self.approved_with_edit / self.approved) if self.approved else None

    @property
    def approval_rate(self) -> float | None:
        reviewed = self.approved + self.rejected
        return (self.approved / reviewed) if reviewed else None

    def to_dict(self) -> dict:
        return {
            "week_start": self.week_start,
            "week_end": self.week_end,
            "drafts_created": self.drafts_created,
            "approved": self.approved,
            "rejected": self.rejected,
            "approved_with_edit": self.approved_with_edit,
            "edit_rate": self.edit_rate,
            "approval_rate": self.approval_rate,
        }


@dataclass
class StatsReport:
    generated_at: str
    weeks: int
    windows: list[WindowStats]
    pending: int
    totals: dict[str, int]
    scrubber_hits: dict[str, int] = field(default_factory=dict)
    scrubber_total: int = 0

    def to_dict(self) -> dict:
        approved = self.totals.get("approved", 0)
        rejected = self.totals.get("rejected", 0)
        with_edit = self.totals.get("approved_with_edit", 0)
        reviewed = approved + rejected
        return {
            "generated_at": self.generated_at,
            "weeks": self.weeks,
            "pending": self.pending,
            "totals": {
                "drafts_created": self.totals.get("drafts_created", 0),
                "approved": approved,
                "rejected": rejected,
                "approved_with_edit": with_edit,
                "approval_rate": (approved / reviewed) if reviewed else None,
                "edit_rate": (with_edit / approved) if approved else None,
            },
            "scrubber_hits": {
                "total": self.scrubber_total,
                "by_kind": dict(self.scrubber_hits),
            },
            "windows": [w.to_dict() for w in self.windows],
        }


def _week_buckets(now: datetime, weeks: int) -> list[tuple[datetime, datetime]]:
    buckets: list[tuple[datetime, datetime]] = []
    for i in range(weeks, 0, -1):
        end = now - timedelta(days=(i - 1) * 7)
        start = end - timedelta(days=7)
        buckets.append((start, end))
    return buckets


def _query_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> WindowStats:
    s, e = start.isoformat(), end.isoformat()
    created = conn.execute(
        "SELECT COUNT(*) FROM digest_drafts WHERE created_at >= ? AND created_at < ?",
        (s, e),
    ).fetchone()[0]
    approved = conn.execute(
        "SELECT COUNT(*) FROM digest_drafts "
        "WHERE status='approved' AND reviewed_at >= ? AND reviewed_at < ?",
        (s, e),
    ).fetchone()[0]
    rejected = conn.execute(
        "SELECT COUNT(*) FROM digest_drafts "
        "WHERE status='rejected' AND reviewed_at >= ? AND reviewed_at < ?",
        (s, e),
    ).fetchone()[0]
    with_edit = conn.execute(
        "SELECT COUNT(*) FROM digest_drafts "
        "WHERE status='approved' AND edited_payload_json IS NOT NULL "
        "AND reviewed_at >= ? AND reviewed_at < ?",
        (s, e),
    ).fetchone()[0]
    return WindowStats(
        week_start=start.date().isoformat(),
        week_end=end.date().isoformat(),
        drafts_created=created,
        approved=approved,
        rejected=rejected,
        approved_with_edit=with_edit,
    )


def _empty_window(start: datetime, end: datetime) -> WindowStats:
    return WindowStats(
        week_start=start.date().isoformat(),
        week_end=end.date().isoformat(),
        drafts_created=0,
        approved=0,
        rejected=0,
        approved_with_edit=0,
    )


def compute_report(*, sqlite_path: Path, weeks: int) -> StatsReport:
    weeks = max(1, int(weeks))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    buckets = _week_buckets(now, weeks)

    windows: list[WindowStats] = []
    pending = 0

    if sqlite_path.exists():
        conn = sqlite3.connect(sqlite_path, timeout=5.0)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "digest_drafts" in tables:
                for start, end in buckets:
                    windows.append(_query_window(conn, start, end))
                pending = conn.execute(
                    "SELECT COUNT(*) FROM digest_drafts WHERE status='pending'"
                ).fetchone()[0]
            else:
                windows = [_empty_window(s, e) for s, e in buckets]
        finally:
            conn.close()
    else:
        windows = [_empty_window(s, e) for s, e in buckets]

    totals = {
        "drafts_created": sum(w.drafts_created for w in windows),
        "approved": sum(w.approved for w in windows),
        "rejected": sum(w.rejected for w in windows),
        "approved_with_edit": sum(w.approved_with_edit for w in windows),
    }

    scrubber_hits: dict[str, int] = {}
    scrubber_total = 0
    if sqlite_path.exists():
        from ghost_daemon.insights.scrubber_hits import ScrubberHitStore

        hits_store = ScrubberHitStore(sqlite_path)
        since = buckets[0][0].replace(tzinfo=timezone.utc)
        totals_obj = hits_store.totals_since(since)
        scrubber_hits = dict(totals_obj.by_kind)
        scrubber_total = totals_obj.total

    return StatsReport(
        generated_at=now.isoformat(),
        weeks=weeks,
        windows=windows,
        pending=pending,
        totals=totals,
        scrubber_hits=scrubber_hits,
        scrubber_total=scrubber_total,
    )


def render_markdown(report: StatsReport) -> str:
    t = report.totals
    approved = t.get("approved", 0)
    rejected = t.get("rejected", 0)
    with_edit = t.get("approved_with_edit", 0)
    reviewed = approved + rejected

    lines: list[str] = []
    lines.append(f"# Vantage stats - last {report.weeks} week(s)")
    lines.append("")
    lines.append(f"_Generated {report.generated_at}Z - all counts are local, no content is shown._")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Drafts created: **{t.get('drafts_created', 0)}**")
    lines.append(f"- Approved: **{approved}**")
    lines.append(f"- Rejected: **{rejected}**")
    lines.append(f"- Pending right now: **{report.pending}**")
    if reviewed:
        lines.append(
            f"- Approval rate: **{approved / reviewed:.0%}**  ({approved}/{reviewed} reviewed)"
        )
    else:
        lines.append("- Approval rate: _no reviews in window_")
    if approved:
        lines.append(
            f"- Edit rate: **{with_edit / approved:.0%}**  "
            f"({with_edit}/{approved} approved drafts were edited before sending)"
        )
    else:
        lines.append("- Edit rate: _no approvals in window_")
    lines.append("")

    lines.append("## Weekly trend")
    lines.append("")
    lines.append("| Week start | Created | Approved | Rejected | Edited | Edit rate |")
    lines.append("|------------|---------|----------|----------|--------|-----------|")
    for w in report.windows:
        rate = f"{w.edit_rate:.0%}" if w.edit_rate is not None else "-"
        lines.append(
            f"| {w.week_start} | {w.drafts_created} | {w.approved} | "
            f"{w.rejected} | {w.approved_with_edit} | {rate} |"
        )
    lines.append("")

    lines.append("## Scrubber hits")
    lines.append("")
    if report.scrubber_total == 0:
        lines.append("_No redactions recorded in this window._")
    else:
        lines.append(f"**{report.scrubber_total}** total redactions. By kind:")
        lines.append("")
        for kind, count in sorted(report.scrubber_hits.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"- `{kind}`: {count}")
    lines.append("")
    return "\n".join(lines)


def render_json(report: StatsReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
