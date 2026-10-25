"""Tests for the Expert Routing classifier + reweighting."""
from __future__ import annotations

import json

from shadow_replica.agent.subscriber_agent import (
    QueryRoute, _reweight_for_why, classify_query,
)


def test_why_variants_route_to_why():
    for q in [
        "why did we pick postgres over sqlite?",
        "why is the auth middleware ripped out?",
        "rationale for the queue rewrite",
        "what motivated using k8s here?",
        "the thinking behind the retry policy?",
    ]:
        route = classify_query(q)
        assert route.kind == "why", q


def test_status_variants_route_to_status():
    for q in [
        "what are we working on",
        "any blockers on checkout?",
        "status of the migration",
    ]:
        route = classify_query(q)
        assert route.kind == "status", q


def test_general_queries_fall_through():
    r = classify_query("show me recent commits")
    assert r.kind == "general"
    assert r.channel_hint == "commit_report"
    r2 = classify_query("yesterday's standup")
    assert r2.kind == "general"
    assert r2.channel_hint == "standup"


def test_empty_query_is_general():
    r = classify_query("")
    assert r.kind == "general"
    assert r.channel_hint is None


def _row(atype: str):
    class Row: pass
    r = Row()
    r.artifact_type = atype
    r.developer_id = "x"
    r.payload_json = json.dumps({})
    r.received_at = ""
    r.approved_at = ""
    return r


def test_reweight_for_why_prefers_commits_and_pr_drafts():
    rows = [
        _row("standup"),
        _row("weekly_digest"),
        _row("commit_report"),
        _row("pr_draft"),
        _row("quick_note"),
    ]
    out = _reweight_for_why(rows)
    types = [r.artifact_type for r in out]
    # commit_report before pr_draft before weekly_digest before standup
    assert types.index("commit_report") < types.index("pr_draft")
    assert types.index("pr_draft") < types.index("weekly_digest")
    assert types.index("weekly_digest") < types.index("standup")


def test_queryroute_dataclass():
    r = QueryRoute(kind="why")
    assert r.channel_hint is None
    r2 = QueryRoute(kind="general", channel_hint="commit_report")
    assert r2.channel_hint == "commit_report"
