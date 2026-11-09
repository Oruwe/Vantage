"""Regression tests for FlowAnalyzer's stuck-pattern surface.

Covers:
  - _collect_stuck_patterns returns BugResolution rows newest-first
  - Window filter excludes drafts older than the cutoff
  - Corrupt payload JSON is skipped, not crashing the whole snapshot
  - Rows other than bug_resolution are ignored
  - total_stuck_attempts aggregates attempt counts
  - FlowSnapshot carries stuck_patterns through to /api/flow
  - /flow page renders the stuck-pattern section when data exists
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghost_daemon.insights.flow_state import FlowAnalyzer


def _insert_bug_resolution(
    db_path: Path,
    *,
    draft_id: str,
    payload: dict,
    created_at: datetime,
    artifact_type: str = "bug_resolution",
) -> None:
    """Write a digest_drafts row directly — the real submit path requires a
    full ApprovalGate, but for query-shape tests we only need the row."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO digest_drafts "
        "(id, developer_id, artifact_type, period_start, period_end, payload_json, status, created_at) "
        "VALUES (?, 'alice', ?, ?, ?, ?, 'pending', ?)",
        (
            draft_id, artifact_type,
            created_at.isoformat(), created_at.isoformat(),
            json.dumps(payload), created_at.isoformat(),
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_with_schema(tmp_path: Path) -> Path:
    """A fresh vantage.db with the digest_drafts table ready."""
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    SQLiteStore(tmp_path / "vantage.db")  # creates the schema
    return tmp_path / "vantage.db"


# ── _collect_stuck_patterns ─────────────────────────────────────────────────


def test_collect_stuck_patterns_returns_newest_first(db_with_schema: Path) -> None:
    now = datetime.now(timezone.utc)
    _insert_bug_resolution(
        db_with_schema, draft_id="r1",
        payload={
            "resolution_id": "r1",
            "developer_id": "alice",
            "error_signature": "TypeError: generic inference",
            "fix_summary": "split constraint",
            "attempt_count": 4,
            "files_touched": ["src/types.ts"],
        },
        created_at=now - timedelta(hours=1),
    )
    _insert_bug_resolution(
        db_with_schema, draft_id="r2",
        payload={
            "resolution_id": "r2",
            "developer_id": "alice",
            "error_signature": "ConnectionError: timeout",
            "fix_summary": "bumped pool size",
            "attempt_count": 2,
            "files_touched": ["db/pool.py", "config/db.toml"],
        },
        created_at=now - timedelta(minutes=10),
    )

    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=db_with_schema)
    snap = analyzer.snapshot(days=1, now=now)
    assert len(snap.stuck_patterns) == 2
    # Newer row is first
    assert snap.stuck_patterns[0].resolution_id == "r2"
    assert snap.stuck_patterns[1].resolution_id == "r1"


def test_stuck_patterns_excludes_outside_window(db_with_schema: Path) -> None:
    now = datetime.now(timezone.utc)
    _insert_bug_resolution(
        db_with_schema, draft_id="old",
        payload={"resolution_id": "old", "developer_id": "alice",
                 "error_signature": "x", "fix_summary": "y", "attempt_count": 1},
        created_at=now - timedelta(days=30),
    )
    _insert_bug_resolution(
        db_with_schema, draft_id="new",
        payload={"resolution_id": "new", "developer_id": "alice",
                 "error_signature": "x", "fix_summary": "y", "attempt_count": 1},
        created_at=now - timedelta(hours=2),
    )

    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=db_with_schema)
    snap = analyzer.snapshot(days=7, now=now)
    ids = [p.resolution_id for p in snap.stuck_patterns]
    assert "new" in ids
    assert "old" not in ids


def test_stuck_patterns_skips_corrupt_payload(db_with_schema: Path) -> None:
    """A row with un-JSON payload shouldn't tank the /flow render."""
    now = datetime.now(timezone.utc)
    # Insert raw non-JSON payload bypassing the helper
    conn = sqlite3.connect(db_with_schema)
    conn.execute(
        "INSERT INTO digest_drafts "
        "(id, developer_id, artifact_type, period_start, period_end, payload_json, status, created_at) "
        "VALUES ('bad', 'alice', 'bug_resolution', ?, ?, ?, 'pending', ?)",
        (now.isoformat(), now.isoformat(), "not valid json", now.isoformat()),
    )
    conn.commit()
    conn.close()
    # Plus one good row to verify the good one still surfaces.
    _insert_bug_resolution(
        db_with_schema, draft_id="ok",
        payload={"resolution_id": "ok", "developer_id": "alice",
                 "error_signature": "x", "fix_summary": "y", "attempt_count": 1},
        created_at=now - timedelta(minutes=5),
    )

    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=db_with_schema)
    snap = analyzer.snapshot(days=1, now=now)
    assert len(snap.stuck_patterns) == 1
    assert snap.stuck_patterns[0].resolution_id == "ok"


def test_stuck_patterns_ignores_other_artifact_types(db_with_schema: Path) -> None:
    """Only bug_resolution rows should show up here."""
    now = datetime.now(timezone.utc)
    _insert_bug_resolution(
        db_with_schema, draft_id="s1",
        payload={"yesterday": ["x"], "today": ["y"]},
        created_at=now - timedelta(hours=1),
        artifact_type="standup",
    )
    _insert_bug_resolution(
        db_with_schema, draft_id="r1",
        payload={"resolution_id": "r1", "developer_id": "alice",
                 "error_signature": "x", "fix_summary": "y", "attempt_count": 1},
        created_at=now - timedelta(hours=1),
        artifact_type="bug_resolution",
    )
    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=db_with_schema)
    snap = analyzer.snapshot(days=1, now=now)
    assert [p.resolution_id for p in snap.stuck_patterns] == ["r1"]


def test_total_stuck_attempts_sums_attempt_counts(db_with_schema: Path) -> None:
    now = datetime.now(timezone.utc)
    for i, attempts in enumerate([3, 5, 2]):
        _insert_bug_resolution(
            db_with_schema, draft_id=f"r{i}",
            payload={"resolution_id": f"r{i}", "developer_id": "alice",
                     "error_signature": f"err{i}", "fix_summary": "y",
                     "attempt_count": attempts},
            created_at=now - timedelta(minutes=10 + i),
        )
    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=db_with_schema)
    snap = analyzer.snapshot(days=1, now=now)
    assert snap.total_stuck_attempts == 3 + 5 + 2


def test_stuck_patterns_truncate_long_fields(db_with_schema: Path) -> None:
    """Error signatures and fix summaries are capped for display."""
    now = datetime.now(timezone.utc)
    big_sig = "X" * 500
    big_fix = "Y" * 500
    _insert_bug_resolution(
        db_with_schema, draft_id="big",
        payload={"resolution_id": "big", "developer_id": "alice",
                 "error_signature": big_sig, "fix_summary": big_fix,
                 "attempt_count": 1, "files_touched": [f"f{i}.py" for i in range(20)]},
        created_at=now - timedelta(minutes=5),
    )
    analyzer = FlowAnalyzer(session_dirs=[], sqlite_path=db_with_schema)
    snap = analyzer.snapshot(days=1, now=now)
    assert len(snap.stuck_patterns) == 1
    p = snap.stuck_patterns[0]
    assert len(p.error_signature) <= 160
    assert len(p.fix_summary) <= 280
    assert len(p.files_touched) <= 8


# ── /flow route integration ─────────────────────────────────────────────────


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
    # Build the SQLite path at cfg.sqlite_path — this requires Config's
    # resolved vantage_dir, which won't be tmp_path. For test purposes we
    # don't care about real vantage_dir; we just assert the route renders.
    store = SQLiteStore(tmp_path / "vantage.db")
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")

    app = build_app(store, gate, audit, subs, cfg)
    return app, store


def test_flow_route_renders_stuck_patterns_section(ghost_app) -> None:
    app, _ = ghost_app
    r = TestClient(app).get("/flow")
    assert r.status_code == 200
    assert "Stuck patterns" in r.text
    assert "what tripped you up" in r.text


def test_flow_api_includes_stuck_patterns_key(ghost_app) -> None:
    app, _ = ghost_app
    r = TestClient(app).get("/api/flow")
    assert r.status_code == 200
    data = r.json()
    assert "stuck_patterns" in data
    assert "total_stuck_attempts" in data
    assert isinstance(data["stuck_patterns"], list)
