"""Boot-level integration smoke test.

Instantiates the real shadow-replica FastAPI app via `build_app`, then uses
FastAPI's TestClient to exercise every major endpoint added this session.

Auth model (per shadow_replica/auth/token_verify.py):
  - Authorization: Bearer <subscriber_token_value>   (required)
  - X-Vantage-Signature: sha256=<hex HMAC-SHA256(token, body)>  (optional)

Manager-side (ui_api, wiretap):
  - Authorization: Bearer <manager_token>, or ?token=<manager_token> on SSE

The point is to catch wiring/import/serialization regressions quickly. No real
network, no real daemons. SSE stream endpoint is accessed via `client.stream()`
so the body never has to be drained (otherwise the TestClient hangs waiting
for a response that never closes).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from shadow_replica.app import build_app
from shadow_replica.config import (
    EmbeddingsConfig, LLMConfig, QdrantConfig, ReplicaConfig, TrustedDev,
)


MGR_TOKEN = "vsm_integration_" + secrets.token_hex(8)
DEV_ID = "alice"
DEV_SUB_TOKEN = "vsub_" + secrets.token_hex(16)
DEV_SUB_ID = "sub_alice"


@pytest.fixture
def client():
    cfg = ReplicaConfig(
        replica_id="integration-test",
        manager_token=MGR_TOKEN,
        trusted_devs=[
            TrustedDev(
                developer_id=DEV_ID,
                name="Alice",
                token_value=DEV_SUB_TOKEN,
                subscriber_id=DEV_SUB_ID,
            ),
        ],
        notifications=[],
        # Point qdrant at a port that refuses connections *fast* — the
        # ReceivedStore probe then fails immediately and falls back to the
        # in-memory list instead of waiting out a 5s TCP timeout per test.
        qdrant=QdrantConfig(enabled=False, url="http://127.0.0.1:1"),
        embeddings=EmbeddingsConfig(provider="ollama", model="nomic", endpoint="http://127.0.0.1:1"),
        llm=LLMConfig(provider=""),  # no LLM → deterministic fallbacks
    )
    app = build_app(cfg)
    with TestClient(app) as c:
        yield c


# ── Helpers ─────────────────────────────────────────────────────────────────

def _mgr():
    return {"Authorization": f"Bearer {MGR_TOKEN}"}


def _dev_headers(body: bytes) -> dict:
    sig = "sha256=" + _hmac.new(
        DEV_SUB_TOKEN.encode(), body, hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"Bearer {DEV_SUB_TOKEN}",
        "X-Vantage-Signature": sig,
        "Content-Type": "application/json",
    }


def _post_artifact(client, artifact_type: str, payload: dict, approval_id: str) -> None:
    body_obj = {
        "approval_id": approval_id,
        "draft_id": f"draft-{approval_id}",
        "artifact_type": artifact_type,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    raw = json.dumps(body_obj).encode("utf-8")
    r = client.post("/v1/receive", content=raw, headers=_dev_headers(raw))
    assert r.status_code == 201, (r.status_code, r.text)


# ── Public endpoints ────────────────────────────────────────────────────────

def test_health_reports_trusted_dev_count(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["trusted_devs"] == 1


def test_metrics_is_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    # Prometheus-format bodies always include at least one TYPE line
    text = r.text
    assert "# TYPE" in text or "# HELP" in text


# ── Manager auth gate ───────────────────────────────────────────────────────

def test_ui_api_rejects_requests_without_manager_token(client):
    for path in (
        "/ui/api/status",
        "/ui/api/developers",
        "/ui/api/activity",
        "/ui/api/friction",
        "/ui/api/brief",
        "/ui/api/initiatives",
        "/ui/api/resolutions",
        "/ui/api/wiretap/recent",
        "/ui/api/stats",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should require auth"


def test_wiretap_stream_rejects_bad_query_token(client):
    with client.stream("GET", "/ui/api/wiretap/stream?token=vsm_wrong") as r:
        assert r.status_code == 401


def test_wiretap_stream_accepts_valid_query_token(client):
    # EventSource can't set headers in browsers, so the endpoint must honor ?token=.
    # Use client.stream() so we never try to drain the forever-open SSE body;
    # we only need to confirm the response starts with the right status + type.
    with client.stream("GET", f"/ui/api/wiretap/stream?token={MGR_TOKEN}") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")


# ── Baseline shape (fresh store) ────────────────────────────────────────────

def test_status_developers_activity_fresh_store(client):
    r = client.get("/ui/api/status", headers=_mgr())
    assert r.status_code == 200
    body = r.json()
    assert body["total_devs"] == 0  # nothing pushed yet
    assert body["online_devs"] == []

    r = client.get("/ui/api/developers", headers=_mgr())
    assert r.status_code == 200
    devs = r.json()
    assert len(devs) == 1  # trusted_devs always surface even with no traffic
    assert devs[0]["developer_id"] == DEV_ID
    assert devs[0]["is_online"] is False

    r = client.get("/ui/api/activity", headers=_mgr())
    assert r.status_code == 200
    assert r.json() == []


# ── Wiretap gets populated by a manager-initiated probe to an offline dev ──

def test_offline_probe_populates_wiretap(client):
    r = client.get("/ui/api/wiretap/recent", headers=_mgr())
    assert r.status_code == 200
    assert r.json() == []

    r = client.post("/ui/api/probe/" + DEV_ID, headers=_mgr())
    assert r.status_code == 200

    r = client.get("/ui/api/wiretap/recent", headers=_mgr())
    envs = r.json()
    intents = [e["intent"] for e in envs]
    assert "probe_request" in intents
    assert any(e["intent"] == "probe_response" and e["status"] == "offline" for e in envs), intents


# ── End-to-end push → initiatives → brief → search → query ─────────────────

def test_full_pipeline_push_then_initiatives_and_brief(client):
    _post_artifact(client, "commit_report", {
        "developer_id": DEV_ID,
        "repo": "vantage",
        "commit_sha": "a" * 40,
        "branch": "payments",
        "diff_stat": "3 files",
        "files_touched": ["webhooks/stripe_payments.py"],
        "author_msg": "stripe webhook signature verification for payments",
        "generated_summary": "add stripe signature header check on payments webhook",
    }, approval_id="app-1")
    _post_artifact(client, "commit_report", {
        "developer_id": DEV_ID,
        "repo": "vantage",
        "commit_sha": "b" * 40,
        "branch": "payments",
        "diff_stat": "2 files",
        "files_touched": ["webhooks/stripe_payments.py"],
        "author_msg": "stripe webhook idempotency keys for payments",
        "generated_summary": "stripe payments webhook replay protection via idempotency",
    }, approval_id="app-2")
    _post_artifact(client, "standup", {
        "yesterday": ["set up stripe webhook payments test harness"],
        "today": ["finishing stripe payments webhook retry policy"],
        "blockers": [],
    }, approval_id="app-3")

    r = client.get("/ui/api/activity", headers=_mgr())
    assert r.status_code == 200
    assert len(r.json()) == 3

    r = client.get(f"/ui/api/timeline/{DEV_ID}", headers=_mgr())
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    assert {it["channel"] for it in items} >= {"commit_report", "standup"}

    r = client.get("/ui/api/initiatives?days=7", headers=_mgr())
    assert r.status_code == 200
    data = r.json()
    assert data["artifact_count"] == 3
    inits = data["initiatives"]
    assert len(inits) == 1, [i["label"] for i in inits]
    it = inits[0]
    assert DEV_ID in it["contributors"]
    assert it["commit_count"] == 2
    assert any(
        kw in t.lower()
        for t in it["top_terms"]
        for kw in ("stripe", "payments", "webhook")
    )

    r = client.get("/ui/api/brief?hours=72", headers=_mgr())
    assert r.status_code == 200
    brief = r.json()
    assert brief["developer_count"] == 1
    assert brief["artifact_count"] >= 3
    assert any("stripe" in s.lower() for s in brief["shipped"])
    assert any(
        "retry" in s.lower() or "stripe" in s.lower()
        for s in brief["in_flight"]
    )

    # /search semantic path depends on Qdrant + embedder being live. In this
    # integration test both are deliberately disabled so we only verify the
    # endpoint responds with the right shape, not that it returns hits.
    r = client.get("/ui/api/search?q=stripe", headers=_mgr())
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "stripe"
    assert isinstance(body["results"], list)

    r = client.post(
        "/ui/api/query",
        json={"query": "what did alice ship on stripe?"},
        headers=_mgr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert "answer" in body
    assert body["sources_used"] >= 1


def test_resolutions_endpoint_filters_by_channel(client):
    _post_artifact(client, "bug_resolution", {
        "resolution_id": "res-1",
        "developer_id": DEV_ID,
        "error_signature": "cannot find module 'foo'",
        "error_sample": "Error: cannot find module 'foo' at line 10",
        "attempt_count": 3,
        "fix_summary": "Edit on package.json",
        "fix_details": "added foo@1.2.3 to dependencies",
        "files_touched": ["package.json"],
        "tools_used": ["Edit", "Bash"],
        "repo": "vantage",
    }, approval_id="res-app-1")

    r = client.get("/ui/api/resolutions", headers=_mgr())
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["resolution"]["fix_summary"] == "Edit on package.json"

    r = client.get("/ui/api/resolutions?q=foo", headers=_mgr())
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 1

    r = client.get("/ui/api/resolutions?q=unlikely_nonsense_xyz_qq", headers=_mgr())
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_converse_offline_dev_returns_offline_result(client):
    r = client.post(
        "/ui/api/converse",
        json={"prompt": "who is on stripe?", "developer_ids": [DEV_ID]},
        headers=_mgr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["prompt"]
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["developer_id"] == DEV_ID
    assert result["status"] == "offline"


def test_friction_endpoint_empty_before_any_report(client):
    r = client.get("/ui/api/friction", headers=_mgr())
    assert r.status_code == 200
    assert r.json() == {}


def test_initiatives_detail_404_for_unknown_id(client):
    r = client.get("/ui/api/initiatives/this-does-not-exist", headers=_mgr())
    assert r.status_code == 404


def test_stats_endpoint_shape(client):
    r = client.get("/ui/api/stats", headers=_mgr())
    assert r.status_code == 200
    data = r.json()
    assert "total_artifacts" in data
    assert "total_developers" in data
    assert data["storage_backend"] == "qdrant"


def test_sparklines_endpoint_shape(client):
    r = client.get("/ui/api/sparklines?days=7", headers=_mgr())
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_wiretap_recent_bounded_by_limit(client):
    # Even an empty store honors ?limit=
    r = client.get("/ui/api/wiretap/recent?limit=1", headers=_mgr())
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) <= 1
