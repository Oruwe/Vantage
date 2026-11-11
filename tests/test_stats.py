"""Regression tests for the `vantage-ghost stats` command.

Covers:
  - compute_report counts drafts / approvals / rejections by week
  - edited drafts are detected via edited_payload_json IS NOT NULL
  - approval + edit rates compute correctly (and None when denominators are 0)
  - scrubber hits are aggregated by kind across the full window
  - pending drafts are counted from current state, not a window
  - fresh install (no SQLite file yet) returns zeroes without crashing
  - markdown renderer has the three section headings and no artifact content
  - JSON renderer round-trips through json.loads and has the stable shape
  - CLI `--weeks` flag is accepted and `--json` toggles the output format
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghost_daemon.approval.gate import ApprovalGate
from ghost_daemon.insights.scrubber_hits import ScrubberHitStore
from ghost_daemon.insights.stats import (
    StatsReport,
    WindowStats,
    compute_report,
    render_json,
    render_markdown,
)
from ghost_daemon.storage.sqlite_store import SQLiteStore
from vantage.schemas import DraftPayload, QuickNote, Redaction, RedactedContext


runner = CliRunner()


# ── Helpers ────────────────────────────────────────────────────────────────


def _backdate_created(db: Path, draft_id: str, when: datetime) -> None:
    """Rewrite created_at for an existing draft. The gate always stamps 'now',
    so tests need to shift rows into older windows."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE digest_drafts SET created_at = ? WHERE id = ?",
            (when.isoformat(), draft_id),
        )
        conn.commit()


def _backdate_reviewed(db: Path, draft_id: str, when: datetime) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE digest_drafts SET reviewed_at = ? WHERE id = ?",
            (when.isoformat(), draft_id),
        )
        conn.commit()


def _submit_quick(gate: ApprovalGate, draft_id: str) -> None:
    gate.submit(DraftPayload(
        draft_id=draft_id,
        artifact_type="quick_note",
        developer_id="alice",
        payload=QuickNote(message=f"msg {draft_id}"),
    ))


# ── Core compute ───────────────────────────────────────────────────────────


def test_compute_report_counts_approved_and_rejected_in_current_week(tmp_path: Path):
    db = tmp_path / "vantage.db"
    store = SQLiteStore(db)
    gate = ApprovalGate(store, "alice")

    _submit_quick(gate, "a1")
    gate.approve("a1")
    _submit_quick(gate, "a2")
    gate.approve("a2")
    _submit_quick(gate, "r1")
    gate.reject("r1")
    _submit_quick(gate, "p1")  # left pending

    report = compute_report(sqlite_path=db, weeks=4)

    assert report.pending == 1
    assert report.totals["approved"] == 2
    assert report.totals["rejected"] == 1
    assert report.totals["drafts_created"] == 4
    # Current (last) bucket should hold all 4; earlier buckets are empty.
    assert report.windows[-1].drafts_created == 4
    assert report.windows[-1].approved == 2
    assert report.windows[-1].rejected == 1
    for w in report.windows[:-1]:
        assert w.drafts_created == 0 and w.approved == 0


def test_approval_rate_and_edit_rate(tmp_path: Path):
    db = tmp_path / "vantage.db"
    store = SQLiteStore(db)
    gate = ApprovalGate(store, "alice")

    _submit_quick(gate, "a1")
    gate.approve("a1", edited_payload={"message": "touched"})
    _submit_quick(gate, "a2")
    gate.approve("a2")  # unedited
    _submit_quick(gate, "r1")
    gate.reject("r1")

    report = compute_report(sqlite_path=db, weeks=1)
    d = report.to_dict()

    # Approval rate = 2 approved / 3 reviewed.
    assert abs(d["totals"]["approval_rate"] - (2 / 3)) < 1e-9
    # Edit rate = 1 edited / 2 approved.
    assert abs(d["totals"]["edit_rate"] - 0.5) < 1e-9


def test_rates_are_none_when_denominator_zero(tmp_path: Path):
    db = tmp_path / "vantage.db"
    SQLiteStore(db)  # create empty schema
    report = compute_report(sqlite_path=db, weeks=2)
    d = report.to_dict()
    assert d["totals"]["approval_rate"] is None
    assert d["totals"]["edit_rate"] is None
    for w in d["windows"]:
        assert w["edit_rate"] is None
        assert w["approval_rate"] is None


def test_backdated_drafts_land_in_earlier_week(tmp_path: Path):
    db = tmp_path / "vantage.db"
    store = SQLiteStore(db)
    gate = ApprovalGate(store, "alice")

    _submit_quick(gate, "old1")
    gate.approve("old1", edited_payload={"message": "e"})
    # Shift both timestamps into the window 10 days ago.
    ten_days = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
    _backdate_created(db, "old1", ten_days)
    _backdate_reviewed(db, "old1", ten_days)

    _submit_quick(gate, "new1")
    gate.approve("new1")

    report = compute_report(sqlite_path=db, weeks=4)
    # old1 is ~10 days back: should fall in the 2nd-newest bucket.
    assert report.windows[-1].approved == 1  # new1
    assert report.windows[-2].approved == 1  # old1
    assert report.windows[-2].approved_with_edit == 1
    # Edit rate in that older week is 100%.
    assert report.windows[-2].edit_rate == 1.0
    # Current week has no edits.
    assert report.windows[-1].edit_rate == 0.0


def test_scrubber_hits_aggregated_across_window(tmp_path: Path):
    db = tmp_path / "vantage.db"
    SQLiteStore(db)  # ensures digest_drafts table exists
    hits = ScrubberHitStore(db)
    # Writes from separate scrubber passes land at distinct timestamps.
    # (The scrubber_hits PK is (kind, occurred_at, file_path), so
    # same-millisecond duplicates collapse — don't rely on that here.)
    files = ["src/app.py", "src/config.py", "src/ci.yaml"]
    kinds = ["aws_access_key", "aws_access_key", "github_token"]
    for i, (fp, kind) in enumerate(zip(files, kinds)):
        ctx = RedactedContext(
            original_length=100,
            scrubbed_text="REDACTED",
            file_path=fp,
            redactions=[Redaction(kind=kind, placeholder=f"<R{i}>", position=0)],
        )
        hits.record_context(ctx)

    report = compute_report(sqlite_path=db, weeks=4)
    assert report.scrubber_total == 3
    assert report.scrubber_hits["aws_access_key"] == 2
    assert report.scrubber_hits["github_token"] == 1


def test_fresh_install_returns_zeroes_without_crashing(tmp_path: Path):
    """No SQLite file yet — the report should still render with all zeroes."""
    db = tmp_path / "vantage.db"
    assert not db.exists()

    report = compute_report(sqlite_path=db, weeks=3)
    assert len(report.windows) == 3
    assert report.pending == 0
    assert report.totals == {
        "drafts_created": 0,
        "approved": 0,
        "rejected": 0,
        "approved_with_edit": 0,
    }
    assert report.scrubber_total == 0


def test_weeks_clamped_to_at_least_one(tmp_path: Path):
    db = tmp_path / "vantage.db"
    report = compute_report(sqlite_path=db, weeks=0)
    assert report.weeks == 1
    assert len(report.windows) == 1


# ── Renderers ──────────────────────────────────────────────────────────────


def test_markdown_render_has_three_sections_and_no_content(tmp_path: Path):
    db = tmp_path / "vantage.db"
    store = SQLiteStore(db)
    gate = ApprovalGate(store, "alice")

    _submit_quick(gate, "a1")
    gate.approve("a1", edited_payload={"message": "secret-content-should-not-appear"})

    report = compute_report(sqlite_path=db, weeks=2)
    md = render_markdown(report)

    assert "## Summary" in md
    assert "## Weekly trend" in md
    assert "## Scrubber hits" in md
    # Ensure no payload content leaked.
    assert "secret-content-should-not-appear" not in md
    assert "msg a1" not in md


def test_json_render_round_trips_and_has_stable_shape(tmp_path: Path):
    db = tmp_path / "vantage.db"
    store = SQLiteStore(db)
    gate = ApprovalGate(store, "alice")
    _submit_quick(gate, "a1")
    gate.approve("a1")

    report = compute_report(sqlite_path=db, weeks=2)
    payload = json.loads(render_json(report))

    assert set(payload) == {"generated_at", "weeks", "pending", "totals", "scrubber_hits", "windows"}
    assert payload["weeks"] == 2
    assert set(payload["totals"]) >= {"approved", "rejected", "approval_rate", "edit_rate"}
    assert set(payload["scrubber_hits"]) == {"total", "by_kind"}
    assert len(payload["windows"]) == 2
    for w in payload["windows"]:
        assert set(w) >= {"week_start", "approved", "rejected", "approved_with_edit", "edit_rate"}


# ── CLI wiring ─────────────────────────────────────────────────────────────


@pytest.fixture
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VANTAGE_HOME", str(tmp_path))
    # Drop API-key env vars so load() doesn't need them.
    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY",
              "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    # Write a minimal config.
    (tmp_path / "config.toml").write_text(
        'developer_id = "alice"\n'
        'display_name = "Alice"\n'
        'session_dirs = []\n\n'
        '[llm]\nprovider = ""\nmodel = ""\napi_key_env = ""\n',
        encoding="utf-8",
    )
    return tmp_path


def test_cli_stats_markdown(_isolated_home: Path):
    from ghost_daemon.__main__ import app
    result = runner.invoke(app, ["stats", "--weeks", "2"])
    assert result.exit_code == 0, result.output
    assert "Vantage stats" in result.output
    assert "## Summary" in result.output


def test_cli_stats_json(_isolated_home: Path):
    from ghost_daemon.__main__ import app
    result = runner.invoke(app, ["stats", "--weeks", "2", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["weeks"] == 2
    assert "windows" in payload


def test_cli_stats_default_weeks_is_four(_isolated_home: Path):
    from ghost_daemon.__main__ import app
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["weeks"] == 4
    assert len(payload["windows"]) == 4


def test_window_stats_dataclass_shape():
    """WindowStats.to_dict is stable — manager agent tools may consume it."""
    w = WindowStats(
        week_start="2026-11-03",
        week_end="2026-11-10",
        drafts_created=3,
        approved=2,
        rejected=1,
        approved_with_edit=1,
    )
    assert w.edit_rate == 0.5
    assert abs(w.approval_rate - (2 / 3)) < 1e-9
    d = w.to_dict()
    assert d["week_start"] == "2026-11-03"
    assert d["edit_rate"] == 0.5


def test_report_dataclass_defaults():
    """StatsReport should allow construction without scrubber data."""
    r = StatsReport(
        generated_at="2026-11-10T00:00:00",
        weeks=1,
        windows=[],
        pending=0,
        totals={"approved": 0, "rejected": 0, "approved_with_edit": 0, "drafts_created": 0},
    )
    assert r.scrubber_total == 0
    assert r.scrubber_hits == {}
