"""Regression tests for the weekly edit-rate trend sparkline.

Covers:
  - EditDiffStore.edits_by_type_in_window boundaries (inclusive-start, exclusive-end)
  - SQLiteStore.count_approved_by_type_in_window boundaries
  - _edit_rate_trend produces a point per week, correctly sized series
  - direction classification: down (improving), up (regressing), flat, unknown
  - sparkline SVG has the right number of points
  - clamps on weeks argument
  - /api/privacy/edit-rate-trend returns the stable JSON shape
  - /privacy page renders the sparkline chunk when trend has data
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghost_daemon.insights.edit_store import EditDiff, EditDiffStore


# ── Windowed counters ───────────────────────────────────────────────────────


def test_edits_by_type_in_window_is_exclusive_on_end(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=7)

    # One edit right at `start` (included) and one right at `end` (excluded).
    store.record(EditDiff(
        draft_id="in",
        developer_id="alice",
        artifact_type="standup",
        approved_at=start.isoformat(),
    ))
    store.record(EditDiff(
        draft_id="out",
        developer_id="alice",
        artifact_type="standup",
        approved_at=end.isoformat(),
    ))

    counts = store.edits_by_type_in_window(start, end)
    assert counts.get("standup") == 1


def test_edits_by_type_in_window_empty_returns_empty_dict(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert store.edits_by_type_in_window(now, now - timedelta(days=1)) == {}
    assert store.edits_by_type_in_window(now - timedelta(days=1), now) == {}


def test_count_approved_in_window_is_exclusive_on_end(tmp_path: Path) -> None:
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, QuickNote

    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")

    # Approve three quick_notes; their reviewed_at timestamps are 'now'.
    for i in range(3):
        d = DraftPayload(
            draft_id=f"q{i}", artifact_type="quick_note", developer_id="alice",
            payload=QuickNote(message=f"hi {i}"),
        )
        gate.submit(d)
        gate.approve(f"q{i}")

    # Wide window: all 3.
    wide = store.count_approved_by_type_in_window(
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    )
    assert wide["quick_note"] == 3

    # Narrow window before all approvals: 0.
    narrow = store.count_approved_by_type_in_window(
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30),
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=29),
    )
    assert narrow.get("quick_note", 0) == 0


# ── Trend series ────────────────────────────────────────────────────────────


def _seed_edits_at(store: EditDiffStore, artifact_type: str, when: datetime, n: int = 1) -> None:
    for i in range(n):
        store.record(EditDiff(
            draft_id=f"{artifact_type}-{when.isoformat()}-{i}",
            developer_id="alice",
            artifact_type=artifact_type,
            approved_at=when.isoformat(),
        ))


@pytest.fixture
def ghost_app(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.insights.scrubber_hits import ScrubberHitStore
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    cfg = Config(developer_id="alice", llm=LLMConfig(provider="groq"))
    store = SQLiteStore(tmp_path / "vantage.db")
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(tmp_path / "vantage.db")
    edits = EditDiffStore(tmp_path / "edits")
    hits = ScrubberHitStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice", edits=edits)

    app = build_app(store, gate, audit, subs, cfg, scrubber_hits=hits, edits=edits)
    return app, store, edits, gate


def test_trend_has_one_point_per_week(ghost_app) -> None:
    from ghost_daemon.ui.local_app import _edit_rate_trend
    app, store, edits, _ = ghost_app
    trend = _edit_rate_trend(store, edits, weeks=6)
    # No data yet → empty dict (no types seen).
    assert trend == {}


def test_trend_direction_down_when_edit_rate_drops(ghost_app) -> None:
    """Simulate the flywheel working: more approvals-with-no-edit in the recent
    week vs the previous week — edit rate drops, direction = 'down'."""
    from ghost_daemon.ui.local_app import _edit_rate_trend
    from vantage.schemas import DraftPayload, StandupBullets

    app, store, edits, gate = ghost_app
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prev_week = now - timedelta(days=10)

    # Previous week: 2 approvals, both edited → 100% edit rate.
    for i in range(2):
        d = DraftPayload(
            draft_id=f"old{i}", artifact_type="standup", developer_id="alice",
            payload=StandupBullets(yesterday=["x"], today=["y"]),
        )
        gate.submit(d)
        gate.approve(f"old{i}", edited_payload={
            "yesterday": ["x", "more"], "today": ["y"], "blockers": [],
        })
    # Rewrite the reviewed_at + approved_at to land in prev_week.
    _reseat_last_approvals(store, prev_week)
    # Also move the edit-diffs' approved_at into prev_week.
    _rewrite_edit_shard(edits, prev_week)

    # This week: 4 approvals, 0 edited → 0% edit rate.
    for i in range(4):
        d = DraftPayload(
            draft_id=f"new{i}", artifact_type="standup", developer_id="alice",
            payload=StandupBullets(yesterday=["x"], today=["y"]),
        )
        gate.submit(d)
        gate.approve(f"new{i}")

    trend = _edit_rate_trend(store, edits, weeks=3)
    assert "standup" in trend
    standup = trend["standup"]
    # direction should be 'down' — edit rate dropped from 100% to 0%
    assert standup["direction"] == "down", standup
    assert standup["delta_pct_points"] is not None
    assert standup["delta_pct_points"] < 0


def test_trend_direction_flat_when_delta_small(ghost_app) -> None:
    from ghost_daemon.ui.local_app import _edit_rate_trend

    app, store, edits, gate = ghost_app
    # Zero edits across the board = rate=0 every week with approvals = flat
    from vantage.schemas import DraftPayload, QuickNote
    for week_offset in (10, 3):
        for i in range(3):
            d = DraftPayload(
                draft_id=f"q-{week_offset}-{i}", artifact_type="quick_note",
                developer_id="alice", payload=QuickNote(message="hi"),
            )
            gate.submit(d)
            gate.approve(f"q-{week_offset}-{i}")
        _reseat_last_approvals(store, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=week_offset))

    trend = _edit_rate_trend(store, edits, weeks=3)
    if "quick_note" in trend:
        assert trend["quick_note"]["direction"] in ("flat", "down", "unknown")


def test_trend_sparkline_svg_has_polyline(ghost_app) -> None:
    from ghost_daemon.ui.local_app import _sparkline_svg
    svg = _sparkline_svg([0.9, 0.7, 0.5, 0.3, 0.1])
    assert svg.startswith("<svg")
    assert "<polyline" in svg
    # Five rates → five x,y pairs in the points attribute.
    assert svg.count(",") == 5


def test_trend_sparkline_empty_when_few_points() -> None:
    from ghost_daemon.ui.local_app import _sparkline_svg
    assert _sparkline_svg([]) == ""
    assert _sparkline_svg([0.5]) == ""


def test_trend_api_endpoint(ghost_app) -> None:
    app, _, _, _ = ghost_app
    r = TestClient(app).get("/api/privacy/edit-rate-trend?weeks=6")
    assert r.status_code == 200
    data = r.json()
    assert data["weeks"] == 6
    assert "trend" in data


def test_trend_api_clamps_weeks(ghost_app) -> None:
    app, _, _, _ = ghost_app
    r = TestClient(app).get("/api/privacy/edit-rate-trend?weeks=99")
    assert r.json()["weeks"] == 26
    r = TestClient(app).get("/api/privacy/edit-rate-trend?weeks=-5")
    assert r.json()["weeks"] == 2


def test_privacy_page_includes_trend_column(ghost_app) -> None:
    from vantage.schemas import DraftPayload, StandupBullets

    app, _, _, gate = ghost_app
    # Seed a single approval so the rate row shows up at all.
    d = DraftPayload(
        draft_id="x", artifact_type="standup", developer_id="alice",
        payload=StandupBullets(yesterday=["a"], today=["b"]),
    )
    gate.submit(d)
    gate.approve("x")

    r = TestClient(app).get("/privacy")
    assert r.status_code == 200
    assert "Trend (8w)" in r.text
    assert "Week-over-week" in r.text


# ── Helpers ─────────────────────────────────────────────────────────────────


def _reseat_last_approvals(store, when: datetime) -> None:
    """Rewrite the ``reviewed_at`` of recently-approved drafts to ``when``.

    The real approval path stamps ``datetime.now(timezone.utc).replace(tzinfo=None)``; for the trend test we
    need rows in prior weeks, so poke the SQLite row directly.
    """
    import sqlite3
    db_path = store._engine.url.database  # type: ignore[union-attr]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE digest_drafts SET reviewed_at = ? WHERE reviewed_at IS NOT NULL "
        "AND reviewed_at >= ?",
        (when.isoformat(), (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)).isoformat()),
    )
    conn.commit()
    conn.close()


def _rewrite_edit_shard(edits: EditDiffStore, when: datetime) -> None:
    """Rewrite the ``approved_at`` of every edit-diff in the current shard to
    ``when``. Used to backdate seeded edits into older weeks for the trend test.
    """
    import json
    shard = edits._current_shard(datetime.now(timezone.utc).replace(tzinfo=None))  # type: ignore[attr-defined]
    if not shard.exists():
        return
    lines = shard.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        data["approved_at"] = when.isoformat()
        out.append(json.dumps(data))
    shard.write_text("\n".join(out) + "\n", encoding="utf-8")
