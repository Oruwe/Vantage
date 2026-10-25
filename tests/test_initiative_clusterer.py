"""Tests for the Initiative clusterer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from shadow_replica.initiatives.clusterer import cluster_recent, extract_text


class _Row:
    def __init__(self, developer_id, artifact_type, payload, received_at):
        self.developer_id = developer_id
        self.artifact_type = artifact_type
        self.channel = artifact_type
        self.payload_json = json.dumps(payload)
        self.approval_id = f"app-{developer_id}-{received_at}"
        self.received_at = received_at
        self.approved_at = received_at
        self.draft_id = f"d-{received_at}"


def _row(dev, atype, payload, when):
    return _Row(dev, atype, payload, when.isoformat())


@pytest.mark.anyio
async def test_single_cluster_from_similar_commits():
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row("alice", "commit_report", {"author_msg": "stripe webhook signature verification for payments endpoint", "generated_summary": "Add stripe signature header check to payments webhook handler", "files_touched": ["webhooks/stripe_payments.py"]}, now - timedelta(hours=5)),
        _row("bob", "commit_report", {"author_msg": "stripe webhook idempotency key handling for payments", "generated_summary": "Store stripe idempotency keys on payments webhook to prevent replay", "files_touched": ["webhooks/stripe_payments.py"]}, now - timedelta(hours=3)),
        _row("carl", "standup", {"today": ["finishing stripe webhook retry logic and signature handling for payments"], "yesterday": [], "blockers": []}, now - timedelta(hours=1)),
    ]
    inits = await cluster_recent(
        rows, dev_name_map={"alice": "Alice", "bob": "Bob", "carl": "Carl"},
        period_start=now - timedelta(days=7), period_end=now,
    )
    # All three mention stripe/webhook — should land in one cluster
    assert len(inits) == 1
    it = inits[0]
    assert set(it.contributors) == {"alice", "bob", "carl"}
    assert it.commit_count == 2
    assert "stripe" in " ".join(it.top_terms).lower() or "webhook" in " ".join(it.top_terms).lower()
    assert any("signature" in s.lower() for s in it.shipped) or any("idempotency" in s.lower() for s in it.shipped)
    assert any("retry" in s.lower() for s in it.in_flight)


@pytest.mark.anyio
async def test_disjoint_topics_produce_distinct_initiatives():
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row("alice", "commit_report", {"author_msg": "stripe webhook signature verification for payments", "generated_summary": "add stripe signature header check to payments webhook"}, now - timedelta(hours=4)),
        _row("alice", "commit_report", {"author_msg": "stripe webhook idempotency keys for payments", "generated_summary": "stripe webhook replay guard with payments idempotency"}, now - timedelta(hours=3)),
        _row("bob", "commit_report", {"author_msg": "grafana observability dashboard for latency metrics", "generated_summary": "add grafana latency panel for observability"}, now - timedelta(hours=2)),
        _row("bob", "commit_report", {"author_msg": "prometheus observability metrics for latency", "generated_summary": "export prometheus latency metrics for observability dashboard"}, now - timedelta(hours=1)),
    ]
    inits = await cluster_recent(
        rows, dev_name_map={"alice": "Alice", "bob": "Bob"},
        period_start=now - timedelta(days=7), period_end=now,
    )
    assert len(inits) == 2
    # Each initiative is single-contributor in this scenario
    for it in inits:
        assert len(it.contributors) == 1


@pytest.mark.anyio
async def test_blockers_set_risk_flag():
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row("alice", "standup", {
            "today": ["debugging checkout flow"],
            "yesterday": ["checkout smoke tests"],
            "blockers": ["payment sandbox credentials expired"],
        }, now - timedelta(hours=2)),
    ]
    inits = await cluster_recent(
        rows, dev_name_map={"alice": "Alice"},
        period_start=now - timedelta(days=7), period_end=now,
    )
    assert len(inits) == 1
    assert inits[0].risk_flag is True
    assert any("sandbox" in b.lower() or "credential" in b.lower() for b in inits[0].blockers)


@pytest.mark.anyio
async def test_momentum_rewards_recency_and_contributors():
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Older, single-contributor initiative
    old = [
        _row("alice", "commit_report", {"author_msg": "legacy auth cleanup", "generated_summary": "remove legacy auth"}, now - timedelta(days=5)),
    ]
    # Fresh, multi-contributor initiative
    fresh = [
        _row("alice", "commit_report", {"author_msg": "payments webhook signature verification stripe", "generated_summary": "verify stripe webhook signature on payments endpoint"}, now - timedelta(hours=2)),
        _row("bob", "commit_report", {"author_msg": "payments webhook idempotency guard stripe", "generated_summary": "stripe payments webhook replay protection via idempotency keys"}, now - timedelta(hours=1)),
        _row("carl", "standup", {"today": ["payments webhook retry policy stripe signature"], "yesterday": [], "blockers": []}, now - timedelta(minutes=30)),
    ]
    inits = await cluster_recent(
        old + fresh, dev_name_map={"alice": "Alice", "bob": "Bob", "carl": "Carl"},
        period_start=now - timedelta(days=14), period_end=now,
    )
    # Results sorted hottest-first
    assert len(inits) == 2
    assert inits[0].momentum_score > inits[1].momentum_score
    assert "payments" in inits[0].label.lower() or "webhook" in inits[0].label.lower() or "signature" in inits[0].label.lower()


@pytest.mark.anyio
async def test_timeline_is_chronological_and_tagged():
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row("alice", "commit_report", {"author_msg": "auth refactor part 1", "generated_summary": "split middleware"}, now - timedelta(hours=4)),
        _row("alice", "standup", {"today": ["auth refactor part 2 WIP"], "blockers": []}, now - timedelta(hours=1)),
    ]
    inits = await cluster_recent(
        rows, dev_name_map={"alice": "Alice"},
        period_start=now - timedelta(days=7), period_end=now,
    )
    assert len(inits) == 1
    tl = inits[0].timeline
    assert len(tl) >= 2
    # Earliest first
    assert tl[0].at <= tl[-1].at
    kinds = {ev.kind for ev in tl}
    assert "shipped" in kinds and "in_flight" in kinds


def test_extract_text_handles_each_artifact_type():
    assert "stripe" in extract_text("commit_report", {"author_msg": "stripe webhook", "generated_summary": ""}).lower()
    assert "payment" in extract_text("standup", {"today": ["payment work"], "yesterday": [], "blockers": []}).lower()
    assert "login" in extract_text("pr_draft", {"title": "add login", "summary": ""}).lower()
    assert "retry" in extract_text("weekly_digest", {"highlights": ["retry queue added"]}).lower()
    assert extract_text("friction_report", {}) == ""  # unknown types get fallback — nothing promising = empty
