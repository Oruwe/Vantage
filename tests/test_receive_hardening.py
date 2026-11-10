"""Regression tests for P1: /v1/receive now REQUIRES a body signature and
short-circuits duplicate pushes before firing notifications again.

The receive path is wired via `build_app`, same as the full integration test,
so we exercise the real signature verification and the real ReceivedStore
dedup check (with Qdrant disabled → in-memory fallback).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import shadow_replica.api.receive as receive_mod
from shadow_replica.app import build_app
from shadow_replica.config import (
    EmbeddingsConfig, LLMConfig, NotificationConfig, QdrantConfig, ReplicaConfig, TrustedDev,
)


MGR_TOKEN = "vsm_hardening_" + secrets.token_hex(8)
DEV_ID = "alice"
DEV_SUB_TOKEN = "vsub_" + secrets.token_hex(16)
DEV_SUB_ID = "sub_alice"


@pytest.fixture
def client_and_dispatch(monkeypatch):
    calls: list[dict] = []

    async def fake_dispatch_all(notifications, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(receive_mod, "dispatch_all", fake_dispatch_all)

    cfg = ReplicaConfig(
        replica_id="hardening-test",
        manager_token=MGR_TOKEN,
        trusted_devs=[
            TrustedDev(
                developer_id=DEV_ID,
                name="Alice",
                token_value=DEV_SUB_TOKEN,
                subscriber_id=DEV_SUB_ID,
            ),
        ],
        # One dummy notification — presence is enough; dispatch_all is mocked.
        notifications=[NotificationConfig(type="generic_webhook", url="http://127.0.0.1:1/x")],
        qdrant=QdrantConfig(enabled=False, url="http://127.0.0.1:1"),
        embeddings=EmbeddingsConfig(provider="ollama", model="nomic", endpoint="http://127.0.0.1:1"),
        llm=LLMConfig(provider=""),
    )
    app = build_app(cfg)
    with TestClient(app) as c:
        yield c, calls


def _signed_headers(body: bytes) -> dict:
    sig = "sha256=" + _hmac.new(
        DEV_SUB_TOKEN.encode(), body, hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"Bearer {DEV_SUB_TOKEN}",
        "X-Vantage-Signature": sig,
        "Content-Type": "application/json",
    }


def _unsigned_headers() -> dict:
    return {
        "Authorization": f"Bearer {DEV_SUB_TOKEN}",
        "Content-Type": "application/json",
    }


def _artifact_body(approval_id: str = "app-1") -> bytes:
    return json.dumps({
        "approval_id": approval_id,
        "draft_id": f"draft-{approval_id}",
        "artifact_type": "quick_note",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"message": "hello", "branch": "main"},
    }).encode("utf-8")


# ── Signature now required ─────────────────────────────────────────────────

def test_unsigned_push_is_rejected(client_and_dispatch):
    client, calls = client_and_dispatch
    body = _artifact_body()
    r = client.post("/v1/receive", content=body, headers=_unsigned_headers())
    assert r.status_code == 401
    assert "signature" in r.json().get("detail", "").lower()
    assert calls == []


def test_signed_push_is_accepted(client_and_dispatch):
    client, calls = client_and_dispatch
    body = _artifact_body()
    r = client.post("/v1/receive", content=body, headers=_signed_headers(body))
    assert r.status_code == 201
    assert r.json()["status"] == "ok"
    assert len(calls) == 1


def test_bad_signature_is_rejected(client_and_dispatch):
    client, calls = client_and_dispatch
    body = _artifact_body()
    headers = _signed_headers(body)
    headers["X-Vantage-Signature"] = "sha256=" + "0" * 64
    r = client.post("/v1/receive", content=body, headers=headers)
    assert r.status_code == 401
    assert calls == []


# ── Duplicate push is a no-op ─────────────────────────────────────────────

def test_duplicate_push_is_idempotent_and_skips_notifications(client_and_dispatch):
    client, calls = client_and_dispatch
    body = _artifact_body(approval_id="dup-1")

    first = client.post("/v1/receive", content=body, headers=_signed_headers(body))
    assert first.status_code == 201
    assert first.json()["status"] == "ok"
    assert len(calls) == 1

    second = client.post("/v1/receive", content=body, headers=_signed_headers(body))
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "approval_id": "dup-1"}

    assert len(calls) == 1, f"notifications fired {len(calls)} times, expected once"
