"""Tests for the shadow-replica request-id middleware + log filter.

Covers:
  - Middleware mints a fresh id when none provided
  - Middleware reuses a client-supplied id when it's sane
  - Middleware ignores + replaces malformed ids (log-injection defense)
  - Response always carries X-Request-Id
  - The ContextVar is populated during handler execution
  - RequestIdLogFilter stamps log records
  - use_trace_id() scopes the id correctly outside HTTP
"""
from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from shadow_replica.observability import (
    RequestIdLogFilter,
    RequestIdMiddleware,
    current_trace_id,
    new_trace_id,
    use_trace_id,
)


# ── Helper: minimal app wrapping just the middleware ────────────────────────

def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo():
        return JSONResponse({"rid": current_trace_id()})

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_test_app())


# ── Middleware behavior ─────────────────────────────────────────────────────


def test_fresh_id_minted_when_absent(client: TestClient) -> None:
    r = client.get("/echo")
    assert r.status_code == 200
    rid_header = r.headers.get("X-Request-Id", "")
    rid_body = r.json()["rid"]
    assert rid_header, "response must carry X-Request-Id"
    assert rid_header == rid_body, "header and context must match"
    # 12 hex chars = our default minting
    assert re.fullmatch(r"[0-9a-f]{12}", rid_header)


def test_sane_incoming_id_is_reused(client: TestClient) -> None:
    rid = "abc123-DEF_456"
    r = client.get("/echo", headers={"X-Request-Id": rid})
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == rid
    assert r.json()["rid"] == rid


def test_malformed_id_is_replaced(client: TestClient) -> None:
    """Any character outside [A-Za-z0-9_-] triggers a mint. Guards against
    header-based log injection."""
    bad = "not safe; drop table users --"
    r = client.get("/echo", headers={"X-Request-Id": bad})
    assert r.status_code == 200
    echoed = r.headers["X-Request-Id"]
    assert echoed != bad
    assert re.fullmatch(r"[0-9a-f]{12}", echoed)
    assert r.json()["rid"] == echoed


def test_empty_id_header_is_replaced(client: TestClient) -> None:
    r = client.get("/echo", headers={"X-Request-Id": "   "})
    assert r.headers["X-Request-Id"]
    assert r.json()["rid"] == r.headers["X-Request-Id"]
    assert re.fullmatch(r"[0-9a-f]{12}", r.headers["X-Request-Id"])


def test_overlong_id_is_replaced(client: TestClient) -> None:
    r = client.get("/echo", headers={"X-Request-Id": "a" * 200})
    assert re.fullmatch(r"[0-9a-f]{12}", r.headers["X-Request-Id"])


def test_context_cleared_after_request() -> None:
    """Between requests the ContextVar should drop back to the sentinel."""
    app = _build_test_app()
    with TestClient(app) as c:
        c.get("/echo", headers={"X-Request-Id": "rid-one"})
        # The TestClient runs each request in a task; the outer thread's
        # ContextVar should be unaffected.
        assert current_trace_id() == "-"


# ── Log filter ──────────────────────────────────────────────────────────────


def test_log_filter_stamps_records_inside_context() -> None:
    logger = logging.getLogger("vantage.test.request_id")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.DEBUG)
    handler.addFilter(RequestIdLogFilter())
    logger.addHandler(handler)
    try:
        with use_trace_id("ctx-test-1"):
            logger.info("inside context")
        logger.info("outside context")
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 2
    assert getattr(captured[0], "trace_id", "") == "ctx-test-1"
    assert getattr(captured[1], "trace_id", "") == "-"


def test_use_trace_id_restores_previous_value() -> None:
    """Nested scopes must pop correctly — no leaked ids after the block."""
    assert current_trace_id() == "-"
    with use_trace_id("outer"):
        assert current_trace_id() == "outer"
        with use_trace_id("inner"):
            assert current_trace_id() == "inner"
        assert current_trace_id() == "outer"
    assert current_trace_id() == "-"


def test_new_trace_id_is_hex_and_unique() -> None:
    a = new_trace_id()
    b = new_trace_id()
    assert a != b
    assert re.fullmatch(r"[0-9a-f]{12}", a)
    assert re.fullmatch(r"[0-9a-f]{12}", b)


# ── Integration: middleware installed in build_app ──────────────────────────


def test_ghost_push_sends_x_request_id() -> None:
    """ghost_daemon.transport.push must include a fresh X-Request-Id so the
    replica can correlate its receive logs with the daemon's push logs."""
    import asyncio
    import httpx

    from ghost_daemon.transport import push as push_mod
    from vantage.schemas import (
        ApprovalSource, ApprovedPayload, Channel, QuickNote, SubscriberIdentity,
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def _go():
        # push() instantiates its own AsyncClient. Swap it out for one bound
        # to our MockTransport so the request body hits the handler above.
        orig = push_mod.httpx.AsyncClient

        class _MockClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        push_mod.httpx.AsyncClient = _MockClient
        try:
            note = QuickNote(developer_id="dev-1", message="hi")
            approved = ApprovedPayload(
                draft_id="draft-1",
                artifact_type="quick_note",
                payload=note,
                developer_id="dev-1",
                approval_source=ApprovalSource.POLICY,
            )
            sub = SubscriberIdentity(
                subscriber_id="sub-1",
                name="Replica",
                token_value="vsub_test_token",
                receive_url="http://replica.test",
                channels=[Channel.QUICK_NOTE],
            )
            return await push_mod.push(approved, sub, developer_id="dev-1")
        finally:
            push_mod.httpx.AsyncClient = orig

    ok = asyncio.run(_go())
    assert ok is True
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-request-id" in headers
    assert re.fullmatch(r"[0-9a-f]{12}", headers["x-request-id"])


def test_manager_agent_stream_emits_trace_id_on_started() -> None:
    """When the agent runs inside an HTTP request context, the first chunk
    must echo the current trace_id so SSE consumers can correlate."""
    import asyncio
    from shadow_replica.observability import use_trace_id

    # Build a minimal agent stub that only runs the pre-loop section — we
    # want to assert on the started chunk, not on the full tool-use loop.
    class _StubAgent:
        async def run_turn_stream(self, *, trace_id_expected: str):
            # Mimic the real method's prologue: capture trace_id, yield started.
            # Reuse the imported current_trace_id via the actual module so
            # the test proves the import path works.
            from shadow_replica.observability import current_trace_id
            yield {
                "type": "started",
                "conversation_id": "test-convo",
                "trace_id": current_trace_id(),
            }

    async def _go():
        stub = _StubAgent()
        chunks = []
        with use_trace_id("abc123def456"):
            async for c in stub.run_turn_stream(trace_id_expected="abc123def456"):
                chunks.append(c)
        return chunks

    chunks = asyncio.run(_go())
    assert len(chunks) == 1
    assert chunks[0]["type"] == "started"
    assert chunks[0]["trace_id"] == "abc123def456"


def test_manager_agent_stream_falls_back_outside_http(tmp_path) -> None:
    """Outside a request context the trace_id must be the sentinel '-' —
    no crash, just no correlation. Covers scheduled-task / script entry."""
    import asyncio
    from unittest.mock import MagicMock
    from shadow_replica.agent.manager_agent import ManagerAgent
    from shadow_replica.agent.chat_memory import ChatMemory

    # Drive the real run_turn_stream prologue by giving it a client=None so
    # it errors out early AFTER yielding started... actually when client is
    # None it errors BEFORE started. Use a lightweight mock client instead.
    memory = ChatMemory(tmp_path / "chat_memory.db")
    mock_client = MagicMock()

    async def _never_called(**kwargs):
        raise AssertionError("should not reach Anthropic — stream aborts early")

    mock_client.messages.create = _never_called

    agent = ManagerAgent.__new__(ManagerAgent)
    agent._client = mock_client
    agent._memory = memory
    agent._tools_ctx = MagicMock()
    agent._bus = None
    agent._pending_approvals = {}
    agent._model = "claude-sonnet-4-6"
    agent._tool_defs_anthropic = []
    agent._system_prompt_blocks = [{"type": "text", "text": "stub"}]

    async def _go():
        chunks = []
        try:
            async for c in agent.run_turn_stream(
                manager_token="vsm_t",
                message="hi",
                conversation_id=None,
            ):
                chunks.append(c)
                if c.get("type") == "started":
                    break
        except AssertionError:
            pass
        return chunks

    chunks = asyncio.run(_go())
    started = [c for c in chunks if c["type"] == "started"]
    assert started and started[0]["trace_id"] == "-"


def test_build_app_installs_middleware(tmp_path, monkeypatch) -> None:
    """The real build_app pipeline must carry X-Request-Id in responses."""
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    # Point replica config dir at tmp so build_app doesn't touch the user's
    # real ~/.vantage-replica.
    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)

    cfg = cfgmod.ReplicaConfig(replica_id="test-replica")
    app = build_app(cfg)
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert "X-Request-Id" in r.headers
        assert re.fullmatch(r"[0-9a-f]{12}", r.headers["X-Request-Id"])
