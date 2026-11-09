"""Regression tests for FlowAnalyzer + /flow route.

Covers:
  - Heatmap bucketing (day_offset × hour)
  - Deep-window detection: single turn, back-to-back, long gaps, empty
  - Approval timeline reads from the ghost-daemon's SQLite ledger
  - Session JSONL parsing tolerates corrupt lines
  - /flow route renders with and without data
  - /api/flow returns stable JSON shape
  - Days parameter is clamped (DoS guard on an API query)
  - FlowAnalyzer never touches the network
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghost_daemon.insights.flow_state import (
    FlowAnalyzer,
    _build_heatmap,
    _find_deep_windows,
)


# ── _build_heatmap ──────────────────────────────────────────────────────────


def test_heatmap_zero_fills_empty_hours() -> None:
    now = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
    cells = _build_heatmap([], now=now, days=3)
    assert len(cells) == 3 * 24
    assert all(c.turn_count == 0 for c in cells)


def test_heatmap_counts_by_day_offset_and_hour() -> None:
    now = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
    stamps = [
        datetime(2026, 11, 8, 10, 15, tzinfo=timezone.utc),  # today 10:xx
        datetime(2026, 11, 8, 10, 45, tzinfo=timezone.utc),  # today 10:xx (2nd)
        datetime(2026, 11, 7, 14, 10, tzinfo=timezone.utc),  # yesterday 14:xx
    ]
    cells = _build_heatmap(stamps, now=now, days=7)
    by_key = {(c.day_offset, c.hour): c.turn_count for c in cells}
    assert by_key[(0, 10)] == 2
    assert by_key[(1, 14)] == 1
    assert by_key[(0, 14)] == 0


def test_heatmap_ignores_out_of_window_timestamps() -> None:
    now = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
    stamps = [datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)]  # 7 days ago
    cells = _build_heatmap(stamps, now=now, days=7)
    # day_offset=7 is out of the 0..6 range → dropped
    assert all(c.turn_count == 0 for c in cells)


# ── _find_deep_windows ──────────────────────────────────────────────────────


def test_deep_windows_empty_input() -> None:
    assert _find_deep_windows([], min_minutes=25, max_gap_minutes=10) == []


def test_deep_windows_single_turn_is_not_a_window() -> None:
    windows = _find_deep_windows(
        [datetime(2026, 11, 8, 10, 0)],
        min_minutes=25, max_gap_minutes=10,
    )
    assert windows == []


def test_deep_windows_detects_continuous_run() -> None:
    base = datetime(2026, 11, 8, 10, 0)
    stamps = [base + timedelta(minutes=i * 3) for i in range(15)]  # 42 min run
    windows = _find_deep_windows(stamps, min_minutes=25, max_gap_minutes=10)
    assert len(windows) == 1
    assert windows[0].duration_minutes >= 25
    assert windows[0].turn_count == 15


def test_deep_windows_splits_on_long_gap() -> None:
    base = datetime(2026, 11, 8, 10, 0)
    stamps = (
        [base + timedelta(minutes=i * 3) for i in range(12)]       # 33 min
        + [base + timedelta(minutes=120 + i * 3) for i in range(12)]  # gap, 33 min
    )
    windows = _find_deep_windows(stamps, min_minutes=25, max_gap_minutes=10)
    assert len(windows) == 2


def test_deep_windows_filters_short_runs() -> None:
    base = datetime(2026, 11, 8, 10, 0)
    # Four spurts of 5 turns each, long gaps between — none span >= 25 min
    stamps: list[datetime] = []
    for cluster in range(4):
        for i in range(5):
            stamps.append(base + timedelta(hours=cluster, minutes=i))
    windows = _find_deep_windows(stamps, min_minutes=25, max_gap_minutes=10)
    assert windows == []


def test_deep_windows_returns_newest_first() -> None:
    base = datetime(2026, 11, 8, 10, 0)
    stamps = (
        [base + timedelta(minutes=i * 3) for i in range(15)]
        + [base + timedelta(days=1, minutes=i * 3) for i in range(15)]
    )
    windows = _find_deep_windows(stamps, min_minutes=25, max_gap_minutes=10)
    assert len(windows) == 2
    assert windows[0].start > windows[1].start


# ── FlowAnalyzer: session parsing ────────────────────────────────────────────


def _write_session(path: Path, timestamps: list[datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i, ts in enumerate(timestamps):
            f.write(json.dumps({
                "role": "user",
                "content": f"turn {i}",
                "timestamp": ts.isoformat(),
            }) + "\n")


def test_flow_analyzer_reads_session_timestamps(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    sess = tmp_path / "project-a"
    stamps = [now - timedelta(minutes=i * 4) for i in range(10)]
    _write_session(sess / "session1.jsonl", stamps)

    analyzer = FlowAnalyzer(session_dirs=[sess], sqlite_path=None)
    snap = analyzer.snapshot(days=1, min_window_minutes=20, max_gap_minutes=10, now=now)
    assert snap.total_turns == 10
    # 10 turns × 4 min spacing = ~36 min window → 1 deep window expected
    assert len(snap.deep_windows) == 1


def test_flow_analyzer_skips_corrupt_lines(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    sess = tmp_path / "project-b"
    sess.mkdir(parents=True)
    path = sess / "session1.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"role":"user","content":"ok","timestamp":"' + now.isoformat() + '"}\n')
        f.write("this is not json\n")
        f.write('{"role":"user","content":"no ts"}\n')  # missing timestamp
    analyzer = FlowAnalyzer(session_dirs=[sess], sqlite_path=None)
    snap = analyzer.snapshot(days=1, now=now)
    assert snap.total_turns == 1


def test_flow_analyzer_skips_timestamps_outside_window(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    sess = tmp_path / "project-c"
    old = now - timedelta(days=30)
    recent = now - timedelta(hours=1)
    _write_session(sess / "mixed.jsonl", [old, recent])
    # File mtime check excludes very old files; simulate a fresh file by touching
    (sess / "mixed.jsonl").touch()

    analyzer = FlowAnalyzer(session_dirs=[sess], sqlite_path=None)
    snap = analyzer.snapshot(days=7, now=now)
    assert snap.total_turns == 1  # only "recent" survives the cutoff


def test_flow_analyzer_handles_missing_dirs() -> None:
    """A session dir that doesn't exist must not crash the analyzer."""
    analyzer = FlowAnalyzer(session_dirs=[Path("/does/not/exist")], sqlite_path=None)
    snap = analyzer.snapshot(days=7)
    assert snap.total_turns == 0


# ── FlowAnalyzer: approvals ──────────────────────────────────────────────────


def test_flow_analyzer_reads_recent_approvals(tmp_path: Path) -> None:
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, QuickNote

    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")
    draft = DraftPayload(
        draft_id="d1", artifact_type="quick_note", developer_id="alice",
        payload=QuickNote(message="hi"),
    )
    gate.submit(draft)
    gate.approve("d1")

    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=tmp_path / "vantage.db")
    snap = analyzer.snapshot(days=7)
    assert len(snap.recent_approvals) == 1
    assert snap.recent_approvals[0].status == "approved"
    assert snap.recent_approvals[0].artifact_type == "quick_note"


# ── /flow route ──────────────────────────────────────────────────────────────


@pytest.fixture
def ghost_app(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    sess = tmp_path / "sessions"
    sess.mkdir()
    cfg = Config(
        developer_id="alice",
        session_dirs=[str(sess)],
        llm=LLMConfig(provider="groq"),
    )
    store = SQLiteStore(tmp_path / "vantage.db")
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")

    # Monkey the vantage_dir property to point at tmp_path — the privacy
    # helpers need sqlite_path which Config computes from vantage_dir.
    # It already looks up sqlite_path via property, which uses vantage_dir —
    # but we can bypass by pointing cfg.session_dirs correctly and letting
    # the analyzer use cfg.sqlite_path through the UI helper.
    #
    # The UI helper reads cfg.resolved_session_dirs + cfg.sqlite_path. The
    # latter points to the real user dir — redirect via session_dirs check
    # below: for the /flow test, we don't need DB data, so this is fine.

    app = build_app(store, gate, audit, subs, cfg)
    return app, sess


def test_flow_route_renders_empty_state(ghost_app) -> None:
    app, _sess = ghost_app
    r = TestClient(app).get("/flow")
    assert r.status_code == 200
    assert "your week" in r.text.lower() or "flow" in r.text.lower()
    assert "Rhythm" in r.text
    assert "Deep-work windows" in r.text


def test_flow_route_shows_session_data(ghost_app) -> None:
    app, sess = ghost_app
    now = datetime.now(timezone.utc)
    stamps = [now - timedelta(minutes=i * 3) for i in range(15)]
    _write_session(sess / "sess1.jsonl", stamps)
    # Touch to pass the mtime cutoff guard
    (sess / "sess1.jsonl").touch()

    r = TestClient(app).get("/flow?days=1")
    assert r.status_code == 200
    # 15 turns should surface in the summary stat
    assert "15" in r.text


def test_flow_api_returns_stable_shape(ghost_app) -> None:
    app, _ = ghost_app
    r = TestClient(app).get("/api/flow")
    assert r.status_code == 200
    data = r.json()
    for key in ("days", "total_turns", "active_hours", "heatmap",
                "deep_windows", "recent_approvals"):
        assert key in data, f"missing key {key}"
    assert isinstance(data["heatmap"], list)
    assert isinstance(data["deep_windows"], list)


def test_flow_route_clamps_days_parameter(ghost_app) -> None:
    app, _ = ghost_app
    # 365 should get clamped to 30; the response must still be valid HTML.
    r = TestClient(app).get("/flow?days=365")
    assert r.status_code == 200
    # Clamp to 30 means heatmap has 30 * 24 = 720 cells; our /api/flow
    # exposes the clamped `days` field.
    api = TestClient(app).get("/api/flow?days=365").json()
    assert api["days"] == 30


def test_flow_route_tolerates_negative_days(ghost_app) -> None:
    app, _ = ghost_app
    r = TestClient(app).get("/api/flow?days=-5")
    assert r.status_code == 200
    assert r.json()["days"] == 1


# ── Local-only invariant ─────────────────────────────────────────────────────


def test_flow_analyzer_makes_no_network_calls(tmp_path: Path, monkeypatch) -> None:
    """FlowAnalyzer must work without network — stub httpx to detonate."""
    import httpx

    def _no_net(*args, **kwargs):
        raise AssertionError("FlowAnalyzer must never make network calls")

    monkeypatch.setattr(httpx, "AsyncClient", _no_net)
    monkeypatch.setattr(httpx, "Client", _no_net)

    sess = tmp_path / "s"
    sess.mkdir()
    _write_session(sess / "x.jsonl", [datetime.now(timezone.utc)])
    (sess / "x.jsonl").touch()

    analyzer = FlowAnalyzer(session_dirs=[sess], sqlite_path=None)
    snap = analyzer.snapshot(days=1)
    assert snap.total_turns >= 0  # just must not raise
