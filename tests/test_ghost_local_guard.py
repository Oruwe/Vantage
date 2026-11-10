"""The ghost UI must only answer requests addressed to this machine.

Reproduces the DNS-rebinding chain found in review: a page on a foreign
origin whose hostname resolves to 127.0.0.1 could list pending drafts, read
them, and approve one with attacker-written text. The browser still sends the
attacker's hostname in ``Host`` and ``Origin`` — that is what the guard keys on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vantage.schemas import DraftPayload, QuickNote

EVIL = "http://evil.example:7474"
LOCAL = "http://127.0.0.1:7474"


@pytest.fixture
def ghost(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    cfg = Config(developer_id="alice", llm=LLMConfig(provider="groq"))
    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")
    app = build_app(
        store, gate, AuditLog(tmp_path / "audit", retention_days=7),
        SubscriberRegistry(tmp_path / "vantage.db"), cfg,
    )
    draft_id = gate.submit(DraftPayload(
        artifact_type="quick_note", developer_id="alice",
        payload=QuickNote(message="Working on the auth refactor"),
    ))
    return app, store, draft_id


def _forged() -> dict:
    return {"edited_json": json.dumps({"message": "ATTACKER-WRITTEN TEXT", "context": ""})}


# ── Foreign Host (DNS rebinding) ────────────────────────────────────────────

def test_foreign_host_cannot_list_pending_drafts(ghost):
    app, _store, _draft_id = ghost
    r = TestClient(app, base_url=EVIL).get("/api/pending-drafts")
    assert r.status_code == 400


def test_foreign_host_cannot_read_draft(ghost):
    app, _store, draft_id = ghost
    r = TestClient(app, base_url=EVIL).get(f"/draft/{draft_id}")
    assert r.status_code == 400
    assert "auth refactor" not in r.text


def test_foreign_host_cannot_approve_with_own_text(ghost):
    app, store, draft_id = ghost
    r = TestClient(app, base_url=EVIL).post(
        f"/draft/{draft_id}/approve", data=_forged(),
        headers={"Origin": EVIL}, follow_redirects=False,
    )
    assert r.status_code == 400
    row = store.get_draft(draft_id)
    assert row.status == "pending"
    assert row.edited_payload_json is None


# ── Foreign Origin on a local Host (classic CSRF) ───────────────────────────

@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_cross_site_post_is_rejected(ghost, origin):
    app, store, draft_id = ghost
    r = TestClient(app, base_url=LOCAL).post(
        f"/draft/{draft_id}/approve", data=_forged(),
        headers={"Origin": origin}, follow_redirects=False,
    )
    assert r.status_code == 403
    assert store.get_draft(draft_id).status == "pending"


# ── Legitimate local use keeps working ──────────────────────────────────────

@pytest.mark.parametrize("base", [LOCAL, "http://localhost:7474", "http://[::1]:7474"])
def test_loopback_hosts_are_served(ghost, base):
    app, _store, _draft_id = ghost
    r = TestClient(app, base_url=base).get("/api/pending-drafts")
    assert r.status_code == 200


def test_same_origin_approve_still_works(ghost):
    app, store, draft_id = ghost
    r = TestClient(app, base_url=LOCAL).post(
        f"/draft/{draft_id}/approve", data={"edited_json": ""},
        headers={"Origin": LOCAL}, follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert store.get_draft(draft_id).status == "approved"


def test_cli_post_without_origin_is_allowed(ghost):
    """curl / scripts send no Origin header; browsers always do cross-site."""
    app, store, draft_id = ghost
    r = TestClient(app, base_url=LOCAL).post(
        f"/draft/{draft_id}/approve", data={"edited_json": ""}, follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert store.get_draft(draft_id).status == "approved"


def test_configured_extra_host_is_served(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig, UIConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    cfg = Config(
        developer_id="alice", llm=LLMConfig(provider="groq"),
        ui=UIConfig(allowed_hosts=["devbox.lan"]),
    )
    store = SQLiteStore(tmp_path / "vantage.db")
    app = build_app(
        store, ApprovalGate(store, "alice"), AuditLog(tmp_path / "audit", retention_days=7),
        SubscriberRegistry(tmp_path / "vantage.db"), cfg,
    )
    assert TestClient(app, base_url="http://devbox.lan:7474").get("/api/pending-drafts").status_code == 200
    assert TestClient(app, base_url=EVIL).get("/api/pending-drafts").status_code == 400
