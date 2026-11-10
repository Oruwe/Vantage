"""Regression tests for the ghost-daemon localhost guard (P2).

Covers two attack shapes:

1. DNS rebinding — a Host header pointing at a name we don't serve is 400.
2. CSRF — a state-changing method with an off-origin (or "null") Origin
   header is 403; a missing Origin is allowed (CLI/curl).

Reuses the ghost_app fixture pattern from test_privacy_overrides.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def ghost_client(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.insights.edit_store import EditDiffStore
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
    with TestClient(app) as c:
        yield c


# ── Host header (DNS rebinding) ────────────────────────────────────────────

def test_rejects_bad_host_header(ghost_client):
    r = ghost_client.get("/", headers={"host": "evil.example"})
    assert r.status_code == 400


def test_rejects_bad_host_with_port(ghost_client):
    r = ghost_client.get("/", headers={"host": "evil.example:7474"})
    assert r.status_code == 400


def test_allows_loopback_host_with_port(ghost_client):
    r = ghost_client.get("/", headers={"host": "127.0.0.1:7474"})
    assert r.status_code == 200


def test_allows_localhost_host(ghost_client):
    r = ghost_client.get("/", headers={"host": "localhost"})
    assert r.status_code == 200


def test_allows_ipv6_loopback_host(ghost_client):
    r = ghost_client.get("/", headers={"host": "[::1]:7474"})
    assert r.status_code == 200


def test_allows_default_testclient_host(ghost_client):
    # TestClient's default Host is "testserver"; leave the header untouched.
    r = ghost_client.get("/")
    assert r.status_code == 200


# ── Origin header (CSRF) ──────────────────────────────────────────────────

def test_rejects_post_with_offsite_origin(ghost_client):
    r = ghost_client.post(
        "/draft/x/approve",
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_rejects_post_with_null_origin(ghost_client):
    r = ghost_client.post(
        "/draft/x/approve",
        headers={"origin": "null"},
    )
    assert r.status_code == 403


def test_accepts_post_with_local_origin(ghost_client):
    # The draft doesn't exist, so the route will return an error, but it
    # must NOT be the 403 the guard emits on a cross-origin write.
    r = ghost_client.post(
        "/draft/x/approve",
        headers={"origin": "http://127.0.0.1:7474"},
    )
    assert r.status_code != 403


def test_accepts_post_without_origin(ghost_client):
    # curl / CLI / server-to-server don't send Origin; must be allowed.
    r = ghost_client.post("/draft/x/approve")
    assert r.status_code != 403
