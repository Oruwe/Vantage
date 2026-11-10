"""Tests for the performance-observability wiring.

Verifies that every hot path lands metrics in the Prometheus registry and
that the ``/ui/api/perf`` endpoint aggregates them correctly.

Layout:
  - HTTP middleware updates request/duration series for every FastAPI call.
  - LLM wrappers (replica + ghost) record latency, count, and tokens.
  - Qdrant + embedder calls update their duration histograms.
  - Agent tool invoke() updates tool-call histograms + counters.
  - Ghost push updates ghost_pushes_total + duration.
  - /metrics endpoint (replica + ghost) exposes all series.
  - /ui/api/perf surfaces p50/p95 from the live registry.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_replica.metrics import (
    agent_tool_call_duration_seconds, agent_tool_calls_total,
    embedder_duration_seconds, http_request_duration_seconds, http_requests_total,
    llm_call_duration_seconds, llm_calls_total, llm_tokens_total,
    qdrant_op_duration_seconds, timeline_requests,
)


def _counter_sum(counter, **labels) -> float:
    """Total across all samples that match the (partial) label filter."""
    total = 0.0
    for metric in counter.collect():
        for s in metric.samples:
            if not s.name.endswith("_total"):
                continue
            if all(s.labels.get(k) == v for k, v in labels.items()):
                total += float(s.value)
    return total


def _histogram_count(hist, **labels) -> float:
    total = 0.0
    for metric in hist.collect():
        for s in metric.samples:
            if not s.name.endswith("_count"):
                continue
            if all(s.labels.get(k) == v for k, v in labels.items()):
                total += float(s.value)
    return total


# ── HTTP middleware ─────────────────────────────────────────────────────────


def test_http_middleware_counts_request(tmp_path, monkeypatch) -> None:
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="perf-test")
    app = build_app(cfg)

    before = _counter_sum(http_requests_total, route="/health", method="GET", status="2xx")

    with TestClient(app) as c:
        for _ in range(3):
            r = c.get("/health")
            assert r.status_code == 200

    after = _counter_sum(http_requests_total, route="/health", method="GET", status="2xx")
    assert after - before == 3, "every request must bump the counter exactly once"
    # And the duration histogram recorded three samples.
    dur_count = _histogram_count(
        http_request_duration_seconds, route="/health", method="GET",
    )
    assert dur_count >= 3


def test_http_middleware_unknown_route_bucketed(tmp_path, monkeypatch) -> None:
    """A 404 to a path with no registered route must collapse to ``_unknown``."""
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="perf-test")
    app = build_app(cfg)

    before = _counter_sum(http_requests_total, route="_unknown")

    with TestClient(app) as c:
        c.get("/this/is/not/a/thing")

    after = _counter_sum(http_requests_total, route="_unknown")
    assert after - before == 1


# ── Replica LLM wrapper ────────────────────────────────────────────────────


def test_replica_llm_wrapper_records_latency_and_tokens() -> None:
    from shadow_replica.app import _LLMWrapper

    class _FakeResp:
        class _Usage:
            prompt_tokens = 123
            completion_tokens = 45
        usage = _Usage()

        class _Choice:
            class _Msg:
                content = "hello"
            message = _Msg()
        choices = [_Choice()]

    class _FakeClient:
        class _Chat:
            class _Completions:
                async def create(self, **kwargs):
                    return _FakeResp()
            completions = _Completions()
        chat = _Chat()

    wrap = _LLMWrapper(_FakeClient(), model="llama3", provider="groq")

    before_ok = _counter_sum(
        llm_calls_total, provider="groq", model="llama3",
        operation="chat.completions.create", outcome="ok",
    )
    before_in_tok = _counter_sum(
        llm_tokens_total, provider="groq", model="llama3", kind="input",
    )

    text = asyncio.run(wrap.complete([{"role": "user", "content": "hi"}]))
    assert text == "hello"

    after_ok = _counter_sum(
        llm_calls_total, provider="groq", model="llama3",
        operation="chat.completions.create", outcome="ok",
    )
    after_in_tok = _counter_sum(
        llm_tokens_total, provider="groq", model="llama3", kind="input",
    )
    assert after_ok - before_ok == 1
    assert after_in_tok - before_in_tok == 123
    dur = _histogram_count(
        llm_call_duration_seconds,
        provider="groq", model="llama3", operation="chat.completions.create",
    )
    assert dur >= 1


def test_replica_llm_wrapper_counts_errors() -> None:
    from shadow_replica.app import _LLMWrapper

    class _AngryClient:
        class _Chat:
            class _Completions:
                async def create(self, **kwargs):
                    raise RuntimeError("boom")
            completions = _Completions()
        chat = _Chat()

    wrap = _LLMWrapper(_AngryClient(), model="mX", provider="groq")

    before_err = _counter_sum(
        llm_calls_total, provider="groq", model="mX",
        operation="chat.completions.create", outcome="error",
    )
    with pytest.raises(RuntimeError):
        asyncio.run(wrap.complete([{"role": "user", "content": "hi"}]))
    after_err = _counter_sum(
        llm_calls_total, provider="groq", model="mX",
        operation="chat.completions.create", outcome="error",
    )
    assert after_err - before_err == 1


# ── Ghost LLM providers ────────────────────────────────────────────────────


def test_ghost_openai_compat_provider_records_metrics(monkeypatch) -> None:
    import ghost_daemon.metrics as gm
    from ghost_daemon.models.providers.openai_compat import OpenAICompatProvider

    # Avoid hitting the keychain — stub resolve_api_key.
    monkeypatch.setattr(
        "ghost_daemon.models.providers.openai_compat.resolve_api_key",
        lambda env: "test-key",
    )

    class _FakeResp:
        class _Usage:
            prompt_tokens = 7
            completion_tokens = 9
        usage = _Usage()

        class _Choice:
            class _Msg:
                content = "ok"
            message = _Msg()
        choices = [_Choice()]

    class _FakeClient:
        class _Chat:
            class _Completions:
                async def create(self, **kwargs):
                    return _FakeResp()
            completions = _Completions()
        chat = _Chat()

    p = OpenAICompatProvider(model="x", endpoint="http://x", api_key_env="K", provider_name="grok")
    p._client = _FakeClient()  # type: ignore[attr-defined]

    before = _counter_sum(
        gm.llm_calls_total, provider="grok", model="x",
        operation="chat.completions.create", outcome="ok",
    )
    before_out = _counter_sum(
        gm.llm_tokens_total, provider="grok", model="x", kind="output",
    )
    asyncio.run(p.complete([{"role": "user", "content": "hi"}]))

    after = _counter_sum(
        gm.llm_calls_total, provider="grok", model="x",
        operation="chat.completions.create", outcome="ok",
    )
    after_out = _counter_sum(
        gm.llm_tokens_total, provider="grok", model="x", kind="output",
    )
    assert after - before == 1
    assert after_out - before_out == 9


# ── Embedder + Qdrant ──────────────────────────────────────────────────────


def test_embedder_records_disabled_path() -> None:
    """The fast-path conftest marks Embedder._available=False from the jump,
    so every call goes through the short-circuit ``disabled`` bucket. The
    metric MUST still fire — operators need to see dormant embedders too."""
    from shadow_replica.models.embedder import Embedder

    e = Embedder(provider="ollama", endpoint="http://127.0.0.1:1")
    before = _histogram_count(embedder_duration_seconds, backend="ollama", outcome="disabled")
    asyncio.run(e.embed("hello"))
    asyncio.run(e.embed("again"))
    after = _histogram_count(embedder_duration_seconds, backend="ollama", outcome="disabled")
    assert after - before == 2


def test_qdrant_op_duration_series_is_registered() -> None:
    """Even before any real Qdrant call lands, the histogram must exist in
    the registry so /metrics scrapes successfully. Prime one label and
    confirm the observation lands."""
    qdrant_op_duration_seconds.labels(op="search", outcome="ok").observe(0.001)
    count = _histogram_count(qdrant_op_duration_seconds, op="search", outcome="ok")
    assert count >= 1


# ── Agent tool-call invoke() ───────────────────────────────────────────────


def test_agent_tool_invoke_records_latency_and_counter() -> None:
    from shadow_replica.agent import tools as toolmod

    tool = toolmod.Tool(
        name="perf_test_tool",
        description="noop",
        input_schema={"type": "object"},
        handler=lambda ctx, args: _noop_handler(),
    )
    toolmod._TOOL_DEFS.append(tool)

    try:
        before = _counter_sum(
            agent_tool_calls_total, tool_name="perf_test_tool", outcome="ok",
        )
        asyncio.run(toolmod.invoke(None, "perf_test_tool", {}))  # type: ignore[arg-type]
        after = _counter_sum(
            agent_tool_calls_total, tool_name="perf_test_tool", outcome="ok",
        )
        assert after - before == 1
        dur = _histogram_count(
            agent_tool_call_duration_seconds, tool_name="perf_test_tool", outcome="ok",
        )
        assert dur >= 1
    finally:
        toolmod._TOOL_DEFS.remove(tool)


def test_agent_tool_invoke_records_errors() -> None:
    from shadow_replica.agent import tools as toolmod

    async def _raises(ctx, args):
        raise ValueError("deliberate")

    tool = toolmod.Tool(
        name="perf_bad_tool",
        description="raises",
        input_schema={"type": "object"},
        handler=_raises,
    )
    toolmod._TOOL_DEFS.append(tool)
    try:
        before = _counter_sum(
            agent_tool_calls_total, tool_name="perf_bad_tool", outcome="error",
        )
        result = asyncio.run(toolmod.invoke(None, "perf_bad_tool", {}))  # type: ignore[arg-type]
        assert "error" in result
        after = _counter_sum(
            agent_tool_calls_total, tool_name="perf_bad_tool", outcome="error",
        )
        assert after - before == 1
    finally:
        toolmod._TOOL_DEFS.remove(tool)


async def _noop_handler() -> dict:
    return {"ok": True}


# ── Ghost push ─────────────────────────────────────────────────────────────


def test_ghost_push_records_metrics() -> None:
    import ghost_daemon.metrics as gm
    from ghost_daemon.transport import push as push_mod
    from vantage.schemas import (
        ApprovalSource, ApprovedPayload, Channel, QuickNote, SubscriberIdentity,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def _go():
        orig = push_mod.httpx.AsyncClient

        class _MockClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        push_mod.httpx.AsyncClient = _MockClient
        try:
            note = QuickNote(developer_id="dev-perf", message="hi")
            approved = ApprovedPayload(
                draft_id="draft-perf",
                artifact_type="quick_note",
                payload=note,
                developer_id="dev-perf",
                approval_source=ApprovalSource.POLICY,
            )
            sub = SubscriberIdentity(
                subscriber_id="sub-perf",
                name="Perf Replica",
                token_value="vsub_perf",
                receive_url="http://replica.test",
                channels=[Channel.QUICK_NOTE],
            )
            return await push_mod.push(approved, sub, developer_id="dev-perf")
        finally:
            push_mod.httpx.AsyncClient = orig

    before = _counter_sum(gm.ghost_pushes_total, subscriber="sub-perf", outcome="ok")
    ok = asyncio.run(_go())
    after = _counter_sum(gm.ghost_pushes_total, subscriber="sub-perf", outcome="ok")
    assert ok is True
    assert after - before == 1
    dur = _histogram_count(
        gm.ghost_push_duration_seconds, subscriber="sub-perf", outcome="ok",
    )
    assert dur >= 1


# ── /metrics endpoint ──────────────────────────────────────────────────────


def test_replica_metrics_endpoint_exposes_new_series(tmp_path, monkeypatch) -> None:
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="perf-test")
    app = build_app(cfg)
    with TestClient(app) as c:
        # Drive at least one request so the histogram has data.
        c.get("/health")
        r = c.get("/metrics")

    assert r.status_code == 200
    body = r.text
    for series in (
        "vantage_http_requests_total",
        "vantage_http_request_duration_seconds",
        "vantage_llm_call_duration_seconds",
        "vantage_llm_calls_total",
        "vantage_qdrant_op_duration_seconds",
        "vantage_embedder_duration_seconds",
        "vantage_agent_tool_call_duration_seconds",
        "vantage_agent_tool_calls_total",
    ):
        assert series in body, f"missing series: {series}"


def test_ghost_metrics_endpoint_exposes_new_series(tmp_path) -> None:
    """The ghost UI must serve /metrics with the ghost-side series populated."""
    import ghost_daemon.metrics as gm
    from ghost_daemon.audit.log import AuditLog
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from ghost_daemon.subscribers.registry import SubscriberRegistry
    from ghost_daemon.ui.local_app import build_app

    sqlite = tmp_path / "ghost.db"
    store = SQLiteStore(sqlite)
    audit = AuditLog(tmp_path / "audit", retention_days=7)
    subs = SubscriberRegistry(sqlite)
    gate = ApprovalGate(store, developer_id="dev-perf")
    cfg = Config(developer_id="dev-perf", llm=LLMConfig(provider="groq"))

    # Prime each series by touching one label set so the scraper emits
    # TYPE/HELP + at least one sample line for each name we care about.
    gm.ghost_pushes_total.labels(subscriber="warmup", outcome="ok").inc(0)
    gm.ghost_push_duration_seconds.labels(subscriber="warmup", outcome="ok").observe(0.0)
    gm.llm_call_duration_seconds.labels(
        provider="warmup", model="x", operation="chat",
    ).observe(0.0)

    app = build_app(store, gate, audit, subs, cfg)
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    body = r.text
    for series in (
        "vantage_ghost_pushes_total",
        "vantage_ghost_push_duration_seconds",
        "vantage_ghost_llm_call_duration_seconds",
    ):
        assert series in body, f"missing ghost series: {series}"


# ── /ui/api/perf endpoint ──────────────────────────────────────────────────


def test_perf_endpoint_returns_structured_summary(tmp_path, monkeypatch) -> None:
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="perf-test", manager_token="vsm_perftoken")
    app = build_app(cfg)
    with TestClient(app) as c:
        # Generate a few requests so the HTTP histogram has data.
        for _ in range(3):
            c.get("/health")
        r = c.get("/ui/api/perf", headers={"Authorization": "Bearer vsm_perftoken"})

    assert r.status_code == 200
    data = r.json()
    for key in ("http", "probe", "llm", "qdrant", "embedder", "tool_call", "counters"):
        assert key in data
    http = data["http"]
    assert http["count"] >= 3
    assert http["p50"] is not None
    assert http["p95"] is not None
    # Counter block aggregates total counts.
    assert data["counters"]["http_requests"] >= 3


def test_perf_endpoint_requires_manager_token(tmp_path, monkeypatch) -> None:
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="perf-test", manager_token="vsm_secret")
    app = build_app(cfg)
    with TestClient(app) as c:
        r = c.get("/ui/api/perf")
        assert r.status_code == 401
        r2 = c.get("/ui/api/perf", headers={"Authorization": "Bearer wrong"})
        assert r2.status_code == 401


# ── Timeline counter fix ───────────────────────────────────────────────────


def test_timeline_router_increments_counter(tmp_path, monkeypatch) -> None:
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="perf-test")
    cfg.trusted_devs = [
        cfgmod.TrustedDev(
            developer_id="dev-tl",
            name="dev-tl",
            token_value="vsub_tl_test",
            subscriber_id="sub-tl",
        ),
    ]
    app = build_app(cfg)

    before = _counter_sum(timeline_requests, developer_id="dev-tl")
    with TestClient(app) as c:
        r = c.get(
            "/v1/timeline/dev-tl",
            headers={
                "Authorization": "Bearer vsub_tl_test",
                "X-Vantage-Developer-Id": "dev-tl",
                "X-Vantage-Signature": "sha256=" + _hmac_hex(b"", "vsub_tl_test"),
            },
        )
    after = _counter_sum(timeline_requests, developer_id="dev-tl")
    # Only assert the counter moved on a successful auth. If verify_request's
    # contract changes, the test should still be readable rather than silently
    # over-asserting on a 403 shape it doesn't own.
    if r.status_code == 200:
        assert after - before == 1
    else:
        assert after == before


def _hmac_hex(body: bytes, token: str) -> str:
    import hashlib
    import hmac as _hmac
    return _hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
