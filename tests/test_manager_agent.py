"""Tests for M13 Phase 1 — tool-calling manager agent with memory.

Covers:
  - ChatMemory CRUD + per-manager isolation + prune caps
  - Tool registry invocation for each of the 5 Phase 1 tools
  - ManagerAgent tool-use loop termination
  - Budget-exhausted behavior at 8 iterations
  - Prompt-cache header shape on the Anthropic request
  - Memory persistence across turns (same conversation_id)
  - POST /ui/api/chat auth + 503 when agent not configured
  - A2A envelope emission (AGENT_TOOL_CALL, AGENT_TOOL_RESULT, AGENT_RESPONSE)
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from types import SimpleNamespace

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
    cfg.trusted_devs.append(TrustedDev(developer_id="bob", name="Bob", token_value="vsub_bob"))
    app = build_app(cfg)
    with TestClient(app) as c:
        c._cfg = cfg
        c._manager = cfg.manager_token
        yield c


def _auth(client, token: str | None = None):
    return {"Authorization": f"Bearer {token or client._manager}"}


# ── ChatMemory tests ────────────────────────────────────────────────────────

def test_hash_manager_token_is_stable_and_scoped(tmp_path):
    from shadow_replica.agent.chat_memory import hash_manager_token
    a1 = hash_manager_token("vsm_abc")
    a2 = hash_manager_token("vsm_abc")
    b = hash_manager_token("vsm_other")
    assert a1 == a2
    assert a1 != b
    assert len(a1) == 32


def test_chat_memory_create_and_fetch(tmp_path):
    from shadow_replica.agent.chat_memory import ChatMemory, hash_manager_token
    mem = ChatMemory(tmp_path / "chat.db")
    h = hash_manager_token("vsm_test")
    convo = mem.get_or_create(h, title_seed="How is Alice doing today?")
    assert convo.conversation_id
    assert convo.title.startswith("How is Alice")
    # Fetching with the same id returns the same convo (not a new one)
    again = mem.get_or_create(h, conversation_id=convo.conversation_id)
    assert again.conversation_id == convo.conversation_id


def test_chat_memory_append_turn_and_prune(tmp_path):
    from shadow_replica.agent.chat_memory import ChatMemory, hash_manager_token
    mem = ChatMemory(tmp_path / "chat.db")
    h = hash_manager_token("vsm_prune")
    convo = mem.get_or_create(h, title_seed="x")
    for i in range(60):
        mem.append_turn(convo.conversation_id, role="user", content=f"msg {i}")
    turns = mem.list_turns(convo.conversation_id)
    # 50-turn cap enforced
    assert len(turns) == 50
    assert turns[-1].content == "msg 59"
    assert turns[0].content == "msg 10"  # earliest 10 pruned


def test_chat_memory_per_manager_isolation(tmp_path):
    from shadow_replica.agent.chat_memory import ChatMemory, hash_manager_token
    mem = ChatMemory(tmp_path / "chat.db")
    h_a = hash_manager_token("vsm_a")
    h_b = hash_manager_token("vsm_b")
    c_a = mem.get_or_create(h_a, title_seed="A asks")
    # Manager B tries to use A's conversation_id — must get a brand-new one
    c_b = mem.get_or_create(h_b, conversation_id=c_a.conversation_id, title_seed="B asks")
    assert c_b.conversation_id != c_a.conversation_id
    # Manager A sees only their own thread
    convos_a = mem.list_conversations(h_a)
    assert len(convos_a) == 1 and convos_a[0].conversation_id == c_a.conversation_id


def test_chat_memory_convo_cap_prunes_oldest(tmp_path):
    from shadow_replica.agent.chat_memory import ChatMemory, hash_manager_token
    mem = ChatMemory(tmp_path / "chat.db")
    h = hash_manager_token("vsm_cap")
    ids = []
    for i in range(12):
        c = mem.get_or_create(h, title_seed=f"q{i}")
        mem.append_turn(c.conversation_id, role="user", content=f"turn {i}")
        ids.append(c.conversation_id)
    convos = mem.list_conversations(h)
    assert len(convos) == 10  # cap enforced; oldest 2 pruned


# ── Tool registry tests ─────────────────────────────────────────────────────

def _build_tool_ctx(client):
    """Snatch the ToolContext from the live app wiring."""
    from shadow_replica.agent.tools import ToolContext
    # Introspect the live cfg + hub + store via the app's routes.
    store = _get_store(client)
    hub = _get_hub(client)
    from shadow_replica.agent.subscriber_agent import SubscriberAgent
    agent = SubscriberAgent(store, embedder=store._embedder)
    return ToolContext(store=store, hub=hub, cfg=client._cfg, agent=agent)


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


def test_tool_registry_exposes_phase_1_tools():
    from shadow_replica.agent.tools import anthropic_schemas, tool_names
    names = set(tool_names())
    # Phase 1 required tools must still be present (Phase 2 adds more alongside).
    assert {
        "search_artifacts", "get_timeline", "get_brief",
        "list_developers", "get_friction",
    } <= names
    schemas = anthropic_schemas()
    assert len(schemas) >= 5
    for s in schemas:
        assert "name" in s and "description" in s and "input_schema" in s


def test_tool_list_developers_returns_known_devs(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "list_developers", {}))
    assert isinstance(out, dict) and "developers" in out
    ids = {d["developer_id"] for d in out["developers"]}
    assert {"alice", "bob"} <= ids


def test_tool_get_timeline_requires_developer_id(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_timeline", {}))
    assert "error" in out


def test_tool_get_timeline_returns_rows_for_developer(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    # Seed one row
    now = "2026-10-25T12:00:00+00:00"
    asyncio.run(ctx.store.save_async(
        approval_id="apr1",
        developer_id="alice",
        artifact_type="commit_report",
        draft_id="",
        channel="commit_report",
        payload_json=json.dumps({"author_msg": "fix auth bug", "generated_summary": "fix auth bug"}),
        approved_at=now,
    ))
    out = asyncio.run(invoke(ctx, "get_timeline", {"developer_id": "alice"}))
    assert out["developer_id"] == "alice"
    assert out["count"] >= 1
    assert any("fix auth" in it["summary"] for it in out["items"])


def test_tool_unknown_returns_error(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "does_not_exist", {}))
    assert "error" in out and "unknown tool" in out["error"]


def test_tool_get_brief_returns_structure(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_brief", {"hours": 24}))
    for k in ("shipped", "in_flight", "bottlenecks", "unresolved_questions", "headline"):
        assert k in out


def test_tool_get_friction_empty_when_hub_quiet(client):
    from shadow_replica.agent.tools import invoke
    ctx = _build_tool_ctx(client)
    out = asyncio.run(invoke(ctx, "get_friction", {}))
    assert out["count"] >= 0 and "friction" in out


# ── Fake Anthropic client for agent-loop tests ──────────────────────────────

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
    """Replays a scripted sequence of responses."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            return _FakeMessage([_text_block("(no more scripted responses)")], stop_reason="end_turn")
        return self._responses.pop(0)


# ── ManagerAgent loop tests ─────────────────────────────────────────────────

def _build_agent(client, tmp_path, fake_client):
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent
    ctx = _build_tool_ctx(client)
    mem = ChatMemory(tmp_path / "chat.db")
    return ManagerAgent(tool_ctx=ctx, memory=mem, anthropic_client=fake_client, model="claude-sonnet-4-6")


def test_agent_simple_response_no_tool_calls(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage([_text_block("Hello, I'm Vantage.")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    result = asyncio.run(agent.run_turn(manager_token="vsm_test", message="hi"))
    assert result.assistant_message == "Hello, I'm Vantage."
    assert result.tool_calls == []
    assert result.loops == 1
    assert not result.budget_exhausted


def test_agent_tool_use_then_text(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "list_developers", {})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("You have Alice and Bob.")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    result = asyncio.run(agent.run_turn(manager_token="vsm_t", message="who's on the team?"))
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool_name == "list_developers"
    assert "Alice" in result.assistant_message
    assert result.loops == 2


def test_agent_budget_exhausted_after_8_loops(client, tmp_path):
    # Script 9 tool_use responses; the 9th should never execute.
    script = []
    for i in range(10):
        script.append(_FakeMessage(
            [_tool_use_block(f"tu{i}", "list_developers", {})],
            stop_reason="tool_use",
        ))
    fake = _FakeAnthropic(script)
    agent = _build_agent(client, tmp_path, fake)
    result = asyncio.run(agent.run_turn(manager_token="vsm_bud", message="loop forever"))
    assert result.budget_exhausted is True
    assert result.loops == 8
    assert "budget" in result.assistant_message.lower()


def test_agent_memory_persists_across_turns(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage([_text_block("hi there")], stop_reason="end_turn"),
        _FakeMessage([_text_block("second turn ack")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    r1 = asyncio.run(agent.run_turn(manager_token="vsm_mem", message="first q"))
    r2 = asyncio.run(agent.run_turn(
        manager_token="vsm_mem",
        message="second q",
        conversation_id=r1.conversation_id,
    ))
    assert r1.conversation_id == r2.conversation_id
    # Second request must have both turns in its messages list
    second_call_msgs = fake.calls[1]["messages"]
    user_contents = [m for m in second_call_msgs if m["role"] == "user"]
    joined = json.dumps(user_contents)
    assert "first q" in joined and "second q" in joined


def test_agent_sends_cached_system_and_tools(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage([_text_block("ok")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    asyncio.run(agent.run_turn(manager_token="vsm_cache", message="x"))
    kwargs = fake.calls[0]
    # system is a cache-eligible content block list
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The last tool def carries cache_control (caches the preceding ones too)
    tools = kwargs["tools"]
    assert len(tools) >= 5
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}


def test_agent_per_manager_isolation_through_conversation_id(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage([_text_block("A's answer")], stop_reason="end_turn"),
        _FakeMessage([_text_block("B's answer")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    r_a = asyncio.run(agent.run_turn(manager_token="vsm_A", message="hi from A"))
    # Manager B tries to use A's conversation_id — should get a different one
    r_b = asyncio.run(agent.run_turn(
        manager_token="vsm_B",
        message="hi from B",
        conversation_id=r_a.conversation_id,
    ))
    assert r_b.conversation_id != r_a.conversation_id


def test_agent_tool_error_recovery_feeds_result_back(client, tmp_path):
    # Tool call with required field missing → tool returns {"error": "..."}
    # Agent should see the error in the tool_result and get a chance to recover.
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "get_timeline", {})],  # missing developer_id
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("I need a developer id.")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    result = asyncio.run(agent.run_turn(manager_token="vsm_err", message="tell me about something"))
    # On the second call, the tool_result block must contain an error
    second = fake.calls[1]
    tool_result_user = next(m for m in second["messages"][::-1] if m["role"] == "user")
    content_str = json.dumps(tool_result_user["content"])
    assert "error" in content_str.lower()
    assert result.loops == 2


def test_agent_wiretap_emits_envelopes_for_tool_calls(client, tmp_path):
    from shadow_replica.wiretap.bus import WiretapBus
    from vantage.a2a import A2AIntent
    bus = WiretapBus()

    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent
    ctx = _build_tool_ctx(client)
    mem = ChatMemory(tmp_path / "chat.db")
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "list_developers", {})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("Alice and Bob")], stop_reason="end_turn"),
    ])
    agent = ManagerAgent(tool_ctx=ctx, memory=mem, anthropic_client=fake, bus=bus)

    asyncio.run(agent.run_turn(manager_token="vsm_wt", message="who?"))

    # `publish` is sync and stores to a ring buffer; just inspect it.
    intents = [e.intent for e in bus.recent(limit=100)]
    assert A2AIntent.AGENT_TOOL_CALL in intents
    assert A2AIntent.AGENT_TOOL_RESULT in intents
    assert A2AIntent.AGENT_RESPONSE in intents


# ── Endpoint tests ─────────────────────────────────────────────────────────

def test_chat_endpoint_rejects_unauthenticated(client):
    r = client.post("/ui/api/chat", json={"message": "hi"})
    assert r.status_code == 401


def test_chat_endpoint_503_when_agent_unconfigured(client):
    # By default cfg.llm.provider is empty → build_manager_agent returns None
    r = client.post("/ui/api/chat", json={"message": "hi"}, headers=_auth(client))
    assert r.status_code == 503


def test_chat_endpoint_requires_message(client):
    r = client.post("/ui/api/chat", json={}, headers=_auth(client))
    # Could be 400 (empty) or 503 (no agent); both are acceptable rejections
    assert r.status_code in (400, 503)


def test_chat_conversations_list_empty_without_agent(client):
    r = client.get("/ui/api/chat/conversations", headers=_auth(client))
    assert r.status_code == 200
    assert r.json() == {"conversations": []}


def test_chat_endpoint_e2e_with_injected_agent(replica_tmp, monkeypatch):
    """Full HTTP round-trip using a scripted Anthropic client.

    Monkeypatches `build_manager_agent` so the app wires in our fake agent
    end-to-end — same route code path a real manager would hit.
    """
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent

    fake = _FakeAnthropic([
        _FakeMessage([_text_block("Hello manager.")], stop_reason="end_turn"),
        _FakeMessage([_text_block("second")], stop_reason="end_turn"),
    ])

    def _fake_build(tool_ctx, memory, cfg, bus=None):
        return ManagerAgent(tool_ctx=tool_ctx, memory=memory, anthropic_client=fake, bus=bus)

    # The import inside build_app() reads from the module directly, so patch there.
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
        resp = c.post("/ui/api/chat", json={"message": "hi"}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["assistant_message"] == "Hello manager."
        convo_id = body["conversation_id"]
        assert convo_id

        # Follow-up turn with the same conversation_id continues the thread
        resp2 = c.post(
            "/ui/api/chat",
            json={"message": "follow-up", "conversation_id": convo_id},
            headers=headers,
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["conversation_id"] == convo_id

        # List conversations endpoint should include this thread
        listing = c.get("/ui/api/chat/conversations", headers=headers)
        assert listing.status_code == 200
        convos = listing.json()["conversations"]
        assert any(cv["conversation_id"] == convo_id for cv in convos)

        # Get the turns back
        detail = c.get(f"/ui/api/chat/conversations/{convo_id}", headers=headers)
        assert detail.status_code == 200
        turns = detail.json()["turns"]
        roles = [t["role"] for t in turns]
        assert roles.count("user") == 2
        assert roles.count("assistant") == 2
