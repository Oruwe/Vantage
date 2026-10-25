"""End-to-end tests for the dashboard's add/remove developer flow.

The flow has two shapes:
  1. Paste a `pairing_code` produced by `vantage-ghost pair` (base64url(json)).
  2. Submit raw fields directly.

Both must mutate `cfg.trusted_devs` in place so the receive and WS routers
accept the new dev without a server restart, AND persist to replica.toml.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def replica_tmp(monkeypatch):
    """Isolate replica.toml onto a temp dir so tests don't pollute APPDATA."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        monkeypatch.setattr(
            "shadow_replica.config._replica_config_dir",
            lambda: tmp,
        )
        yield tmp


@pytest.fixture
def client(replica_tmp):
    from shadow_replica.app import build_app
    from shadow_replica.config import load

    cfg = load()
    cfg.qdrant.enabled = False  # avoid any network call
    app = build_app(cfg)
    with TestClient(app) as c:
        c._cfg = cfg  # type: ignore[attr-defined]
        c._manager = cfg.manager_token
        yield c


def _auth(client):
    return {"Authorization": f"Bearer {client._manager}"}


def _pairing_code(developer_id: str, name: str, token: str, subscriber_id: str = "sub_abc123", receive_url: str = "http://alice-mac:7474") -> str:
    blob = {
        "v": 1,
        "developer_id": developer_id,
        "name": name,
        "subscriber_id": subscriber_id,
        "token": token,
        "receive_url": receive_url,
    }
    raw = json.dumps(blob, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_decode_pairing_code_returns_preview(client):
    code = _pairing_code("alice", "Alice", "vsub_fakeToken123")
    r = client.post("/ui/api/pair/decode", json={"pairing_code": code}, headers=_auth(client))
    assert r.status_code == 200
    data = r.json()
    assert data["developer_id"] == "alice"
    assert data["name"] == "Alice"
    assert data["token"] == "vsub_fakeToken123"
    assert data["subscriber_id"] == "sub_abc123"


def test_decode_rejects_garbage(client):
    r = client.post("/ui/api/pair/decode", json={"pairing_code": "not-base64-at-all"}, headers=_auth(client))
    # base64 decoding of truly non-b64 chars yields invalid JSON → 400
    assert r.status_code == 400


def test_add_dev_via_pairing_code_persists_and_mutates_live_cfg(client, replica_tmp):
    code = _pairing_code("alice", "Alice", "vsub_realtoken_abcdef")
    r = client.post("/ui/api/developers/add", json={"pairing_code": code}, headers=_auth(client))
    assert r.status_code == 200, r.text
    assert r.json()["developer_id"] == "alice"

    # Live cfg object (shared with receive/WS routers) now has Alice:
    cfg = client._cfg
    assert any(d.developer_id == "alice" and d.token_value == "vsub_realtoken_abcdef" for d in cfg.trusted_devs)

    # And replica.toml persisted:
    toml = (replica_tmp / "replica.toml").read_text(encoding="utf-8")
    assert "alice" in toml
    assert "vsub_realtoken_abcdef" in toml


def test_add_dev_via_raw_fields(client):
    r = client.post(
        "/ui/api/developers/add",
        json={"developer_id": "bob", "name": "Bob", "token": "vsub_bobsToken", "subscriber_id": "sub_xyz"},
        headers=_auth(client),
    )
    assert r.status_code == 200
    cfg = client._cfg
    bob = next(d for d in cfg.trusted_devs if d.developer_id == "bob")
    assert bob.name == "Bob"
    assert bob.subscriber_id == "sub_xyz"


def test_add_dev_rejects_missing_token(client):
    r = client.post(
        "/ui/api/developers/add",
        json={"developer_id": "carol", "name": "Carol"},
        headers=_auth(client),
    )
    assert r.status_code == 400


def test_add_dev_rejects_wrong_prefix(client):
    r = client.post(
        "/ui/api/developers/add",
        json={"developer_id": "carol", "token": "wrong_prefix_token"},
        headers=_auth(client),
    )
    assert r.status_code == 400


def test_add_dev_is_idempotent_update(client):
    code = _pairing_code("alice", "Alice", "vsub_firstToken")
    client.post("/ui/api/developers/add", json={"pairing_code": code}, headers=_auth(client))

    # Re-add with a rotated token
    code2 = _pairing_code("alice", "Alice Updated", "vsub_rotatedToken")
    r = client.post("/ui/api/developers/add", json={"pairing_code": code2}, headers=_auth(client))
    assert r.status_code == 200

    cfg = client._cfg
    alices = [d for d in cfg.trusted_devs if d.developer_id == "alice"]
    assert len(alices) == 1
    assert alices[0].token_value == "vsub_rotatedToken"
    assert alices[0].name == "Alice Updated"


def test_remove_dev(client):
    code = _pairing_code("alice", "Alice", "vsub_someToken")
    client.post("/ui/api/developers/add", json={"pairing_code": code}, headers=_auth(client))
    assert any(d.developer_id == "alice" for d in client._cfg.trusted_devs)

    r = client.delete("/ui/api/developers/alice", headers=_auth(client))
    assert r.status_code == 200
    assert not any(d.developer_id == "alice" for d in client._cfg.trusted_devs)


def test_remove_dev_404_for_unknown(client):
    r = client.delete("/ui/api/developers/ghost-not-registered", headers=_auth(client))
    assert r.status_code == 404


def test_unauthenticated_add_rejected(client):
    code = _pairing_code("eve", "Eve", "vsub_eveToken")
    r = client.post("/ui/api/developers/add", json={"pairing_code": code})
    assert r.status_code == 401


def test_added_dev_can_push_without_restart(client):
    """The hot-add contract: once added, the receive endpoint accepts them."""
    import hashlib
    import hmac as _hmac

    token = "vsub_hotAddTestToken123"
    r = client.post(
        "/ui/api/developers/add",
        json={"developer_id": "dana", "name": "Dana", "token": token},
        headers=_auth(client),
    )
    assert r.status_code == 200

    body = json.dumps({
        "approval_id": "ap_1",
        "artifact_type": "quick_note",
        "payload": {"message": "hello from dana"},
    }).encode("utf-8")
    sig = "sha256=" + _hmac.new(token.encode(), body, hashlib.sha256).hexdigest()

    r2 = client.post(
        "/v1/receive",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Vantage-Signature": sig,
        },
    )
    # The receive endpoint returns 201 Created on success; 401 would mean our
    # live-cfg mutation didn't take effect (the whole point of hot-add).
    assert r2.status_code in (200, 201), r2.text
    assert r2.status_code != 401


def test_ghost_pairing_code_roundtrips_with_decoder(client):
    """`vantage-ghost`'s encoder must produce codes the decoder accepts."""
    from ghost_daemon.__main__ import _build_pairing_code

    class _StubCfg:
        developer_id = "ella"
        display_name = "Ella"

    class _StubIdentity:
        subscriber_id = "sub_ella01"
        token_value = "vsub_ellasToken_12345"
        receive_url = "http://manager-host:7475"

    code = _build_pairing_code(_StubCfg(), _StubIdentity())
    r = client.post("/ui/api/pair/decode", json={"pairing_code": code}, headers=_auth(client))
    assert r.status_code == 200
    d = r.json()
    assert d["developer_id"] == "ella"
    assert d["token"] == "vsub_ellasToken_12345"
    assert d["subscriber_id"] == "sub_ella01"
    assert d["receive_url"] == "http://manager-host:7475"
