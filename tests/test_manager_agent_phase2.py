"""Phase-2 tests for the manager agent — new tools + streaming SSE.

Covers:
  - 5 new tools registered: get_initiatives, get_threads, get_collisions,
    get_resolutions, get_github_events
  - Each new tool invokes without crashing on a quiet / empty store
  - get_github_events respects developer_id filter
  - get_resolutions returns stored bug_resolution artifacts
  - run_turn_stream chunk sequence: started → (tool_call → tool_result)+ →
    message_delta → done
  - run_turn_stream budget-exhausted path emits message_delta + done with flag
  - SSE /ui/api/chat/stream endpoint: auth + 503 + happy-path chunking
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


# ── Shared fixtures (duplicate of phase 1 for isolation) ────────────────────

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
    cfg.trusted_devs.append(TrustedDev(developer_id="bob", name="Bob", token_value="vsub_bob"))
    app = build_app(cfg)
    with TestClient(app) as c:
        c._cfg = cfg
        c._manager = cfg.manager_token
        yield c


def _auth(client, token=None):
    return {"Authorization": f"Bearer {token or client._manager}"}


# ── Helpers (mirrored from phase 1) ─────────────────────────────────────────

def _walk_routes(container):
    for route in getattr(container, "routes", []):
        if hasattr(route, "endpoint") and hasattr(route, "path"):
            yield route
        else:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from _walk_routes(inner)


def _get_store(client):
    for route in _walk_routes(client.app):
        if hasattr(route, "endpoint"):
            for cell in route.endpoint.__closure__ or ():
                v = cell.cell_contents
                if hasattr(v, "save_async") and hasattr(v, "list_for_developer"):
                    return v
    raise RuntimeError("Could not resolve ReceivedStore")


def _get_hub(client):
    for route in _walk_routes(client.app):
        if hasattr(route, "endpoint"):
            for cell in route.endpoint.__closure__ or ():
                v = cell.cell_contents
                if hasattr(v, "online_developer_ids") and hasattr(v, "friction_all"):
                    return v
    raise RuntimeError("Could not resolve WSHub")


def _build_tool_ctx(client):
    from shadow_replica.agent.tools import ToolContext
    from shadow_replica.agent.subscriber_agent import SubscriberAgent
    store = _get_store(client)
    hub = _get_hub(client)
    agent = SubscriberAgent(store, embedder=store._embedder)
    return ToolContext(store=store, hub=hub, cfg=client._cfg, agent=agent)


# ── Fake Anthropic (same pattern as phase 1) ────────────────────────────────

class _FakeMessage:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeBlock(SimpleNamespace):
    pass


def _text_block(text):
    return _FakeBlock(type="text", text=text)


def _tool_use_block(tool_id, name, input_):
    return _FakeBlock(type="tool_use", id=tool_id, name=name, input=input_)


class _FakeAnthropic:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            return _FakeMessage([_text_block("(no more scripted responses)")], stop_reason="end_turn")
        return self._responses.pop(0)


def _build_agent(client, tmp_path, fake_client, bus=None):
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent
    ctx = _build_tool_ctx(client)
    mem = ChatMemory(tmp_path / "chat.db")
    return ManagerAgent(tool_ctx=ctx, memory=mem, anthropic_client=fake_client, bus=bus)


# ── Phase 2 tool registration ───────────────────────────────────────────────

def test_tool_registry_has_all_phase_2_tools():
    from shadow_replica.agent.tools import tool_names, anthropic_schemas
    names = set(tool_names())
    assert {
        "search_artifacts", "get_timeline", "get_brief",
        "list_developers", "get_friction",
        "get_initiatives", "get_threads", "get_collisions",
        "get_resolutions", "get_github_events",
    } <= names
    schemas = anthropic_schemas()
    assert len(schemas) >= 10
    # Last tool must carry cache_control key when wrapped by _cached_tool_blocks
    from shadow_replica.agent.manager_agent import _cached_tool_blocks
    wrapped = _cached_tool_blocks()
    assert wrapped[-1]["cache_control"] == {"type": "ephemeral"}


# ── Each new tool, happy path on empty-ish store ────────────────────────────

def test_tool_get_initiatives_handles_empty_store(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_initiatives", {"days": 7}))
    assert "initiatives" in out and "artifact_count" in out


def test_tool_get_threads_handles_empty_store(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_threads", {"hours": 24}))
    assert "threads" in out and out["count"] == 0


def test_tool_get_collisions_handles_empty_store(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_collisions", {"hours": 48}))
    assert "collisions" in out and out["count"] == 0


def test_tool_get_resolutions_empty_and_populated(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_resolutions", {}))
    assert "resolutions" in out and out["count"] == 0

    # Seed one bug_resolution
    now = "2026-10-25T12:00:00+00:00"
    asyncio.run(ctx.store.save_async(
        approval_id="brA",
        developer_id="alice",
        artifact_type="bug_resolution",
        draft_id="",
        channel="bug_resolution",
        payload_json=json.dumps({
            "error_signature": "ModuleNotFoundError: No module named 'foo'",
            "fix_summary": "pip install foo",
            "repo": "acme/payments",
        }),
        approved_at=now,
    ))
    out2 = asyncio.run(invoke(ctx, "get_resolutions", {}))
    assert out2["count"] >= 1
    assert any("ModuleNotFoundError" in r["error_signature"] for r in out2["resolutions"])


def test_tool_get_github_events_filters_by_developer(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    now = "2026-10-25T12:00:00+00:00"
    for dev, approval in [("alice", "ghA"), ("bob", "ghB")]:
        asyncio.run(ctx.store.save_async(
            approval_id=approval,
            developer_id=dev,
            artifact_type="github_event",
            draft_id="",
            channel="github_event",
            payload_json=json.dumps({
                "event_type": "pull_request.opened",
                "action": "opened",
                "repo": "acme/payments",
                "pr_number": 42,
                "title": f"PR by {dev}",
                "summary": f"{dev} opened PR #42",
                "url": "https://github.com/acme/payments/pull/42",
            }),
            approved_at=now,
        ))

    out_all = asyncio.run(invoke(ctx, "get_github_events", {}))
    assert out_all["count"] >= 2
    out_alice = asyncio.run(invoke(ctx, "get_github_events", {"developer_id": "alice"}))
    devs = {e["developer_id"] for e in out_alice["events"]}
    assert devs == {"alice"}


# ── Streaming method chunk sequence ─────────────────────────────────────────

def test_run_turn_stream_simple_message(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage([_text_block("Hi there")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _collect():
        chunks = []
        async for c in agent.run_turn_stream(manager_token="vsm_s", message="hey"):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_collect())
    types = [c["type"] for c in chunks]
    assert types[0] == "started"
    assert "message_delta" in types
    assert types[-1] == "done"
    text = next(c["content"] for c in chunks if c["type"] == "message_delta")
    assert text == "Hi there"
    done = chunks[-1]
    assert done["loops"] == 1
    assert done["budget_exhausted"] is False
    assert done["conversation_id"]


def test_run_turn_stream_tool_use_sequence(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "list_developers", {})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("Alice and Bob.")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _collect():
        chunks = []
        async for c in agent.run_turn_stream(manager_token="vsm_s2", message="who?"):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_collect())
    types = [c["type"] for c in chunks]
    # Must emit: started, tool_call, tool_result, message_delta, done
    assert types == ["started", "tool_call", "tool_result", "message_delta", "done"]
    call = next(c for c in chunks if c["type"] == "tool_call")
    assert call["tool"] == "list_developers"
    result = next(c for c in chunks if c["type"] == "tool_result")
    assert result["id"] == "tu1"
    assert result["latency_ms"] >= 0


def test_run_turn_stream_budget_exhausted(client, tmp_path):
    # Script 9 tool_use rounds; stream should stop at 8 with the budget notice.
    script = []
    for i in range(10):
        script.append(_FakeMessage(
            [_tool_use_block(f"tu{i}", "list_developers", {})],
            stop_reason="tool_use",
        ))
    fake = _FakeAnthropic(script)
    agent = _build_agent(client, tmp_path, fake)

    async def _collect():
        chunks = []
        async for c in agent.run_turn_stream(manager_token="vsm_bud", message="loop"):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_collect())
    done = chunks[-1]
    assert done["type"] == "done"
    assert done["budget_exhausted"] is True
    # Must have emitted a budget message_delta before done
    deltas = [c for c in chunks if c["type"] == "message_delta"]
    assert any("budget" in d["content"].lower() for d in deltas)


# ── SSE endpoint ────────────────────────────────────────────────────────────

def test_stream_endpoint_rejects_unauthenticated(client):
    r = client.post("/ui/api/chat/stream", json={"message": "hi"})
    assert r.status_code == 401


def test_stream_endpoint_503_when_agent_unconfigured(client):
    r = client.post("/ui/api/chat/stream", json={"message": "hi"}, headers=_auth(client))
    assert r.status_code == 503


def test_stream_endpoint_requires_message(client):
    r = client.post("/ui/api/chat/stream", json={}, headers=_auth(client))
    assert r.status_code in (400, 503)


def test_stream_endpoint_e2e_with_injected_agent(replica_tmp, monkeypatch):
    """Full SSE round-trip: parse the event stream."""
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent

    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "list_developers", {})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("Alice and Bob.")], stop_reason="end_turn"),
    ])

    def _fake_build(tool_ctx, memory, cfg, bus=None):
        return ManagerAgent(tool_ctx=tool_ctx, memory=memory, anthropic_client=fake, bus=bus)

    monkeypatch.setattr(
        "shadow_replica.agent.manager_agent.build_manager_agent",
        _fake_build,
    )

    from shadow_replica.app import build_app
    from shadow_replica.config import load, TrustedDev
    cfg = load()
    cfg.qdrant.enabled = False
    cfg.trusted_devs.append(TrustedDev(developer_id="alice", name="Alice", token_value="vsub_alice"))
    app = build_app(cfg)
    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {cfg.manager_token}"}
        with c.stream("POST", "/ui/api/chat/stream", json={"message": "who?"}, headers=headers) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            body = "".join(resp.iter_text())

    events = _parse_sse(body)
    event_names = [e[0] for e in events]
    assert event_names[0] == "started"
    assert "tool_call" in event_names
    assert "tool_result" in event_names
    assert "message_delta" in event_names
    assert event_names[-1] == "done"

    # Parse the final done payload
    done_payload = next(p for name, p in events if name == "done")
    assert done_payload["budget_exhausted"] is False
    assert done_payload["loops"] == 2
    assert done_payload["conversation_id"]


# ── SSE parser ──────────────────────────────────────────────────────────────

def _parse_sse(body: str):
    """Return list of (event_name, data_dict) for a raw SSE body."""
    out = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event = None
        data_lines = []
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                event = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        if event is None:
            continue
        try:
            data = json.loads("".join(data_lines))
        except Exception:
            data = {}
        out.append((event, data))
    return out
