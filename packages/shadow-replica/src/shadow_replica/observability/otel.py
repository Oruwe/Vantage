"""Optional OpenTelemetry integration.

Deliberately lazy: the ``opentelemetry-*`` packages are an optional extra.
Calling ``maybe_setup_otel(app)`` is a no-op unless **both** conditions hold:

  1. ``VANTAGE_OTEL_ENABLED=1`` is set in the environment.
  2. The OTel API/SDK + the FastAPI/HTTPX instrumentation libraries are
     installed (``pip install shadow-replica[otel]``).

When active, the module:

  - Configures a global ``TracerProvider`` with an OTLP HTTP span exporter
    (endpoint from ``OTEL_EXPORTER_OTLP_ENDPOINT`` or the OTel SDK default).
  - Instruments the FastAPI app so every HTTP request gets a span.
  - Instruments the httpx client so outbound pushes get child spans.
  - Returns a sentinel ``True`` that callers can log.

The ``RequestIdMiddleware`` separately consults ``otel_trace_id_hex()`` so
log lines and OTel spans share the same correlation id — operators can
pivot between Jaeger/Tempo and plain log grep without a translation table.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_ENV_FLAG = "VANTAGE_OTEL_ENABLED"
_SERVICE_NAME_DEFAULT = "vantage-shadow-replica"

_otel_active: bool = False


def otel_enabled() -> bool:
    """Return True once ``maybe_setup_otel`` has successfully configured OTel."""
    return _otel_active


def otel_trace_id_hex() -> str:
    """Return the current OTel span's trace-id (first 12 hex chars) or ``""``.

    We truncate to 12 hex chars to match Vantage's correlation-id format so
    a single value appears in logs AND as the leading prefix of the full
    32-char OTel trace-id in the exporter UI. Full id is still available on
    the span itself for anyone hitting Jaeger directly.
    """
    if not _otel_active:
        return ""
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except Exception:
        return ""
    span = trace.get_current_span()
    if span is None:
        return ""
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return ""
    # OTel stores trace_id as a 128-bit int.
    return format(ctx.trace_id, "032x")[:12]


def maybe_setup_otel(app: "FastAPI | None" = None, *, service_name: str | None = None) -> bool:
    """Install OTel wiring if the env flag is set and deps are available.

    Safe to call multiple times — subsequent calls short-circuit once
    activation has succeeded. Returns True iff OTel is now active.
    """
    global _otel_active
    if _otel_active:
        return True

    if os.environ.get(_ENV_FLAG, "").strip() != "1":
        return False

    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )
    except Exception as exc:
        logger.warning(
            "VANTAGE_OTEL_ENABLED=1 but opentelemetry deps missing (%s). "
            "Install with `pip install shadow-replica[otel]`.",
            exc,
        )
        return False

    svc = service_name or os.environ.get("OTEL_SERVICE_NAME", _SERVICE_NAME_DEFAULT)
    resource = Resource.create({"service.name": svc})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # FastAPI instrumentation is optional — the user may only want outbound
    # httpx spans. Guard each import separately so one missing package
    # doesn't block the other.
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import (  # type: ignore[import-not-found]
                FastAPIInstrumentor,
            )
            FastAPIInstrumentor.instrument_app(app)
        except Exception as exc:
            logger.warning("OTel FastAPI instrumentation unavailable: %s", exc)

    try:
        from opentelemetry.instrumentation.httpx import (  # type: ignore[import-not-found]
            HTTPXClientInstrumentor,
        )
        HTTPXClientInstrumentor().instrument()
    except Exception as exc:
        logger.warning("OTel httpx instrumentation unavailable: %s", exc)

    _otel_active = True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "(default)")
    logger.info("OTel enabled — service=%s exporter=%s", svc, endpoint)
    return True


def reset_for_tests() -> None:
    """Flip the active flag back off. Tests only — not thread-safe."""
    global _otel_active
    _otel_active = False
