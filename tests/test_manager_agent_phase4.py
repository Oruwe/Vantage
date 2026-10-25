"""Phase 4 — dangerous actions behind manager confirmation.

Covers:
  - probe_live + ask_dev registered with requires_confirmation=True
  - requires_confirmation() helper for other callers
  - Non-streaming run_turn synthesizes "use streaming endpoint" for dangerous tools
  - Streaming run_turn_stream pauses on a dangerous tool and emits
    requires_confirmation + pending_approval done
  - resume_after_approval(approved=True) runs the tool
  - resume_after_approval(approved=False) synthesizes a denied result and the
    agent gets a chance to recover
  - Non-matching manager tokens cannot resume someone else's pending call
  - POST /ui/api/chat/approve endpoint: auth + full e2e flow
  - POST /ui/api/chat/pending/{id} returns the paused args before approval
  - UI assets include the Approve/Deny controls + resume handler
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def _auth(client, token=None):
    return {"Authorization": f"Bearer {token or client._manager}"}


# ── Helpers (reused from earlier phases) ────────────────────────────────────

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


# ── Fake Anthropic ──────────────────────────────────────────────────────────

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
            return _FakeMessage([_text_block("(exhausted)")], stop_reason="end_turn")
        return self._responses.pop(0)


def _build_agent(client, tmp_path, fake_client, bus=None):
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent
    ctx = _build_tool_ctx(client)
    mem = ChatMemory(tmp_path / "chat.db")
    return ManagerAgent(tool_ctx=ctx, memory=mem, anthropic_client=fake_client, bus=bus)


# ── Tool registration ──────────────────────────────────────────────────────

def test_dangerous_tools_registered():
    from shadow_replica.agent.tools import tool_names, anthropic_schemas, requires_confirmation
    names = set(tool_names())
    assert "probe_live" in names
    assert "ask_dev" in names
    # Must count 12 total now (10 phase-2 + 2 phase-4)
    schemas = anthropic_schemas()
    assert len(schemas) == 12
    # Flag plumbed to lookup helper
    assert requires_confirmation("probe_live") is True
    assert requires_confirmation("ask_dev") is True
    assert requires_confirmation("search_artifacts") is False
    assert requires_confirmation("nonexistent") is False


def test_dangerous_tool_schema_descriptions_mention_confirmation():
    from shadow_replica.agent.tools import anthropic_schemas
    schemas = {s["name"]: s for s in anthropic_schemas()}
    # Signal to the LLM that these are different in character
    assert "confirm" in schemas["probe_live"]["description"].lower() or \
           "approval" in schemas["probe_live"]["description"].lower()
    assert "confirm" in schemas["ask_dev"]["description"].lower() or \
           "approval" in schemas["ask_dev"]["description"].lower()


# ── Non-streaming run_turn refuses dangerous tools ──────────────────────────

def test_non_streaming_endpoint_synthesizes_streaming_hint(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "probe_live", {"developer_id": "alice"})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("As requested, use the streaming endpoint.")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)
    result = asyncio.run(agent.run_turn(manager_token="vsm_t", message="probe Alice"))
    # Tool returned a hint, not an actual probe
    assert len(result.tool_calls) == 1
    assert "requires_streaming_endpoint" in result.tool_calls[0].tool_output_preview


# ── Streaming pause behavior ────────────────────────────────────────────────

def test_stream_pauses_on_dangerous_tool(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "probe_live", {"developer_id": "alice"})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("Alice is editing auth.py")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _collect():
        chunks = []
        async for c in agent.run_turn_stream(manager_token="vsm_a", message="probe Alice"):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_collect())
    # Should see: started, tool_call (confirmation), done (pending). No tool_result.
    types = [c["type"] for c in chunks]
    assert types == ["started", "tool_call", "done"]
    tc = next(c for c in chunks if c["type"] == "tool_call")
    assert tc["requires_confirmation"] is True
    assert tc["tool"] == "probe_live"
    done = chunks[-1]
    assert done["pending_approval"] is True
    assert done["pending_tool_use_id"] == "tu1"
    assert done["stop_reason"] == "awaiting_approval"
    # Second model call must NOT have been made yet
    assert len(fake.calls) == 1


def test_pending_approval_can_be_inspected(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu7", "probe_live", {"developer_id": "alice"})],
            stop_reason="tool_use",
        ),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _run():
        async for _ in agent.run_turn_stream(manager_token="vsm_i", message="peek"):
            pass
    asyncio.run(_run())

    peek = agent.pending_approval("vsm_i", "tu7")
    assert peek is not None
    assert peek["tool_name"] == "probe_live"
    assert peek["tool_input"]["developer_id"] == "alice"
    # Wrong manager cannot peek
    assert agent.pending_approval("vsm_different", "tu7") is None


# ── Resume: approved ────────────────────────────────────────────────────────

def test_resume_approved_runs_tool_and_continues(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu1", "probe_live", {"developer_id": "alice"})],
            stop_reason="tool_use",
        ),
        _FakeMessage(
            [_text_block("Alice is probing cleanly.")],
            stop_reason="end_turn",
        ),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _run():
        async for _ in agent.run_turn_stream(manager_token="vsm_r", message="probe"):
            pass
        chunks = []
        async for c in agent.resume_after_approval(
            manager_token="vsm_r", tool_use_id="tu1", approved=True
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())
    types = [c["type"] for c in chunks]
    # tool_result → message_delta → done
    assert "tool_result" in types
    assert "message_delta" in types
    assert types[-1] == "done"
    # The resumed tool_result carries approved=True
    tr = next(c for c in chunks if c["type"] == "tool_result")
    assert tr["approved"] is True
    # The second model call must now have been made (resume unpauses it)
    assert len(fake.calls) == 2
    # And the done chunk is not pending_approval
    done = chunks[-1]
    assert done.get("pending_approval") is not True
    # The pending state must have been consumed
    assert agent.pending_approval("vsm_r", "tu1") is None


# ── Resume: denied ─────────────────────────────────────────────────────────

def test_resume_denied_feeds_refusal_to_agent(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu2", "ask_dev",
                             {"developer_id": "alice", "question": "status?"})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("Understood — I won't ask Alice.")], stop_reason="end_turn"),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _run():
        async for _ in agent.run_turn_stream(manager_token="vsm_d", message="ask alice"):
            pass
        chunks = []
        async for c in agent.resume_after_approval(
            manager_token="vsm_d", tool_use_id="tu2", approved=False
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())
    tr = next(c for c in chunks if c["type"] == "tool_result")
    assert tr["approved"] is False
    # The denial message must have been serialized to the agent
    import json as _json
    preview = tr["preview"]
    data = _json.loads(preview)
    assert data["status"] == "denied_by_manager"
    # Agent continues cleanly
    done = chunks[-1]
    assert done["type"] == "done"
    assert done.get("pending_approval") is not True


# ── Resume authorization ────────────────────────────────────────────────────

def test_resume_rejects_different_manager(client, tmp_path):
    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tuX", "probe_live", {"developer_id": "alice"})],
            stop_reason="tool_use",
        ),
    ])
    agent = _build_agent(client, tmp_path, fake)

    async def _run():
        async for _ in agent.run_turn_stream(manager_token="vsm_owner", message="probe"):
            pass
        chunks = []
        async for c in agent.resume_after_approval(
            manager_token="vsm_someoneelse", tool_use_id="tuX", approved=True
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert "not authorized" in chunks[0]["detail"]
    # Pending state still there — owner can still approve
    assert agent.pending_approval("vsm_owner", "tuX") is not None


# ── Resume with unknown id ─────────────────────────────────────────────────

def test_resume_unknown_id_is_error(client, tmp_path):
    fake = _FakeAnthropic([])
    agent = _build_agent(client, tmp_path, fake)

    async def _run():
        chunks = []
        async for c in agent.resume_after_approval(
            manager_token="vsm_x", tool_use_id="nonsense", approved=True
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"


# ── Endpoint tests ─────────────────────────────────────────────────────────

def test_approve_endpoint_rejects_unauthenticated(client):
    r = client.post("/ui/api/chat/approve", json={"tool_use_id": "x", "approved": True})
    assert r.status_code == 401


def test_approve_endpoint_503_without_agent(client):
    r = client.post("/ui/api/chat/approve",
                    json={"tool_use_id": "x", "approved": True},
                    headers=_auth(client))
    assert r.status_code == 503


def test_pending_endpoint_404_without_pending(client, replica_tmp, monkeypatch):
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent

    fake = _FakeAnthropic([])

    def _fake_build(tool_ctx, memory, cfg, bus=None):
        return ManagerAgent(tool_ctx=tool_ctx, memory=memory, anthropic_client=fake, bus=bus)

    monkeypatch.setattr("shadow_replica.agent.manager_agent.build_manager_agent", _fake_build)

    from shadow_replica.app import build_app
    from shadow_replica.config import load, TrustedDev
    cfg = load()
    cfg.qdrant.enabled = False
    app = build_app(cfg)
    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {cfg.manager_token}"}
        r = c.get("/ui/api/chat/pending/unknown-id", headers=headers)
        assert r.status_code == 404


def test_approve_endpoint_e2e(replica_tmp, monkeypatch):
    """Full flow: /chat/stream pauses, GET /chat/pending/{id}, POST /chat/approve resumes."""
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent

    fake = _FakeAnthropic([
        _FakeMessage(
            [_tool_use_block("tu_e2e", "probe_live", {"developer_id": "alice"})],
            stop_reason="tool_use",
        ),
        _FakeMessage([_text_block("All clear.")], stop_reason="end_turn"),
    ])

    def _fake_build(tool_ctx, memory, cfg, bus=None):
        return ManagerAgent(tool_ctx=tool_ctx, memory=memory, anthropic_client=fake, bus=bus)

    monkeypatch.setattr("shadow_replica.agent.manager_agent.build_manager_agent", _fake_build)

    from shadow_replica.app import build_app
    from shadow_replica.config import load, TrustedDev
    cfg = load()
    cfg.qdrant.enabled = False
    cfg.trusted_devs.append(TrustedDev(developer_id="alice", name="Alice", token_value="vsub_alice"))
    app = build_app(cfg)
    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {cfg.manager_token}"}

        # Step 1 — streaming pauses on the dangerous tool
        with c.stream("POST", "/ui/api/chat/stream",
                      json={"message": "probe alice"},
                      headers=headers) as resp:
            body = "".join(resp.iter_text())
        events = _parse_sse(body)
        names = [e[0] for e in events]
        assert "tool_call" in names
        assert names[-1] == "done"
        done_payload = events[-1][1]
        assert done_payload["pending_approval"] is True
        tool_use_id = done_payload["pending_tool_use_id"]

        # Step 2 — manager peeks at the pending args
        peek = c.get(f"/ui/api/chat/pending/{tool_use_id}", headers=headers)
        assert peek.status_code == 200
        peek_body = peek.json()
        assert peek_body["tool_name"] == "probe_live"
        assert peek_body["tool_input"] == {"developer_id": "alice"}

        # Step 3 — manager approves; resume stream must produce tool_result + done
        with c.stream("POST", "/ui/api/chat/approve",
                      json={"tool_use_id": tool_use_id, "approved": True},
                      headers=headers) as resp2:
            body2 = "".join(resp2.iter_text())
        events2 = _parse_sse(body2)
        names2 = [e[0] for e in events2]
        assert "tool_result" in names2
        assert "message_delta" in names2
        assert names2[-1] == "done"
        done2 = events2[-1][1]
        assert done2.get("pending_approval") is not True

        # Step 4 — pending state should now be consumed
        peek2 = c.get(f"/ui/api/chat/pending/{tool_use_id}", headers=headers)
        assert peek2.status_code == 404


# ── UI static assertions ───────────────────────────────────────────────────

def test_ui_has_approval_controls():
    html = (Path(__file__).resolve().parents[1] / "packages" / "shadow-replica"
            / "src" / "shadow_replica" / "static" / "index.html").read_text(encoding="utf-8")
    assert "agAddConfirmControls" in html
    assert "agResolveApproval" in html
    # Buttons visible to the manager
    assert "ag-btn-approve" in html
    assert "ag-btn-deny" in html
    # Resume path hits the correct endpoint
    assert "/ui/api/chat/approve" in html
    # The paused branch is explicitly handled
    assert "requires_confirmation" in html
    assert "pending_approval" in html


def test_ui_dangerous_tool_warning_present():
    html = (Path(__file__).resolve().parents[1] / "packages" / "shadow-replica"
            / "src" / "shadow_replica" / "static" / "index.html").read_text(encoding="utf-8")
    # Confirmation message mentions "approval" or "live"
    assert ("Needs approval" in html or "approval" in html.lower())
    assert "touch a developer" in html.lower() or "live session" in html.lower()


# ── SSE parser ─────────────────────────────────────────────────────────────

def _parse_sse(body: str):
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
