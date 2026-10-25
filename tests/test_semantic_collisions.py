"""Tests for Option 3 — Semantic Collision Radar.

Covers:
  - _cosine / _tokens primitives
  - Pure-vector detection: two artifacts, different devs, same meaning
  - File-overlap fallback when embeddings aren't available
  - Same-dev artifacts never collide with themselves
  - Unmapped github authors excluded
  - Dev-pair dedupe: one collision per (A,B) even with many artifacts
  - Severity assignment (high / medium / low)
  - Time-window respected (old artifacts don't match newer ones)
  - /ui/api/collisions returns the detector output
  - Brief includes `collisions` field and emits SEMANTIC_COLLISION_ALERT envelopes
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def replica_tmp(monkeypatch):
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
    from shadow_replica.config import load, TrustedDev
    cfg = load()
    cfg.qdrant.enabled = False
    cfg.trusted_devs.append(TrustedDev(developer_id="alice", name="Alice", token_value="vsub_alice"))
    cfg.trusted_devs.append(TrustedDev(developer_id="bob",   name="Bob",   token_value="vsub_bob"))
    app = build_app(cfg)
    with TestClient(app) as c:
        c._cfg = cfg
        c._manager = cfg.manager_token
        yield c


def _auth(client):
    return {"Authorization": f"Bearer {client._manager}"}


# ── Helpers ────────────────────────────────────────────────────────────────

def _store_from_client(client):
    """Pull the ReceivedStore out of any route's closure."""
    for route in _walk_routes(client.app):
        if hasattr(route, "endpoint"):
            for cell in route.endpoint.__closure__ or ():
                v = cell.cell_contents
                if hasattr(v, "save_async") and hasattr(v, "list_recent_with_vectors"):
                    return v
    raise RuntimeError("Could not resolve ReceivedStore")


def _walk_routes(container):
    for route in getattr(container, "routes", []):
        if hasattr(route, "endpoint") and hasattr(route, "path"):
            yield route
        else:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from _walk_routes(inner)


async def _save_commit(store, developer_id, approval_id, author_msg,
                        files=None, branch="main", commit_sha="",
                        vector=None, received_at=None):
    """Save a commit_report artifact with an optional pre-set vector."""
    received = received_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "author_msg": author_msg,
        "generated_summary": author_msg,
        "branch": branch,
        "commit_sha": commit_sha,
        "files_touched": files or [],
        "repo": "acme/payments",
    }
    await store.save_async(
        approval_id=approval_id,
        developer_id=developer_id,
        artifact_type="commit_report",
        draft_id="",
        channel="commit_report",
        payload_json=json.dumps(payload),
        approved_at=received,
    )
    # Overwrite the fallback row's vector so pairwise cosine has something to work with.
    for row in store._fallback:
        if row.approval_id == approval_id and vector is not None:
            # Monkey-patch: store the vector on the row for the test. The real
            # path uses Qdrant vectors; our list_recent_with_vectors fallback
            # calls _embed_row which we also stub below.
            row._test_vector = vector  # type: ignore[attr-defined]
        if row.approval_id == approval_id and received_at:
            row.received_at = received_at


def _install_vector_stub(store):
    """Stub _embed_row to return the vector attached to each row via _test_vector.

    Also sets a sentinel non-None embedder so list_recent_with_vectors takes
    the embed-each-row branch (which is where our stub lives).
    """
    class _Sentinel:
        async def embed(self, text):
            return [0.0] * 768
    store._embedder = _Sentinel()

    async def _embed(row):
        return getattr(row, "_test_vector", [0.0] * 768)
    store._embed_row = _embed


# ── Primitives ─────────────────────────────────────────────────────────────

def test_cosine_identical_vectors_is_one():
    from shadow_replica.collision.semantic import _cosine
    v = [1.0, 2.0, 3.0]
    assert abs(_cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_is_zero():
    from shadow_replica.collision.semantic import _cosine
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_handles_zero_vectors():
    from shadow_replica.collision.semantic import _cosine
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_tokens_strips_stopwords_and_short_words():
    from shadow_replica.collision.semantic import _tokens
    toks = _tokens("the auth middleware is being refactored for payments")
    assert "auth" in toks
    assert "middleware" in toks
    assert "payments" in toks
    assert "the" not in toks and "is" not in toks and "being" not in toks


# ── Detector unit tests ────────────────────────────────────────────────────

def test_semantic_match_fires_when_cosine_above_threshold():
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)

        # Two near-identical vectors, different devs, same conceptual terms
        v = [0.1] * 10 + [0.9] * 10
        await _save_commit(store, "alice", "a1", "refactor auth middleware for payment handler", vector=v)
        await _save_commit(store, "bob",   "b1", "rewrite auth session validation in payment flow", vector=v)

        det = SemanticCollisionDetector(store, threshold=0.5)
        res = await det.scan_recent(hours=48, dev_name_map={"alice": "Alice", "bob": "Bob"})
        return res

    collisions = asyncio.run(_run())
    assert len(collisions) == 1
    c = collisions[0]
    assert {c.developer_a, c.developer_b} == {"alice", "bob"}
    assert c.cosine_similarity >= 0.5
    # Shared "auth", "payment" etc show up
    assert any(t in {"auth", "payment", "payments"} for t in c.shared_terms)


def test_file_overlap_fires_even_without_embeddings():
    """Zero vectors — but shared files should still trigger a collision."""
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)

        await _save_commit(store, "alice", "a1", "wip change",
                            files=["src/auth/middleware.py"], vector=[0.0]*768)
        await _save_commit(store, "bob",   "b1", "another thing",
                            files=["src/auth/middleware.py", "src/other.py"], vector=[0.0]*768)

        det = SemanticCollisionDetector(store)
        return await det.scan_recent(hours=48)

    collisions = asyncio.run(_run())
    assert len(collisions) == 1
    assert "src/auth/middleware.py" in collisions[0].file_overlap


def test_same_developer_never_collides_with_self():
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)
        v = [1.0]*768
        await _save_commit(store, "alice", "a1", "refactor auth", vector=v)
        await _save_commit(store, "alice", "a2", "refactor auth more", vector=v)

        det = SemanticCollisionDetector(store, threshold=0.5)
        return await det.scan_recent(hours=48)

    assert asyncio.run(_run()) == []


def test_unmapped_developers_excluded():
    """Attribution-unmapped github events shouldn't fire false alarms."""
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)
        v = [0.5]*20 + [0.5]*(768-20)
        await _save_commit(store, "unmapped:alice-dev", "u1", "auth payment refactor",
                            files=["x.py"], vector=v)
        await _save_commit(store, "bob", "b1", "auth payment work",
                            files=["x.py"], vector=v)

        det = SemanticCollisionDetector(store, threshold=0.5)
        return await det.scan_recent(hours=48)

    assert asyncio.run(_run()) == []


def test_dev_pair_dedupe_keeps_highest_similarity():
    """Multiple collisions between the same two devs → report only the best one."""
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)

        v_strong = [0.9]*10 + [0.1]*(768-10)
        v_weak   = [0.5]*5 + [0.5]*5 + [0.0]*(768-10)

        await _save_commit(store, "alice", "a1", "auth middleware payment flow refactor",
                            vector=v_strong)
        await _save_commit(store, "alice", "a2", "auth middleware weak",
                            vector=v_weak)
        await _save_commit(store, "bob", "b1", "auth middleware payment handler",
                            vector=v_strong)
        await _save_commit(store, "bob", "b2", "auth middleware weaker",
                            vector=v_weak)

        det = SemanticCollisionDetector(store, threshold=0.3)
        return await det.scan_recent(hours=48)

    collisions = asyncio.run(_run())
    # Only one collision per dev-pair
    assert len(collisions) == 1
    # It kept the stronger pair
    assert collisions[0].cosine_similarity > 0.7


def test_severity_high_when_file_overlap_and_strong_cosine():
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)
        v = [1.0]*768
        await _save_commit(store, "alice", "a1", "auth middleware payment",
                            files=["auth.py", "session.py"], vector=v)
        await _save_commit(store, "bob", "b1", "auth middleware payment",
                            files=["auth.py", "session.py"], vector=v)

        det = SemanticCollisionDetector(store)
        return await det.scan_recent(hours=48)

    c = asyncio.run(_run())[0]
    assert c.severity == "high"


def test_time_window_filters_old_artifacts():
    """An artifact older than `hours` must not pair with a recent one."""
    from shadow_replica.collision.semantic import SemanticCollisionDetector

    async def _run():
        from shadow_replica.storage.received_store import ReceivedStore
        store = ReceivedStore(embedder=None)
        _install_vector_stub(store)
        v = [1.0]*768
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await _save_commit(store, "alice", "a1", "auth payment refactor",
                            vector=v, received_at=old)
        await _save_commit(store, "bob", "b1", "auth payment refactor",
                            vector=v, received_at=now)

        det = SemanticCollisionDetector(store)
        return await det.scan_recent(hours=48)

    assert asyncio.run(_run()) == []


# ── HTTP endpoint ──────────────────────────────────────────────────────────

def test_collisions_endpoint_requires_manager_token(client):
    r = client.get("/ui/api/collisions")
    assert r.status_code == 401


def test_collisions_endpoint_returns_detector_output(client):
    store = _store_from_client(client)
    _install_vector_stub(store)
    v = [1.0]*768

    async def _seed():
        await _save_commit(store, "alice", "a1", "auth payment refactor",
                            files=["auth.py"], vector=v)
        await _save_commit(store, "bob", "b1", "auth payment work",
                            files=["auth.py"], vector=v)
    asyncio.run(_seed())

    r = client.get("/ui/api/collisions?hours=48", headers=_auth(client))
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    c = data["collisions"][0]
    assert {c["developer_a"], c["developer_b"]} == {"alice", "bob"}
    assert "auth.py" in c["file_overlap"]
    # Names resolved from trusted_devs
    assert {c["developer_a_name"], c["developer_b_name"]} == {"Alice", "Bob"}


# ── Brief integration ─────────────────────────────────────────────────────

def test_brief_includes_collisions_field(client):
    store = _store_from_client(client)
    _install_vector_stub(store)
    v = [1.0]*768

    async def _seed():
        await _save_commit(store, "alice", "a1", "auth payment refactor",
                            files=["auth.py"], vector=v)
        await _save_commit(store, "bob", "b1", "auth payment work",
                            files=["auth.py"], vector=v)
    asyncio.run(_seed())

    r = client.get("/ui/api/brief?hours=24", headers=_auth(client))
    assert r.status_code == 200
    data = r.json()
    assert "collisions" in data
    assert len(data["collisions"]) == 1


def test_brief_emits_semantic_collision_envelope_on_wiretap(client):
    store = _store_from_client(client)
    _install_vector_stub(store)
    v = [1.0]*768

    async def _seed():
        await _save_commit(store, "alice", "a1", "auth payment refactor",
                            files=["auth.py"], vector=v)
        await _save_commit(store, "bob", "b1", "auth payment work",
                            files=["auth.py"], vector=v)
    asyncio.run(_seed())

    # Trigger the brief (which runs the collision scan)
    client.get("/ui/api/brief?hours=24", headers=_auth(client))

    # Wiretap recent should contain a SEMANTIC_COLLISION_ALERT envelope
    r = client.get("/ui/api/wiretap/recent?limit=200", headers=_auth(client))
    envs = r.json()
    alerts = [e for e in envs if e["intent"] == "semantic_collision_alert"]
    assert alerts, f"expected a semantic_collision_alert envelope; got {[e['intent'] for e in envs]}"
    assert "alice" in alerts[-1]["summary"].lower() or "Alice" in alerts[-1]["summary"]
