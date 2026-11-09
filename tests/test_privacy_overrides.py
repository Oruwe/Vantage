"""Regression tests for the override + edit-rate loop.

Covers:
  - POST /api/privacy/override happy path
  - 400 on missing/invalid body fields
  - 400 on oversized pattern (DoS guard)
  - still_redacted_by_current_scrubber flag reflects reality
  - GET /api/privacy/overrides lists recent entries newest-first
  - EditDiffStore.edits_by_type_since windowed count
  - SQLiteStore.count_approved_by_type_since windowed count
  - /privacy HTML page renders the rate section when data is present
  - GET /api/privacy/edit-rate math
  - override → precision eval bridge: a recorded override surfaces as a case
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghost_daemon.insights.edit_store import EditDiff, EditDiffStore
from ghost_daemon.insights.scrubber_hits import ScrubberHitStore


@pytest.fixture
def ghost_app(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
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
    return app, store, hits, edits


# ── Override endpoint ───────────────────────────────────────────────────────


def test_override_endpoint_records_and_confirms_match(ghost_app) -> None:
    app, _store, hits, _edits = ghost_app
    client = TestClient(app)
    # A real AWS access key — the scrubber will match this.
    r = client.post("/api/privacy/override", json={
        "pattern": "AKIAIOSFODNN7EXAMPLE",
        "kind": "aws_access_key",
    })
    assert r.status_code == 200
    d = r.json()
    assert d["recorded"] is True
    assert d["still_redacted_by_current_scrubber"] is True
    assert d["kind"] == "aws_access_key"
    # And it's persisted
    overrides = hits.recent_overrides()
    assert ("AKIAIOSFODNN7EXAMPLE", "aws_access_key", pytest.approx) or overrides, (
        "override must be in the SQLite store"
    )
    assert any(o[0] == "AKIAIOSFODNN7EXAMPLE" for o in overrides)


def test_override_endpoint_notices_already_safe_patterns(ghost_app) -> None:
    """When the dev reports a pattern that the scrubber does not currently
    redact, we still record it (for posterity) but flag the no-op state."""
    app, _, hits, _ = ghost_app
    r = TestClient(app).post("/api/privacy/override", json={
        "pattern": "this is just plain prose and should not match anything",
        "kind": "aws_access_key",
    })
    assert r.status_code == 200
    d = r.json()
    assert d["still_redacted_by_current_scrubber"] is False
    assert any(o[0].startswith("this is just plain prose") for o in hits.recent_overrides())


def test_override_endpoint_rejects_missing_fields(ghost_app) -> None:
    app, _, _, _ = ghost_app
    client = TestClient(app)
    assert client.post("/api/privacy/override", json={"pattern": ""}).status_code == 400
    assert client.post("/api/privacy/override", json={"pattern": "x"}).status_code == 400
    assert client.post("/api/privacy/override", json={"kind": "jwt"}).status_code == 400


def test_override_endpoint_rejects_oversize_pattern(ghost_app) -> None:
    """A 600-char pattern should 400 — this is a DoS guard on a public-ish
    local endpoint, not a real limit."""
    app, _, _, _ = ghost_app
    r = TestClient(app).post("/api/privacy/override", json={
        "pattern": "X" * 600,
        "kind": "high_entropy",
    })
    assert r.status_code == 400


def test_override_endpoint_rejects_invalid_json(ghost_app) -> None:
    app, _, _, _ = ghost_app
    r = TestClient(app).post(
        "/api/privacy/override", content="not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_override_list_endpoint_returns_recent(ghost_app) -> None:
    app, _, hits, _ = ghost_app
    hits.record_override("ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD", "github_token")
    hits.record_override("another one", "high_entropy")
    r = TestClient(app).get("/api/privacy/overrides")
    assert r.status_code == 200
    data = r.json()
    assert len(data["overrides"]) == 2
    patterns = [o["pattern"] for o in data["overrides"]]
    assert "another one" in patterns


# ── Edit-rate math ──────────────────────────────────────────────────────────


def test_edits_by_type_since_counts_only_recent(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # One recent, one old
    store.record(EditDiff(
        draft_id="d1", developer_id="alice", artifact_type="standup",
        approved_at=now.isoformat(),
    ))
    store.record(EditDiff(
        draft_id="d2", developer_id="alice", artifact_type="standup",
        approved_at=(now - timedelta(days=60)).isoformat(),
    ))
    counts = store.edits_by_type_since(now - timedelta(days=30))
    assert counts.get("standup") == 1


def test_count_approved_by_type_since(tmp_path: Path) -> None:
    """SQLiteStore reports the right per-type approval count in the window."""
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, QuickNote, StandupBullets

    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")

    for i in range(3):
        draft = DraftPayload(
            draft_id=f"s{i}", artifact_type="standup", developer_id="alice",
            payload=StandupBullets(yesterday=["x"], today=["y"]),
        )
        gate.submit(draft)
        gate.approve(f"s{i}")
    draft = DraftPayload(
        draft_id="q1", artifact_type="quick_note", developer_id="alice",
        payload=QuickNote(message="hi"),
    )
    gate.submit(draft)
    gate.approve("q1")

    counts = store.count_approved_by_type_since(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30))
    assert counts["standup"] == 3
    assert counts["quick_note"] == 1


def test_edit_rate_api_returns_per_type_math(ghost_app, tmp_path: Path) -> None:
    from ghost_daemon.approval.gate import ApprovalGate
    from vantage.schemas import DraftPayload, StandupBullets

    app, store, _, edits = ghost_app
    gate = ApprovalGate(store, "alice", edits=edits)

    # Approve 4 standup drafts; edit 1 of them.
    for i in range(4):
        d = DraftPayload(
            draft_id=f"s{i}", artifact_type="standup", developer_id="alice",
            payload=StandupBullets(yesterday=["x"], today=["y"]),
        )
        gate.submit(d)
        if i == 0:
            gate.approve(f"s{i}", edited_payload={
                "yesterday": ["x", "also fixed a bug"],
                "today": ["y"],
                "blockers": [],
            })
        else:
            gate.approve(f"s{i}")

    r = TestClient(app).get("/api/privacy/edit-rate?days=30")
    assert r.status_code == 200
    data = r.json()
    standup = next(row for row in data["rows"] if row["artifact_type"] == "standup")
    assert standup["approvals"] == 4
    assert standup["edits"] == 1
    assert 0.24 < standup["rate"] < 0.26


def test_privacy_page_renders_rate_section(ghost_app) -> None:
    app, _, _, _ = ghost_app
    r = TestClient(app).get("/privacy")
    assert r.status_code == 200
    assert "Edit rate" in r.text
    assert "report false positive" in r.text or "false positive" in r.text


# ── Override → eval bridge ──────────────────────────────────────────────────


def test_overrides_eval_bridge_picks_up_stored_patterns(ghost_app, monkeypatch) -> None:
    """The override-driven precision test should discover entries from the DB
    when ``VANTAGE_OVERRIDES_DB`` points at the ghost daemon's vantage.db."""
    app, store, hits, _ = ghost_app
    hits.record_override("hunter2_is_not_a_secret_right", "high_entropy")

    db_path = Path(store._engine.url.database)  # type: ignore[union-attr]
    monkeypatch.setenv("VANTAGE_OVERRIDES_DB", str(db_path))

    # Dynamically reload the bridge so it re-reads the env
    import importlib
    import tests.evals.scrubber.test_overrides as bridge
    importlib.reload(bridge)

    cases = bridge._load_overrides()
    assert any(p == "hunter2_is_not_a_secret_right" for p, _ in cases)


def test_overrides_bridge_skips_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("VANTAGE_OVERRIDES_DB", raising=False)
    import importlib
    import tests.evals.scrubber.test_overrides as bridge
    importlib.reload(bridge)
    assert bridge._load_overrides() == []
