"""End-to-end regression for P6 offline demo mode.

Shape: seed a Claude session JSONL containing a planted fake AWS key → run
the digest agent against it with the MockLLMProvider → assert the draft
is pending → assert the AWS key was removed by the scrubber → approve →
push to a TestClient-wrapped replica → assert the replica's /ui/api/activity
returns the artifact.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_DEMO_AWS_KEY = "AKIAEXAMPLEDEMO9999"  # same shape the demo CLI plants

MGR_TOKEN = "vsm_demo_e2e_" + secrets.token_hex(8)
DEV_ID = "demo-alice"
SUB_TOKEN = "vsub_demo_e2e_" + secrets.token_hex(16)
SUB_ID = "sub_demo_e2e"


def _seed_session(sessions_dir: Path) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    jsonl = sessions_dir / "demo-session.jsonl"
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    turns = [
        {
            "role": "user",
            "content": (
                "Reviewing the S3 upload refactor. I pasted the sandbox key "
                f"{_DEMO_AWS_KEY} so you have it."
            ),
            "timestamp": now,
        },
        {
            "role": "assistant",
            "content": (
                "Got it. I'll keep the key out of the commit. Split the uploader "
                "out of handlers/s3.py and added tests/test_uploader.py."
            ),
            "timestamp": now,
        },
    ]
    with open(jsonl, "w", encoding="utf-8") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")
    return jsonl


@pytest.fixture
def demo_env(tmp_path: Path, monkeypatch):
    # Isolate ghost + replica state via the P6 env-var overrides.
    ghost_home = tmp_path / "ghost"
    replica_home = tmp_path / "replica"
    sessions_root = tmp_path / "sessions"
    sessions_dir = sessions_root / "demo-project"
    ghost_home.mkdir(parents=True, exist_ok=True)
    replica_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VANTAGE_HOME", str(ghost_home))
    monkeypatch.setenv("VANTAGE_REPLICA_HOME", str(replica_home))

    _seed_session(sessions_dir)

    from ghost_daemon.config import (
        Config, EmbeddingsConfig, LLMConfig, QdrantConfig, UIConfig,
    )
    from shadow_replica.config import (
        EmbeddingsConfig as RepEmb, LLMConfig as RepLLM, QdrantConfig as RepQ,
        ReplicaConfig, TrustedDev,
    )

    gc = Config(
        developer_id=DEV_ID,
        display_name="Demo Alice",
        session_dirs=[str(sessions_root)],
        llm=LLMConfig(provider="mock", model="mock", api_key_env=""),
        embeddings=EmbeddingsConfig(provider="mock", model="mock", endpoint=""),
        ui=UIConfig(bind_address="127.0.0.1", port=7474),
        qdrant=QdrantConfig(enabled=False, url="http://127.0.0.1:1"),
    )
    rc = ReplicaConfig(
        replica_id="demo-replica",
        manager_token=MGR_TOKEN,
        trusted_devs=[TrustedDev(
            developer_id=DEV_ID,
            name="Demo Alice",
            token_value=SUB_TOKEN,
            subscriber_id=SUB_ID,
        )],
        qdrant=RepQ(enabled=False, url="http://127.0.0.1:1"),
        embeddings=RepEmb(provider="mock", model="mock", endpoint=""),
        llm=RepLLM(provider="mock", model="mock", api_key_env=""),
    )
    return gc, rc, sessions_dir


def _signed_headers(body: bytes) -> dict:
    sig = "sha256=" + _hmac.new(SUB_TOKEN.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"Bearer {SUB_TOKEN}",
        "X-Vantage-Signature": sig,
        "Content-Type": "application/json",
    }


def test_digest_scrubs_then_replica_receives(demo_env):
    import asyncio
    from shadow_replica.app import build_app as build_replica

    gc, rc, _ = demo_env
    replica_app = build_replica(rc)

    from ghost_daemon.agents.digest_agent import DigestAgent
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.models.registry import build_from_config
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.watchers.project_map import ProjectMap
    from ghost_daemon.watchers.session_tail import SessionTailer

    gc.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    gc.state_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(gc.sqlite_path)
    gate = ApprovalGate(store, gc.developer_id)
    llm = build_from_config(gc)
    assert llm.model_id == "mock"

    tailer = SessionTailer(gc.state_dir)
    project_map = ProjectMap(gc)
    agent = DigestAgent(gc, llm, project_map, tailer)

    # Point the agent at a period that covers "now" so the seeded turns fall
    # inside the digest window.
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=7)

    draft = asyncio.run(agent.run(start, end))

    # 1. Draft was produced and is of the expected type.
    assert draft.artifact_type == "weekly_digest"

    # 2. The scrubber dropped the fake AWS key from every string field in the
    #    draft payload (highlights / commits / any serialized string).
    payload_json = draft.payload.model_dump_json()
    assert _DEMO_AWS_KEY not in payload_json, (
        f"planted AWS key leaked into the draft payload: {payload_json[:400]}"
    )

    # 3. Submitting to the gate → the UI would surface it as pending.
    draft_id = gate.submit(draft)
    assert any(d["draft_id"] == draft_id for d in gate.list_pending())

    # 4. Approve → we get an ApprovedPayload.
    approved = gate.approve(draft_id)
    assert approved.approval_id

    # 5. Push to the replica via its TestClient, with real signature auth.
    body = approved.model_dump_json().encode()
    with TestClient(replica_app) as replica:
        r = replica.post("/v1/receive", content=body, headers=_signed_headers(body))
        assert r.status_code == 201, (r.status_code, r.text)

        # 6. The replica's dashboard activity view shows exactly this artifact.
        r = replica.get(
            "/ui/api/activity",
            headers={"Authorization": f"Bearer {MGR_TOKEN}"},
        )
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1, items
        assert items[0]["developer_id"] == DEV_ID
        assert items[0]["channel"] == "weekly_digest"

        # And the full payload JSON (as stored) also has no AWS key.
        timeline = replica.get(
            f"/ui/api/timeline/{DEV_ID}",
            headers={"Authorization": f"Bearer {MGR_TOKEN}"},
        ).json()
        full = json.dumps(timeline)
        assert _DEMO_AWS_KEY not in full, (
            "planted AWS key leaked to the replica timeline payload"
        )
