"""Scrubber hit counter — turns the redactor into a visible dev-facing surface.

Every time ``scrubber.scrub()`` runs on something and strips a secret, we
increment a per-kind counter in a local SQLite table. The /privacy page
reads from here so the dev can see "this week your scrubber caught 3 AWS
keys and 11 high-entropy strings."

Design notes:
  - All writes go to the same ``vantage.db`` the SQLiteStore already owns,
    on a new table. No new file to manage.
  - Writes are best-effort: a broken hit-write must never block an LLM call.
  - Also tracks ``overrides`` — when the dev decides a redaction was a false
    positive, we record the pattern so the eval dataset grows for free.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vantage.schemas import RedactedContext

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS scrubber_hits (
    kind         TEXT    NOT NULL,
    occurred_at  TEXT    NOT NULL,
    file_path    TEXT,
    PRIMARY KEY (kind, occurred_at, file_path)
);
CREATE INDEX IF NOT EXISTS idx_scrubber_hits_at ON scrubber_hits(occurred_at);

CREATE TABLE IF NOT EXISTS scrubber_overrides (
    pattern      TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    overridden_at TEXT   NOT NULL,
    PRIMARY KEY (pattern, kind)
);
"""


@dataclass
class HitTotals:
    """Aggregated counts for the UI."""
    by_kind: dict[str, int]
    total: int
    window_start: str
    window_end: str


class ScrubberHitStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CREATE_SQL)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_context(self, ctx: RedactedContext) -> None:
        """Log every redaction in a RedactedContext. Best-effort."""
        if not ctx.redactions:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = [(r.kind, now, ctx.file_path or "") for r in ctx.redactions]
            with self._lock, self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO scrubber_hits (kind, occurred_at, file_path) VALUES (?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            logger.debug("ScrubberHitStore.record_context failed: %s", exc)

    def record_override(self, pattern: str, kind: str) -> None:
        """Remember when a dev marked a redaction as a false positive."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO scrubber_overrides (pattern, kind, overridden_at) VALUES (?, ?, ?)",
                    (pattern, kind, now),
                )
        except sqlite3.Error as exc:
            logger.debug("ScrubberHitStore.record_override failed: %s", exc)

    def totals_since(self, since: datetime) -> HitTotals:
        """Count hits grouped by kind since ``since`` (inclusive)."""
        since_iso = since.astimezone(timezone.utc).isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT kind, COUNT(*) FROM scrubber_hits WHERE occurred_at >= ? GROUP BY kind",
                    (since_iso,),
                )
                rows = cur.fetchall()
        except sqlite3.Error as exc:
            logger.debug("ScrubberHitStore.totals_since failed: %s", exc)
            rows = []
        by_kind = {kind: n for kind, n in rows}
        return HitTotals(
            by_kind=by_kind,
            total=sum(by_kind.values()),
            window_start=since_iso,
            window_end=now_iso,
        )

    def weekly_totals(self) -> HitTotals:
        return self.totals_since(datetime.now(timezone.utc) - timedelta(days=7))

    def recent_overrides(self, limit: int = 20) -> list[tuple[str, str, str]]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT pattern, kind, overridden_at FROM scrubber_overrides "
                    "ORDER BY overridden_at DESC LIMIT ?",
                    (limit,),
                )
                return list(cur.fetchall())
        except sqlite3.Error as exc:
            logger.debug("ScrubberHitStore.recent_overrides failed: %s", exc)
            return []
