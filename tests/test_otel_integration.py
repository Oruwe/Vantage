"""Tests for the optional OpenTelemetry integration.

Design intent: the OTel wiring is strictly opt-in and dep-optional.
When the flag is unset OR the ``opentelemetry-*`` packages aren't
installed, ``maybe_setup_otel`` must return False and not disturb any
other code path (log filter, request-id middleware, push handler).

Tests cover:
  - Default state: OTel inactive, hex helper returns ""
  - Flag unset: ``maybe_setup_otel`` is a no-op
  - Flag set + deps missing: logs a warning, returns False, still no-op
  - Idempotent: second call doesn't re-import or re-instrument
  - Middleware falls back to a minted id when OTel inactive
  - Middleware composes: client-supplied id wins over OTel span
"""
from __future__ import annotations

import logging
import re
import sys

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from shadow_replica.observability import (
    RequestIdMiddleware,
    current_trace_id,
    maybe_setup_otel,
    otel_enabled,
    otel_trace_id_hex,
)
from shadow_replica.observability import otel as otel_mod


@pytest.fixture(autouse=True)
def _reset_otel_state():
    """Ensure each test starts with the ``_otel_active`` flag off."""
    otel_mod.reset_for_tests()
    yield
    otel_mod.reset_for_tests()


# ── Default-off behavior ────────────────────────────────────────────────────


def test_otel_disabled_by_default() -> None:
    assert otel_enabled() is False
    assert otel_trace_id_hex() == ""


def test_maybe_setup_otel_noop_when_flag_unset(monkeypatch) -> None:
    monkeypatch.delenv("VANTAGE_OTEL_ENABLED", raising=False)
    app = FastAPI()
    assert maybe_setup_otel(app) is False
    assert otel_enabled() is False


# ── Flag set but deps missing ──────────────────────────────────────────────


def test_maybe_setup_otel_handles_missing_deps(monkeypatch, caplog) -> None:
    """If the opentelemetry.sdk imports fail (packages not installed), the
    setup must log a warning and return False — not crash app startup."""
    monkeypatch.setenv("VANTAGE_OTEL_ENABLED", "1")

    # Simulate the packages being absent by poisoning sys.modules with
    # None entries so subsequent `from opentelemetry.sdk.trace import …`
    # raises ImportError.
    for name in (
        "opentelemetry.sdk",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.resources",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    ):
        monkeypatch.setitem(sys.modules, name, None)

    with caplog.at_level(logging.WARNING, logger="shadow_replica.observability.otel"):
        ok = maybe_setup_otel(FastAPI())
    assert ok is False
    assert otel_enabled() is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any("opentelemetry deps missing" in m for m in msgs), msgs


def test_maybe_setup_otel_noop_without_env_flag_even_if_deps_present(monkeypatch) -> None:
    """With the flag unset we must not even *try* the imports — this keeps
    cold-start time deterministic for everyone who isn't opting in."""
    monkeypatch.delenv("VANTAGE_OTEL_ENABLED", raising=False)
    called = {"n": 0}

    import builtins
    orig_import = builtins.__import__

    def _spy(name, *a, **kw):
        if name.startswith("opentelemetry"):
            called["n"] += 1
        return orig_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _spy)
    maybe_setup_otel(FastAPI())
    assert called["n"] == 0


# ── Middleware fallback: no OTel ───────────────────────────────────────────


def _app_with_only_our_middleware() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo():
        return JSONResponse({"rid": current_trace_id()})

    return app


def test_middleware_mints_when_no_otel_and_no_header() -> None:
    client = TestClient(_app_with_only_our_middleware())
    r = client.get("/echo")
    rid = r.headers["X-Request-Id"]
    assert re.fullmatch(r"[0-9a-f]{12}", rid)
    assert r.json()["rid"] == rid


def test_middleware_prefers_client_header_over_otel(monkeypatch) -> None:
    """Even if OTel had a trace, a sane client-supplied id wins — it's the
    operator's explicit correlation signal and we respect it."""
    # Pretend OTel has an active trace. We don't need the real SDK — the
    # middleware's _otel_trace_id just calls our hex helper, which we can
    # monkeypatch.
    import shadow_replica.observability.trace as trace_mod
    monkeypatch.setattr(
        trace_mod,
        "_TRACE_ID_MAX_LEN",
        128,  # unchanged; just proving monkeypatch target is loaded
    )
    monkeypatch.setattr(
        "shadow_replica.observability.otel.otel_trace_id_hex",
        lambda: "deadbeefcafe",
    )

    client = TestClient(_app_with_only_our_middleware())
    r = client.get("/echo", headers={"X-Request-Id": "client-wins-123"})
    assert r.headers["X-Request-Id"] == "client-wins-123"
    assert r.json()["rid"] == "client-wins-123"


def test_middleware_uses_otel_trace_when_no_header(monkeypatch) -> None:
    """When no client header AND OTel has a trace, use the OTel id so logs
    share the same 12-char prefix that appears in Jaeger/Tempo."""
    monkeypatch.setattr(
        "shadow_replica.observability.otel.otel_trace_id_hex",
        lambda: "deadbeefcafe",
    )

    client = TestClient(_app_with_only_our_middleware())
    r = client.get("/echo")
    assert r.headers["X-Request-Id"] == "deadbeefcafe"
    assert r.json()["rid"] == "deadbeefcafe"


# ── Idempotency ────────────────────────────────────────────────────────────


def test_maybe_setup_otel_is_idempotent_when_active(monkeypatch) -> None:
    """Simulate a successful first setup via flag flip, then verify second
    call short-circuits without re-attempting imports."""
    # Pretend we succeeded last time.
    otel_mod._otel_active = True
    assert otel_enabled() is True

    import builtins
    orig_import = builtins.__import__
    called = {"n": 0}

    def _spy(name, *a, **kw):
        if name.startswith("opentelemetry"):
            called["n"] += 1
        return orig_import(name, *a, **kw)

    monkeypatch.setenv("VANTAGE_OTEL_ENABLED", "1")
    monkeypatch.setattr(builtins, "__import__", _spy)
    assert maybe_setup_otel(FastAPI()) is True
    assert called["n"] == 0, "idempotent path must not re-import"


# ── build_app smoke: OTel absence doesn't break app startup ────────────────


def test_build_app_starts_cleanly_without_otel(tmp_path, monkeypatch) -> None:
    """Flag unset by default; build_app must succeed and /health return 200."""
    monkeypatch.delenv("VANTAGE_OTEL_ENABLED", raising=False)
    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="test-replica")
    app = build_app(cfg)
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert "X-Request-Id" in r.headers


def test_build_app_tolerates_missing_otel_deps_when_flag_set(
    tmp_path, monkeypatch, caplog
) -> None:
    """Even with the flag on, if deps are absent build_app must still work."""
    monkeypatch.setenv("VANTAGE_OTEL_ENABLED", "1")
    for name in (
        "opentelemetry.sdk",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.resources",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    ):
        monkeypatch.setitem(sys.modules, name, None)

    from shadow_replica import config as cfgmod
    from shadow_replica.app import build_app

    monkeypatch.setattr(cfgmod, "_replica_config_dir", lambda: tmp_path)
    cfg = cfgmod.ReplicaConfig(replica_id="test-replica")
    with caplog.at_level(logging.WARNING, logger="shadow_replica.observability.otel"):
        app = build_app(cfg)
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
