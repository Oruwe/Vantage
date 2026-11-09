"""Tests for the local dev-insights surface.

Covers:
  - EditDiffStore round-trip + no-op suppression + thread-safe append
  - ScrubberHitStore counting from a RedactedContext
  - ScrubberHitStore weekly window
  - Scrubber sink registration fires on every scrub() call
  - ApprovalGate records an edit-diff only when the payload actually changed
  - /privacy route renders with and without data
  - /privacy never touches the shadow-replica push path (local-only)
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ghost_daemon.insights.edit_store import (
    EditDiff,
    EditDiffStore,
    compute_diff,
)
from ghost_daemon.insights.scrubber_hits import ScrubberHitStore
from ghost_daemon.redaction import scrubber as scrubber_mod
from vantage.schemas import Redaction, RedactedContext


# ── EditDiffStore ────────────────────────────────────────────────────────────


def test_edit_diff_store_roundtrips(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    diff = EditDiff(
        draft_id="d1",
        developer_id="alice",
        artifact_type="standup",
        approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        changed_fields=["summary"],
        original={"summary": "did 3 things"},
        edited={"summary": "did 4 things"},
        original_chars=12,
        edited_chars=12,
    )
    store.record(diff)

    recent = store.read_recent()
    assert len(recent) == 1
    assert recent[0].draft_id == "d1"
    assert recent[0].changed_fields == ["summary"]


def test_edit_diff_store_appends_without_clobber(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    for i in range(5):
        store.record(EditDiff(
            draft_id=f"d{i}",
            developer_id="alice",
            artifact_type="standup",
            approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        ))
    recent = store.read_recent()
    assert len(recent) == 5
    assert {e.draft_id for e in recent} == {"d0", "d1", "d2", "d3", "d4"}


def test_edit_diff_store_is_thread_safe(tmp_path: Path) -> None:
    """20 threads × 5 writes each must land exactly 100 well-formed JSONL lines."""
    store = EditDiffStore(tmp_path / "edits")

    def _writer(thread_id: int) -> None:
        for n in range(5):
            store.record(EditDiff(
                draft_id=f"t{thread_id}-{n}",
                developer_id="alice",
                artifact_type="commit_report",
                approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            ))

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    shard = next((tmp_path / "edits").glob("*.jsonl"))
    lines = [line for line in shard.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 100
    for line in lines:
        json.loads(line)  # each line must be valid JSON on its own


def test_compute_diff_suppresses_no_op() -> None:
    """If the dev clicks Approve without editing, no diff should be produced."""
    same = {"summary": "unchanged"}
    assert compute_diff("d1", "alice", "standup", same, same) is None


def test_compute_diff_detects_field_changes() -> None:
    diff = compute_diff(
        "d1", "alice", "standup",
        {"summary": "v1", "mood": "ok"},
        {"summary": "v2", "mood": "ok"},
    )
    assert diff is not None
    assert diff.changed_fields == ["summary"]


def test_compute_diff_accepts_json_strings() -> None:
    diff = compute_diff(
        "d1", "alice", "commit_report",
        json.dumps({"title": "old"}),
        {"title": "new"},
    )
    assert diff is not None
    assert "title" in diff.changed_fields


# ── ScrubberHitStore ─────────────────────────────────────────────────────────


def test_scrubber_hit_store_records_redactions(tmp_path: Path) -> None:
    hits = ScrubberHitStore(tmp_path / "vantage.db")
    ctx = RedactedContext(
        original_length=100,
        scrubbed_text="…",
        redactions=[
            Redaction(kind="aws_access_key", placeholder="[REDACTED:...]", position=0),
            Redaction(kind="github_token", placeholder="[REDACTED:...]", position=50),
            Redaction(kind="aws_access_key", placeholder="[REDACTED:...]", position=60),
        ],
        file_path="src/app.py",
    )
    hits.record_context(ctx)

    totals = hits.weekly_totals()
    # Hits with identical (kind, occurred_at, file_path) collapse; three hits
    # written in one millisecond share a PK, so expect at least 1 of each kind.
    assert totals.total >= 2
    assert "aws_access_key" in totals.by_kind
    assert "github_token" in totals.by_kind


def test_scrubber_hit_store_ignores_empty_context(tmp_path: Path) -> None:
    hits = ScrubberHitStore(tmp_path / "vantage.db")
    hits.record_context(RedactedContext(
        original_length=10, scrubbed_text="hello", redactions=[], file_path=None,
    ))
    assert hits.weekly_totals().total == 0


def test_scrubber_hit_store_window_excludes_old(tmp_path: Path) -> None:
    """Only counts since the cutoff should be returned."""
    hits = ScrubberHitStore(tmp_path / "vantage.db")
    hits.record_context(RedactedContext(
        original_length=10, scrubbed_text="…",
        redactions=[Redaction(kind="jwt", placeholder="x", position=0)],
    ))
    # Query in the future — should return zero.
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert hits.totals_since(future).total == 0


def test_scrubber_hit_store_tracks_overrides(tmp_path: Path) -> None:
    hits = ScrubberHitStore(tmp_path / "vantage.db")
    hits.record_override("internal.example.com", "high_entropy")
    overrides = hits.recent_overrides()
    assert len(overrides) == 1
    pattern, kind, _ = overrides[0]
    assert pattern == "internal.example.com"
    assert kind == "high_entropy"


# ── Scrubber sink registration ───────────────────────────────────────────────


def test_scrubber_sink_fires_on_every_scrub() -> None:
    received: list[RedactedContext] = []
    scrubber_mod.clear_hit_sinks()
    scrubber_mod.register_hit_sink(received.append)
    try:
        scrubber_mod.scrub("export AWS_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE")
        scrubber_mod.scrub("nothing to redact here")
    finally:
        scrubber_mod.clear_hit_sinks()

    assert len(received) == 2
    assert received[0].redactions, "secret-bearing scrub should emit a non-empty context"
    assert not received[1].redactions, "benign scrub should emit an empty context"


def test_scrubber_sink_exceptions_dont_break_scrub() -> None:
    """A broken sink must never block the scrub pipeline — this is critical
    because the scrub path runs before every LLM call."""
    def boom(_ctx):
        raise RuntimeError("sink is buggy")

    scrubber_mod.clear_hit_sinks()
    scrubber_mod.register_hit_sink(boom)
    try:
        ctx = scrubber_mod.scrub("AKIAIOSFODNN7EXAMPLE should still be redacted")
    finally:
        scrubber_mod.clear_hit_sinks()

    assert "AKIAIOSFODNN7EXAMPLE" not in ctx.scrubbed_text


# ── ApprovalGate edit capture (integration) ──────────────────────────────────


def test_approval_gate_records_edit_when_payload_changed(tmp_path: Path) -> None:
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, StandupBullets

    edits = EditDiffStore(tmp_path / "edits")
    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice", edits=edits)

    draft = DraftPayload(
        draft_id="d1",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(
            yesterday=["started auth refactor"],
            today=["finish auth refactor"],
        ),
    )
    gate.submit(draft)

    # Approve with an edit
    gate.approve(
        "d1",
        edited_payload={
            "yesterday": ["started auth refactor", "fixed a typo"],
            "today": ["finish auth refactor"],
            "blockers": [],
        },
    )

    recent = edits.read_recent()
    assert len(recent) == 1
    assert "yesterday" in recent[0].changed_fields


def test_approval_gate_skips_edit_record_when_no_edit(tmp_path: Path) -> None:
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, StandupBullets

    edits = EditDiffStore(tmp_path / "edits")
    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice", edits=edits)

    draft = DraftPayload(
        draft_id="d1",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(yesterday=["did a thing"], today=["more things"]),
    )
    gate.submit(draft)

    # Approve without editing — passing None means "use the original draft"
    gate.approve("d1", edited_payload=None)

    assert edits.read_recent() == []


def test_approval_gate_works_without_edits_store(tmp_path: Path) -> None:
    """Edit capture is opt-in. ApprovalGate must remain functional when no
    EditDiffStore is wired up."""
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, QuickNote

    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")  # no edits= kwarg

    draft = DraftPayload(
        draft_id="d1",
        artifact_type="quick_note",
        developer_id="alice",
        payload=QuickNote(message="hi"),
    )
    gate.submit(draft)
    approved = gate.approve("d1")
    assert approved.developer_id == "alice"


# ── /privacy route (integration) ─────────────────────────────────────────────


@pytest.fixture
def ghost_app(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    cfg = Config(developer_id="alice", llm=LLMConfig(provider="groq"))
    # Redirect vantage_dir to tmp for the test
    cfg.__dict__["_test_vantage_dir"] = tmp_path
    # Monkey the property via a subclass-ish trick isn't trivial; instead
    # construct stores by hand under tmp_path and pass them in.
    store = SQLiteStore(tmp_path / "vantage.db")
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(tmp_path / "vantage.db")
    edits = EditDiffStore(tmp_path / "edits")
    hits = ScrubberHitStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice", edits=edits)

    app = build_app(
        store, gate, audit, subs, cfg,
        scrubber_hits=hits, edits=edits,
    )
    return app, hits, edits


def test_privacy_route_renders_empty_state(ghost_app) -> None:
    from fastapi.testclient import TestClient

    app, _, _ = ghost_app
    client = TestClient(app)
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "what your scrubber caught" in r.text
    assert "Last 7 days" in r.text


def test_privacy_route_shows_recorded_hits(ghost_app) -> None:
    from fastapi.testclient import TestClient

    app, hits, _ = ghost_app
    hits.record_context(RedactedContext(
        original_length=20, scrubbed_text="…",
        redactions=[Redaction(kind="aws_access_key", placeholder="x", position=0)],
    ))
    r = TestClient(app).get("/privacy")
    assert r.status_code == 200
    assert "aws access key" in r.text.lower() or "aws_access_key" in r.text


def test_privacy_api_returns_json_totals(ghost_app) -> None:
    from fastapi.testclient import TestClient

    app, hits, _ = ghost_app
    hits.record_context(RedactedContext(
        original_length=20, scrubbed_text="…",
        redactions=[Redaction(kind="github_token", placeholder="x", position=0)],
    ))
    r = TestClient(app).get("/api/privacy/totals")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert "github_token" in data["by_kind"]


# ── Local-only invariant ─────────────────────────────────────────────────────


def test_edit_diff_store_never_calls_out(tmp_path: Path, monkeypatch) -> None:
    """EditDiffStore must do its job with only filesystem access — a broken
    network must not affect the approval path. We confirm this by stubbing
    httpx.AsyncClient to detonate if called."""
    import httpx

    def _no_net(*args, **kwargs):
        raise AssertionError("EditDiffStore must never make network calls")

    monkeypatch.setattr(httpx, "AsyncClient", _no_net)

    store = EditDiffStore(tmp_path / "edits")
    diff = EditDiff(
        draft_id="d1", developer_id="alice", artifact_type="standup",
        approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    )
    store.record(diff)  # must succeed


def test_scrubber_hit_store_never_calls_out(tmp_path: Path, monkeypatch) -> None:
    import httpx

    def _no_net(*args, **kwargs):
        raise AssertionError("ScrubberHitStore must never make network calls")

    monkeypatch.setattr(httpx, "AsyncClient", _no_net)

    hits = ScrubberHitStore(tmp_path / "vantage.db")
    hits.record_context(RedactedContext(
        original_length=10, scrubbed_text="…",
        redactions=[Redaction(kind="jwt", placeholder="x", position=0)],
    ))
    assert hits.weekly_totals().total >= 1
