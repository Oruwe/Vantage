"""Tests for trace_id propagation across the WebSocket probe channel.

Verifies the replica→ghost→replica correlation flow on the WS hop that
``trace.py`` originally skipped (its M17 non-goal). Covers:

  - ProbeRequest / ProbeResponse carry an optional ``trace_id`` field
  - ``WSHub.probe`` embeds the current HTTP trace_id in the outbound payload
  - ``WSHub.probe`` mints a fresh id when no HTTP context exists
  - A caller-supplied ``request.trace_id`` is respected unchanged
  - ``WSHub.converse`` adds the trace_id to discovery_request messages
  - Ghost-side ``ws_client._handle_message`` wraps the probe handler in
    ``use_trace_id`` using the inbound id, and echoes it on the response
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from shadow_replica.connections.ws_hub import WSHub
from shadow_replica.observability import current_trace_id, use_trace_id
from vantage.schemas import (
    PolicyDecision, ProbeRequest, ProbeResponse, RedactionLevel,
)


# ── Schema shape ────────────────────────────────────────────────────────────


def test_probe_request_has_optional_trace_id() -> None:
    req = ProbeRequest(subscriber_id="sub-1")
    assert req.trace_id == ""
    req2 = ProbeRequest(subscriber_id="sub-1", trace_id="abc123")
    assert req2.trace_id == "abc123"


def test_probe_response_has_optional_trace_id() -> None:
    resp = ProbeResponse(
        request_id="r1",
        developer_id="dev-1",
        policy_decision=PolicyDecision.ALLOWED,
    )
    assert resp.trace_id == ""
    resp2 = ProbeResponse(
        request_id="r1",
        developer_id="dev-1",
        policy_decision=PolicyDecision.ALLOWED,
        trace_id="abc123",
    )
    assert resp2.trace_id == "abc123"


# ── Fake WS used by the hub tests ───────────────────────────────────────────


class _FakeWS:
    """Minimal duck-typed WebSocket. Records outbound frames; responses are
    pushed in from the test by resolving the hub's pending future."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def accept(self) -> None:  # pragma: no cover - not hit in these tests
        return None

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:  # pragma: no cover - not hit
        raise AssertionError("unused in these tests")


async def _run_probe_with(hub: WSHub, dev_id: str, ws: _FakeWS, request: ProbeRequest,
                          response_data: dict | None) -> tuple[ProbeResponse | None, list[str]]:
    """Wire the fake WS into the hub, launch probe(), resolve the pending
    future from the side (as if a real ghost had sent a probe_response)."""
    # Inject a connection the hub thinks is live.
    async with hub._lock:
        hub._connections.setdefault(dev_id, {})["machine-a"] = ws  # type: ignore[assignment]

    async def _resolve_later():
        # Wait until the hub has registered a pending future for this request
        # (probe() does this after send_text). Poll briefly, then resolve.
        for _ in range(50):
            if request.request_id in hub._pending:
                break
            await asyncio.sleep(0.005)
        if response_data is not None:
            fut = hub._pending.get(request.request_id)
            if fut and not fut.done():
                fut.set_result(ProbeResponse(**response_data))

    task = asyncio.create_task(_resolve_later())
    resp = await hub.probe(dev_id, request, timeout=1.0)
    await task
    return resp, ws.sent


# ── Hub inherits trace_id from the HTTP context ─────────────────────────────


def test_probe_inherits_trace_id_from_http_context() -> None:
    hub = WSHub()
    ws = _FakeWS()
    dev_id = "dev-1"

    req = ProbeRequest(subscriber_id="sub-1", repo_hint="acme/pay")
    response_payload = {
        "request_id": req.request_id,
        "developer_id": dev_id,
        "policy_decision": "allowed",
        "redaction_level_applied": "standard",
        "trace_id": "httpctxtrace",
    }

    async def _go():
        with use_trace_id("httpctxtrace"):
            return await _run_probe_with(hub, dev_id, ws, req, response_payload)

    resp, sent = asyncio.run(_go())

    assert resp is not None
    assert sent, "hub should have sent a probe_request frame"
    frame = json.loads(sent[0])
    assert frame["type"] == "probe_request"
    assert frame["data"]["trace_id"] == "httpctxtrace"
    # And the ProbeRequest object itself was stamped with the trace_id
    assert req.trace_id == "httpctxtrace"


def test_probe_mints_trace_id_without_http_context() -> None:
    hub = WSHub()
    ws = _FakeWS()
    dev_id = "dev-2"

    req = ProbeRequest(subscriber_id="sub-1")
    response_payload = {
        "request_id": req.request_id,
        "developer_id": dev_id,
        "policy_decision": "allowed",
        "redaction_level_applied": "standard",
    }

    async def _go():
        assert current_trace_id() == "-"
        return await _run_probe_with(hub, dev_id, ws, req, response_payload)

    resp, sent = asyncio.run(_go())
    assert resp is not None
    frame = json.loads(sent[0])
    assert frame["data"]["trace_id"], "a trace_id must be minted even outside HTTP"
    assert frame["data"]["trace_id"] == req.trace_id


def test_probe_respects_caller_supplied_trace_id() -> None:
    """If the caller explicitly set request.trace_id, keep it — even inside an
    HTTP context with a different trace. Rare but useful for scheduled probes
    that want to tag with a job id instead of the triggering request."""
    hub = WSHub()
    ws = _FakeWS()
    dev_id = "dev-3"

    req = ProbeRequest(subscriber_id="sub-1", trace_id="caller-supplied")
    response_payload = {
        "request_id": req.request_id,
        "developer_id": dev_id,
        "policy_decision": "allowed",
        "redaction_level_applied": "standard",
    }

    async def _go():
        with use_trace_id("other-http-trace"):
            return await _run_probe_with(hub, dev_id, ws, req, response_payload)

    resp, sent = asyncio.run(_go())
    assert resp is not None
    assert req.trace_id == "caller-supplied"
    frame = json.loads(sent[0])
    assert frame["data"]["trace_id"] == "caller-supplied"


# ── Discovery requests ──────────────────────────────────────────────────────


def test_converse_embeds_trace_id_in_discovery_request() -> None:
    hub = WSHub()
    ws = _FakeWS()
    dev_id = "dev-4"

    async def _go():
        async with hub._lock:
            hub._connections.setdefault(dev_id, {})["machine-a"] = ws  # type: ignore[assignment]

        async def _resolve():
            for _ in range(50):
                if hub._pending_disco:
                    break
                await asyncio.sleep(0.005)
            for req_id, fut in list(hub._pending_disco.items()):
                if not fut.done():
                    fut.set_result({
                        "developer_id": dev_id,
                        "answer": "ok",
                        "policy_decision": "allowed",
                    })

        task = asyncio.create_task(_resolve())
        with use_trace_id("convotrace12"):
            results = await hub.converse("hi", developer_ids=[dev_id], timeout=1.0)
        await task
        return results

    results = asyncio.run(_go())
    assert results, "converse should return at least one result"
    sent_frames = [json.loads(s) for s in ws.sent]
    discovery = [f for f in sent_frames if f.get("type") == "discovery_request"]
    assert discovery, "a discovery_request frame should have been sent"
    assert discovery[0]["data"]["trace_id"] == "convotrace12"
    assert discovery[0]["data"]["prompt"] == "hi"


# ── Ghost-side handler wraps probe in use_trace_id ──────────────────────────


def test_ws_client_probe_handler_uses_inbound_trace_id() -> None:
    """The ghost's _handle_message should enter use_trace_id BEFORE calling
    the probe_agent, so any log line produced by the agent is correlated."""
    from ghost_daemon.transport.ws_client import WSClient

    captured_trace: dict[str, Any] = {}

    class _StubProbeAgent:
        async def run(self, request: ProbeRequest) -> ProbeResponse:
            # Capture what the ContextVar says at the moment of handling.
            from ghost_daemon.observability import current_trace_id as cur
            captured_trace["seen_in_agent"] = cur()
            captured_trace["request_trace_id"] = request.trace_id
            return ProbeResponse(
                request_id=request.request_id,
                developer_id="dev-x",
                policy_decision=PolicyDecision.ALLOWED,
                redaction_level_applied=RedactionLevel.STANDARD,
            )

    class _StubWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    client = WSClient.__new__(WSClient)
    client._subscriber = type("S", (), {"subscriber_id": "sub-x"})()  # type: ignore[attr-defined]
    client._developer_id = "dev-x"
    client._machine_id = "m-x"
    client._probe_agent = _StubProbeAgent()  # type: ignore[assignment]
    client._discovery_agent = None
    client._tailer = None
    client._cfg = None
    client._running = True
    client._on_connect = None
    client._on_disconnect = None

    ws = _StubWS()
    raw = json.dumps({
        "type": "probe_request",
        "data": {
            "request_id": "probe-xyz",
            "subscriber_id": "sub-x",
            "repo_hint": "",
            "trace_id": "inboundtraceid",
        },
    })

    asyncio.run(client._handle_message(ws, raw))

    assert captured_trace.get("seen_in_agent") == "inboundtraceid"
    assert captured_trace.get("request_trace_id") == "inboundtraceid"
    # Response echoes the trace back for the replica's post-hop logs.
    assert ws.sent, "ghost must send a probe_response"
    sent_frame = json.loads(ws.sent[0])
    assert sent_frame["type"] == "probe_response"
    assert sent_frame["data"]["trace_id"] == "inboundtraceid"


def test_ws_client_sanitizes_malformed_inbound_trace_id() -> None:
    """A probe_request carrying an unsafe trace_id (e.g. header injection)
    must not leak into the ghost's log context — use_trace_id replaces it."""
    from ghost_daemon.transport.ws_client import WSClient

    seen: dict[str, Any] = {}

    class _StubProbeAgent:
        async def run(self, request: ProbeRequest) -> ProbeResponse:
            from ghost_daemon.observability import current_trace_id as cur
            seen["trace"] = cur()
            seen["request_trace"] = request.trace_id
            return ProbeResponse(
                request_id=request.request_id,
                developer_id="dev-x",
                policy_decision=PolicyDecision.ALLOWED,
            )

    class _StubWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    client = WSClient.__new__(WSClient)
    client._subscriber = type("S", (), {"subscriber_id": "sub-x"})()  # type: ignore[attr-defined]
    client._developer_id = "dev-x"
    client._probe_agent = _StubProbeAgent()  # type: ignore[assignment]
    client._discovery_agent = None
    client._running = True

    raw = json.dumps({
        "type": "probe_request",
        "data": {
            "request_id": "probe-xyz",
            "subscriber_id": "sub-x",
            "trace_id": "bad; drop table users",
        },
    })

    asyncio.run(client._handle_message(_StubWS(), raw))

    sanitized = seen.get("trace") or ""
    # 12 hex chars: the ghost-side use_trace_id minted a replacement.
    import re
    assert re.fullmatch(r"[0-9a-f]{12}", sanitized), f"got {sanitized!r}"
    assert seen.get("request_trace") == sanitized
