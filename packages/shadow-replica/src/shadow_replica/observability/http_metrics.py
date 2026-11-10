"""HTTP request-path Prometheus instrumentation.

``MetricsMiddleware`` wraps every FastAPI request and updates two series:

  - ``vantage_http_requests_total{route,method,status}``
  - ``vantage_http_request_duration_seconds{route,method}``

The ``route`` label is the Starlette route path *template* (e.g.
``/v1/timeline/{developer_id}``), extracted from
``request.scope["route"].path``. Falling back to the raw URL would blow
label cardinality apart as soon as a single endpoint sees arbitrary user
ids in the path.

Routes that don't match any registered endpoint are labeled ``"_unknown"``
so a flood of scanner probes shows up as one series, not thousands.
"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from shadow_replica.metrics import (
    http_request_duration_seconds,
    http_requests_total,
)


def _route_template(request: Request) -> str:
    """Return the matched route's path template, or ``"_unknown"``."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) if route is not None else None
    if isinstance(path, str) and path:
        return path
    # Pre-routing or no match. Prefer the actual path on health/metrics, but
    # bucket everything else to keep cardinality bounded.
    url_path = request.url.path
    if url_path in ("/", "/health", "/metrics", "/docs", "/openapi.json"):
        return url_path
    return "_unknown"


def _status_bucket(code: int) -> str:
    """Collapse status codes into class strings so a stray 404 doesn't create
    a per-code label series. Mirrors the common nginx/prom pattern."""
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record per-request latency and status counts."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        t0 = time.monotonic()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            # Let the exception propagate — FastAPI's error-handling will turn
            # it into a 500 — but still record the metric so operators can see
            # error-path latency distribution instead of silence.
            raise
        finally:
            elapsed = time.monotonic() - t0
            route = _route_template(request)
            method = request.method
            http_request_duration_seconds.labels(route=route, method=method).observe(elapsed)
            http_requests_total.labels(
                route=route, method=method, status=_status_bucket(status_code),
            ).inc()
