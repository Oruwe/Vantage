"""Edit-diff capture: the dev's local self-improvement dataset.

When the dev approves a draft *after editing it*, we record:
  - what the agent proposed
  - what the dev actually approved
  - which fields changed

Diffs are appended to monthly JSONL shards under ``~/.vantage/edits/``.
Nothing ever goes to the shadow-replica. This is purely a local signal for
the dev (and, later, for local prompt tuning).

Storage layout:
  ~/.vantage/edits/
    2026-11.jsonl       # one line per edit
    2026-10.jsonl
    ...
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EditDiff:
    draft_id: str
    developer_id: str
    artifact_type: str
    approved_at: str                   # ISO8601 UTC
    changed_fields: list[str] = field(default_factory=list)
    original: dict = field(default_factory=dict)
    edited: dict = field(default_factory=dict)
    # Lightweight size heuristics — cheap to aggregate later
    original_chars: int = 0
    edited_chars: int = 0

    def as_jsonl_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


class EditDiffStore:
    """Append-only, per-month JSONL shard. Thread-safe via a module-level lock
    so concurrent approvals from the UI don't interleave bytes."""

    def __init__(self, edits_dir: Path) -> None:
        self._edits_dir = edits_dir
        self._lock = threading.Lock()

    def _current_shard(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(timezone.utc).replace(tzinfo=None)
        return self._edits_dir / f"{when:%Y-%m}.jsonl"

    def record(self, diff: EditDiff) -> Path:
        """Append the diff to the current month shard. Returns the file path.

        Creates the directory on first write. Silent on IO error — a failed
        insight write must never block the approval path.
        """
        shard = self._current_shard()
        try:
            self._edits_dir.mkdir(parents=True, exist_ok=True)
            line = diff.as_jsonl_line() + "\n"
            with self._lock:
                with open(shard, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError as exc:
            logger.debug("EditDiffStore.record failed: %s", exc)
        return shard

    def read_recent(self, limit: int = 50) -> list[EditDiff]:
        """Return the most recent ``limit`` diffs across shards, newest first.

        Reads the current month then falls back to the previous month if more
        are needed. Older shards are archived — load them explicitly via
        :meth:`iter_shard`."""
        results: list[EditDiff] = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for shard in self._shard_candidates(now, max_months_back=2):
            if not shard.exists():
                continue
            for row in reversed(self._read_shard(shard)):
                results.append(row)
                if len(results) >= limit:
                    return results
        return results

    def iter_shard(self, when: datetime) -> list[EditDiff]:
        shard = self._current_shard(when)
        return self._read_shard(shard) if shard.exists() else []

    # ── Introspection helpers ─────────────────────────────────────────────

    def count_in_shard(self, when: datetime | None = None) -> int:
        when = when or datetime.now(timezone.utc).replace(tzinfo=None)
        shard = self._current_shard(when)
        if not shard.exists():
            return 0
        with open(shard, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def edits_by_type_since(
        self, since: datetime, max_months_back: int = 2,
    ) -> dict[str, int]:
        """Count recorded edits per artifact_type since ``since``.

        Looks at up to ``max_months_back`` shards so a 30-day window spanning
        a month boundary is covered. Edits older than ``since`` are skipped.
        """
        counts: dict[str, int] = {}
        for shard in self._shard_candidates(datetime.now(timezone.utc).replace(tzinfo=None), max_months_back):
            if not shard.exists():
                continue
            for row in self._read_shard(shard):
                try:
                    approved = datetime.fromisoformat(row.approved_at)
                except ValueError:
                    continue
                if approved < since:
                    continue
                counts[row.artifact_type] = counts.get(row.artifact_type, 0) + 1
        return counts

    def edits_by_type_in_window(
        self,
        start: datetime,
        end: datetime,
        *,
        max_months_back: int = 3,
    ) -> dict[str, int]:
        """Count edits per artifact_type where ``start <= approved_at < end``.

        Used by the weekly-trend sparkline on /privacy — walk the recent
        shards once per window rather than N+1 queries.
        """
        if end <= start:
            return {}
        counts: dict[str, int] = {}
        for shard in self._shard_candidates(end, max_months_back):
            if not shard.exists():
                continue
            for row in self._read_shard(shard):
                try:
                    approved = datetime.fromisoformat(row.approved_at)
                except ValueError:
                    continue
                if approved < start or approved >= end:
                    continue
                counts[row.artifact_type] = counts.get(row.artifact_type, 0) + 1
        return counts

    # ── Internals ─────────────────────────────────────────────────────────

    def _shard_candidates(self, start: datetime, max_months_back: int) -> list[Path]:
        shards: list[Path] = []
        year, month = start.year, start.month
        for _ in range(max_months_back + 1):
            shards.append(self._edits_dir / f"{year:04d}-{month:02d}.jsonl")
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        return shards

    def _read_shard(self, shard: Path) -> list[EditDiff]:
        out: list[EditDiff] = []
        with open(shard, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    out.append(EditDiff(**data))
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.debug("EditDiffStore.read skipped bad row: %s", exc)
        return out


def compute_diff(
    draft_id: str,
    developer_id: str,
    artifact_type: str,
    original: Any,
    edited: Any,
) -> EditDiff | None:
    """Produce an EditDiff iff the dev actually changed something.

    Both ``original`` and ``edited`` may be Pydantic models, dicts, or JSON
    strings. Returns ``None`` if no fields differ — we don't record no-op
    approvals, only genuine edits.
    """
    orig_d = _to_dict(original)
    edit_d = _to_dict(edited)
    if orig_d == edit_d:
        return None

    changed: list[str] = []
    keys = sorted(set(orig_d) | set(edit_d))
    for k in keys:
        if orig_d.get(k) != edit_d.get(k):
            changed.append(k)

    return EditDiff(
        draft_id=draft_id,
        developer_id=developer_id,
        artifact_type=artifact_type,
        approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        changed_fields=changed,
        original=orig_d,
        edited=edit_d,
        original_chars=_payload_chars(orig_d),
        edited_chars=_payload_chars(edit_d),
    )


def _to_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        try:
            parsed = json.loads(obj)
            return parsed if isinstance(parsed, dict) else {"_value": parsed}
        except json.JSONDecodeError:
            return {"_value": obj}
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return {}


def _payload_chars(d: dict) -> int:
    return sum(len(v) for v in d.values() if isinstance(v, str))
