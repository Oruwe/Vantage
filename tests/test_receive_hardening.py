"""/v1/receive must require the body signature and must not re-notify on replays.

Reproduces two gaps found in review:
  * a push carrying only the bearer token (no X-Vantage-Signature) was accepted;
  * resending the same push fired Slack/email/webhook notifications again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from shadow_replica.app import build_app
from shadow_replica.config import (
    EmbeddingsConfig, LLMConfig, QdrantConfig, ReplicaConfig, TrustedDev,
)
from shadow_replica.notify.notifier import NotificationConfig

TOKEN = "vsub_" + secrets.token_hex(16)


@pytest.fixture
def notified(monkeypatch) -> list[str]:
    import shadow_replica.api.receive as receive

    calls: list[str] = []

    async def _fake_dispatch(_configs, **kw):
        calls.append(kw["artifact_type"])

    monkeypatch.setattr(receive, "dispatch_all", _fake_dispatch)
    return calls


@pytest.fixture
def client(notified):
    cfg = ReplicaConfig(
        replica_id="hardening-test",
        manager_token="vsm_" + secrets.token_hex(8),
        trusted_devs=[TrustedDev(
            developer_id="alice", name="Alice", token_value=TOKEN, subscriber_id="sub_alice",
        )],
        notifications=[NotificationConfig(type="slack_webhook", url="http://127.0.0.1:1/unused")],
        qdrant=QdrantConfig(enabled=False, url="http://127.0.0.1:1"),
        embeddings=EmbeddingsConfig(provider="ollama", model="nomic", endpoint="http://127.0.0.1:1"),
        llm=LLMConfig(provider=""),
    )
    with TestClient(build_app(cfg)) as c:
        yield c


def _body(approval_id: str = "appr-1", message: str = "shipped dark mode") -> bytes:
    return json.dumps({
        "approval_id": approval_id,
        "draft_id": f"draft-{approval_id}",
        "artifact_type": "quick_note",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"message": message},
    }).encode()


def _headers(body: bytes, *, signed: bool = True) -> dict:
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if signed:
        h["X-Vantage-Signature"] = "sha256=" + hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()
    return h


def test_unsigned_push_is_rejected(client, notified):
    body = _body()
    r = client.post("/v1/receive", content=body, headers=_headers(body, signed=False))
    assert r.status_code == 401
    assert notified == []


def test_signed_push_is_accepted(client, notified):
    body = _body()
    r = client.post("/v1/receive", content=body, headers=_headers(body))
    assert r.status_code == 201
    assert notified == ["quick_note"]


def test_replayed_push_does_not_notify_twice(client, notified):
    body = _body()
    first = client.post("/v1/receive", content=body, headers=_headers(body))
    second = client.post("/v1/receive", content=body, headers=_headers(body))
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert notified == ["quick_note"]


def test_distinct_pushes_each_notify(client, notified):
    for i in range(3):
        body = _body(approval_id=f"appr-{i}")
        assert client.post("/v1/receive", content=body, headers=_headers(body)).status_code == 201
    assert len(notified) == 3


def test_replay_does_not_overwrite_stored_content(client):
    original = _body(message="original")
    client.post("/v1/receive", content=original, headers=_headers(original))
    changed = _body(message="changed later")  # same approval_id
    r = client.post("/v1/receive", content=changed, headers=_headers(changed))
    assert r.json()["status"] == "duplicate"
    timeline = client.get("/v1/timeline/alice", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    messages = [item["payload"]["message"] for item in timeline["items"]]
    assert messages == ["original"]


def test_unsigned_get_endpoints_still_work(client):
    """The collision poller and timeline reads don't sign; only /v1/receive requires it."""
    r = client.get("/v1/collisions/for-me", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
