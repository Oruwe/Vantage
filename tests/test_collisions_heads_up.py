"""Regression tests for the dev-facing collision heads-up surface.

Covers:
  - Replica /v1/collisions/for-me: auth required, filters to the dev's own pairs
  - CollisionPoller aggregates across subscribers, dedupes by collision_id
  - Poller caches between calls within refresh_seconds
  - Poller tolerates one subscriber failing without losing the others
  - Poller labels the "other" developer correctly regardless of a/b order
  - /api/collisions returns stable JSON shape
  - Ghost UI /api/collisions is empty-safe (no poller wired)
  - Collision pill + drawer markup present in base.html
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from ghost_daemon.insights.collision_poller import CollisionPoller


# ── CollisionPoller internals ────────────────────────────────────────────────


@pytest.fixture
def subscriber_with_identity(tmp_path: Path):
    """A SubscriberRegistry with one active subscriber ready to poll."""
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from vantage.schemas import Channel

    reg = SubscriberRegistry(tmp_path / "vantage.db")
    reg.add(
        name="manager-a",
        receive_url="https://manager-a.local",
        channels=[Channel.WEEKLY_DIGEST],
    )
    return reg


def _collision_payload(collision_id: str, dev_a: str, dev_b: str, *, kind="semantic", score=0.82):
    """Shape matching SemanticCollision.model_dump(mode='json')."""
    return {
        "collision_id": collision_id,
        "developer_a": dev_a,
        "developer_b": dev_b,
        "developer_a_name": dev_a.title(),
        "developer_b_name": dev_b.title(),
        "artifact_a_id": f"a-{collision_id}",
        "artifact_b_id": f"b-{collision_id}",
        "cosine": score,
        "kind": kind,
        "detected_at": f"2026-11-08T12:{collision_id[-2:]}:00Z",
        "shared_files": ["auth/jwt.py"],
    }


def test_poller_aggregates_across_subscribers(tmp_path: Path) -> None:
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from vantage.schemas import Channel

    reg = SubscriberRegistry(tmp_path / "vantage.db")
    reg.add("m1", "https://m1.local", [Channel.WEEKLY_DIGEST])
    reg.add("m2", "https://m2.local", [Channel.WEEKLY_DIGEST])

    def handler(request: httpx.Request) -> httpx.Response:
        if "m1" in str(request.url):
            return httpx.Response(200, json={"collisions": [
                _collision_payload("01", "alice", "bob"),
            ]})
        return httpx.Response(200, json={"collisions": [
            _collision_payload("02", "alice", "carol"),
        ]})

    poller = CollisionPoller(subscribers=reg, developer_id="alice", refresh_seconds=0.0)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with patch("httpx.AsyncClient", return_value=c):
                # The real poller creates its own client; patch the class
                return await poller.get(force=True)
            # Note: the context manager above is a no-op because the poller
            # creates its own httpx.AsyncClient. We need a different approach.

    # Simpler: patch the poller's _fetch_one.
    async def _fake_fetch_one(client, row):
        if row.name == "m1":
            return [poller._normalize(_collision_payload("01", "alice", "bob"), source_subscriber=row.name)]
        return [poller._normalize(_collision_payload("02", "alice", "carol"), source_subscriber=row.name)]

    poller._fetch_one = _fake_fetch_one  # type: ignore[assignment]
    bundle = asyncio.run(poller.get(force=True))
    assert bundle.count == 2
    assert {c.other_developer for c in bundle.collisions} == {"bob", "carol"}


def test_poller_dedupes_by_collision_id(tmp_path: Path, subscriber_with_identity) -> None:
    from vantage.schemas import Channel
    subscriber_with_identity.add("manager-b", "https://manager-b.local", [Channel.WEEKLY_DIGEST])
    poller = CollisionPoller(
        subscribers=subscriber_with_identity,
        developer_id="alice",
        refresh_seconds=0.0,
    )

    async def _fake_fetch_one(client, row):
        # Both managers report the same collision_id (multi-manager fan-in).
        return [poller._normalize(
            _collision_payload("dup", "alice", "bob"), source_subscriber=row.name,
        )]

    poller._fetch_one = _fake_fetch_one  # type: ignore[assignment]
    bundle = asyncio.run(poller.get(force=True))
    assert bundle.count == 1


def test_poller_caches_within_refresh_window(subscriber_with_identity) -> None:
    poller = CollisionPoller(
        subscribers=subscriber_with_identity,
        developer_id="alice",
        refresh_seconds=60.0,
    )
    calls = {"n": 0}

    async def _fake_fetch_one(client, row):
        calls["n"] += 1
        return [poller._normalize(
            _collision_payload("c1", "alice", "bob"), source_subscriber=row.name,
        )]

    poller._fetch_one = _fake_fetch_one  # type: ignore[assignment]

    async def _go():
        await poller.get(force=True)
        await poller.get()  # should be cached
        await poller.get()  # cached again
        return calls["n"]

    n = asyncio.run(_go())
    assert n == 1, f"expected a single network burst, got {n}"


def test_poller_tolerates_one_subscriber_failing(tmp_path: Path) -> None:
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from vantage.schemas import Channel

    reg = SubscriberRegistry(tmp_path / "vantage.db")
    reg.add("good", "https://good.local", [Channel.WEEKLY_DIGEST])
    reg.add("bad", "https://bad.local", [Channel.WEEKLY_DIGEST])

    poller = CollisionPoller(subscribers=reg, developer_id="alice", refresh_seconds=0.0)

    async def _fake_fetch_one(client, row):
        if row.name == "bad":
            raise RuntimeError("manager down")
        return [poller._normalize(
            _collision_payload("c1", "alice", "bob"), source_subscriber=row.name,
        )]

    poller._fetch_one = _fake_fetch_one  # type: ignore[assignment]
    bundle = asyncio.run(poller.get(force=True))
    assert bundle.count == 1  # the good one survives
    assert any("bad" in e for e in bundle.errors)


def test_poller_normalize_labels_other_developer_regardless_of_order(
    subscriber_with_identity,
) -> None:
    poller = CollisionPoller(
        subscribers=subscriber_with_identity,
        developer_id="alice",
        refresh_seconds=0.0,
    )
    # alice is developer_a → other is bob
    r1 = poller._normalize(
        _collision_payload("c1", "alice", "bob"), source_subscriber="m",
    )
    # alice is developer_b → other is still "the other one"
    r2 = poller._normalize(
        _collision_payload("c2", "carol", "alice"), source_subscriber="m",
    )
    assert r1.other_developer == "bob"
    assert r2.other_developer == "carol"


def test_poller_empty_when_no_subscribers(tmp_path: Path) -> None:
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    reg = SubscriberRegistry(tmp_path / "vantage.db")  # no add()

    poller = CollisionPoller(subscribers=reg, developer_id="alice", refresh_seconds=0.0)
    bundle = asyncio.run(poller.get(force=True))
    assert bundle.count == 0
    assert bundle.errors == []


# ── Ghost UI /api/collisions ─────────────────────────────────────────────────


@pytest.fixture
def ghost_app_with_poller(tmp_path: Path):
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.insights.collision_poller import CollisionPoller
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app
    from vantage.schemas import Channel

    cfg = Config(developer_id="alice", llm=LLMConfig(provider="groq"))
    store = SQLiteStore(tmp_path / "vantage.db")
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(tmp_path / "vantage.db")
    subs.add("m1", "https://m1.local", [Channel.WEEKLY_DIGEST])
    gate = ApprovalGate(store, "alice")

    poller = CollisionPoller(subscribers=subs, developer_id="alice", refresh_seconds=60.0)

    async def _fake_fetch_one(client, row):
        return [poller._normalize(
            _collision_payload("ui-c1", "alice", "bob"), source_subscriber=row.name,
        )]

    poller._fetch_one = _fake_fetch_one  # type: ignore[assignment]

    app = build_app(store, gate, audit, subs, cfg, collisions=poller)
    return app, poller


def test_api_collisions_returns_populated_bundle(ghost_app_with_poller) -> None:
    app, _ = ghost_app_with_poller
    r = TestClient(app).get("/api/collisions?force=true")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["collisions"][0]["other_developer"] == "bob"
    assert "detected_at" in data["collisions"][0]


def test_api_collisions_empty_safe_without_poller(tmp_path: Path) -> None:
    """When no CollisionPoller is wired, /api/collisions must return a safe
    empty payload rather than 500."""
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    cfg = Config(developer_id="alice", llm=LLMConfig())
    store = SQLiteStore(tmp_path / "vantage.db")
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice")

    app = build_app(store, gate, audit, subs, cfg)  # no collisions=
    r = TestClient(app).get("/api/collisions")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["collisions"] == []


# ── UI markup checks ─────────────────────────────────────────────────────────


def _base_html_text() -> str:
    path = Path(__file__).resolve().parent.parent / "packages" / "ghost-daemon" / "src" / "ghost_daemon" / "ui" / "templates" / "base.html"
    return path.read_text(encoding="utf-8")


def test_base_html_contains_collision_pill_markup() -> None:
    html = _base_html_text()
    assert 'id="coll-pill"' in html
    assert 'id="coll-text"' in html
    assert 'id="coll-drawer"' in html


def test_base_html_registers_collision_poll_interval() -> None:
    html = _base_html_text()
    assert "refreshCollisions()" in html
    # Interval must be set (any reasonable period); just confirm the setInterval call exists
    assert re.search(r"setInterval\(refreshCollisions,\s*\d+\)", html) is not None


# ── Replica endpoint (shadow-replica side) ───────────────────────────────────


def _fast_replica_cfg(**overrides):
    """Build a ReplicaConfig that doesn't probe Qdrant/Ollama at import time."""
    from shadow_replica.config import (
        EmbeddingsConfig, LLMConfig, QdrantConfig, ReplicaConfig,
    )
    kwargs = dict(
        replica_id="test-replica",
        qdrant=QdrantConfig(enabled=False, url="http://127.0.0.1:1"),
        embeddings=EmbeddingsConfig(provider="ollama", model="nomic", endpoint="http://127.0.0.1:1"),
        llm=LLMConfig(provider=""),
    )
    kwargs.update(overrides)
    return ReplicaConfig(**kwargs)


def test_replica_collisions_for_me_requires_bearer() -> None:
    """Hitting the endpoint without auth should 401."""
    from shadow_replica.app import build_app

    cfg = _fast_replica_cfg()
    app = build_app(cfg)
    r = TestClient(app).get("/v1/collisions/for-me")
    assert r.status_code == 401


def test_replica_collisions_for_me_filters_to_own_pairs(tmp_path: Path) -> None:
    """A trusted dev's bearer should only see collisions involving their id."""
    from shadow_replica.app import build_app
    from shadow_replica.config import TrustedDev
    from vantage.schemas import SemanticCollision

    cfg = _fast_replica_cfg(trusted_devs=[
        TrustedDev(developer_id="alice", name="Alice", token_value="tkn-alice"),
        TrustedDev(developer_id="bob",   name="Bob",   token_value="tkn-bob"),
        TrustedDev(developer_id="carol", name="Carol", token_value="tkn-carol"),
    ])

    app = build_app(cfg)

    # Mock the detector to return a fixed set of collisions.
    fake_items = [
        SemanticCollision(developer_a="alice", developer_b="bob",
                          artifact_a_id="a1", artifact_b_id="b1",
                          artifact_a_type="commit_report", artifact_b_type="commit_report"),
        SemanticCollision(developer_a="bob", developer_b="carol",
                          artifact_a_id="a2", artifact_b_id="b2",
                          artifact_a_type="commit_report", artifact_b_type="commit_report"),
        SemanticCollision(developer_a="carol", developer_b="alice",
                          artifact_a_id="a3", artifact_b_id="b3",
                          artifact_a_type="commit_report", artifact_b_type="commit_report"),
    ]

    class _FakeDetector:
        def __init__(self, *args, **kwargs): pass
        async def scan_recent(self, **kwargs):
            return fake_items

    with patch("shadow_replica.collision.semantic.SemanticCollisionDetector", _FakeDetector):
        r = TestClient(app).get(
            "/v1/collisions/for-me",
            headers={"Authorization": "Bearer tkn-alice"},
        )
    assert r.status_code == 200
    data = r.json()
    # Alice should see 2 collisions (alice-bob, carol-alice), NOT bob-carol.
    assert data["count"] == 2
    for c in data["collisions"]:
        assert c["developer_a"] == "alice" or c["developer_b"] == "alice"
