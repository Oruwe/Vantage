"""Tests for the policy counter-offer path in ProbeAgent."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ghost_daemon.agents.probe_agent import ProbeAgent
from ghost_daemon.approval.policy import save_policy
from ghost_daemon.storage.sqlite_store import DigestDraftRow, SQLiteStore
from vantage.schemas import Channel, PolicyDecision, ProbeRequest, RedactionLevel, SharingPolicy


class _FakeTailer:
    async def tail_recent(self, path, n=15):
        return []


class _FakeCfg:
    def __init__(self, vantage_dir: Path):
        self.developer_id = "dev-under-test"
        self.vantage_dir = vantage_dir
        self.resolved_session_dirs: list[Path] = []


def _write_policy(vantage_dir: Path, subscriber_id: str, **overrides) -> None:
    defaults = {
        "channels": [Channel.WEEKLY_DIGEST],   # LIVE_PROBE deliberately NOT included
        "redaction_level": RedactionLevel.STANDARD,
        "auto_approve": False,
    }
    defaults.update(overrides)
    policy = SharingPolicy(subscriber_id=subscriber_id, **defaults)
    save_policy(policy, vantage_dir)


def _seed_approved_commit(store: SQLiteStore, when: datetime, msg: str) -> None:
    """Insert an already-approved commit_report row the way the gate would."""
    row = DigestDraftRow(
        id=f"draft-{when.timestamp()}",
        developer_id="dev-under-test",
        artifact_type="commit_report",
        period_start=when.isoformat(),
        period_end=when.isoformat(),
        payload_json=json.dumps({"author_msg": msg, "branch": "feature/x", "generated_summary": msg}),
        status="approved",
        created_at=when.isoformat(),
        reviewed_at=when.isoformat(),
        approval_source="manual",
    )
    from sqlmodel import Session
    with Session(store._engine) as s:
        s.add(row); s.commit()


@pytest.mark.anyio
async def test_denied_probe_returns_counter_offer_with_last_commit(tmp_path):
    vantage_dir = tmp_path / "vantage"
    vantage_dir.mkdir()
    sub_id = "sub-mgr"
    _write_policy(vantage_dir, sub_id)   # LIVE_PROBE channel not granted

    store = SQLiteStore(tmp_path / "ghost.sqlite")
    _seed_approved_commit(store, datetime(2026, 10, 24, 12, 0, 0), "refactor billing")

    agent = ProbeAgent(cfg=_FakeCfg(vantage_dir), llm=None, tailer=_FakeTailer(), store=store)
    resp = await agent.run(ProbeRequest(subscriber_id=sub_id))

    assert resp.policy_decision == PolicyDecision.DENIED_BY_POLICY
    assert resp.counter_offer is not None
    co = resp.counter_offer
    assert co.artifact_type == "commit_report"
    assert "refactor billing" in co.summary
    assert co.reason == "channel not enabled"


@pytest.mark.anyio
async def test_denied_probe_returns_reason_only_when_no_history(tmp_path):
    vantage_dir = tmp_path / "vantage"
    vantage_dir.mkdir()
    sub_id = "sub-mgr"
    _write_policy(vantage_dir, sub_id)

    store = SQLiteStore(tmp_path / "ghost.sqlite")  # empty

    agent = ProbeAgent(cfg=_FakeCfg(vantage_dir), llm=None, tailer=_FakeTailer(), store=store)
    resp = await agent.run(ProbeRequest(subscriber_id=sub_id))

    assert resp.policy_decision == PolicyDecision.DENIED_BY_POLICY
    assert resp.counter_offer is not None
    assert resp.counter_offer.approval_id == ""   # no artifact to point at
    assert resp.counter_offer.reason == "channel not enabled"


@pytest.mark.anyio
async def test_hours_denial_names_hours_in_reason(tmp_path):
    vantage_dir = tmp_path / "vantage"
    vantage_dir.mkdir()
    sub_id = "sub-mgr"
    # Enable LIVE_PROBE but set a 1-hour window that definitely excludes "now"
    now_hour = datetime.utcnow().hour
    narrow_start = (now_hour + 2) % 24
    narrow_end = (now_hour + 3) % 24
    _write_policy(
        vantage_dir, sub_id,
        channels=[Channel.LIVE_PROBE],
        probe_hours_start=narrow_start,
        probe_hours_end=narrow_end,
    )

    store = SQLiteStore(tmp_path / "ghost.sqlite")
    agent = ProbeAgent(cfg=_FakeCfg(vantage_dir), llm=None, tailer=_FakeTailer(), store=store)
    resp = await agent.run(ProbeRequest(subscriber_id=sub_id))

    assert resp.policy_decision == PolicyDecision.DENIED_BY_POLICY
    assert resp.counter_offer is not None
    assert "hours" in resp.counter_offer.reason
