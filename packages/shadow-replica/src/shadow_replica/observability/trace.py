"""Trace-id propagation for the shadow-replica.

Named ``trace_id`` deliberately — the probe/WebSocket protocol already uses
``request_id`` as a wire-level identifier (see ``vantage.schemas.ProbeRequest``).
Two different ids on two different layers; kept distinct so grep stays honest.

A ``ContextVar`` holds the trace-id for the duration of each request (asyncio
task scope). ``RequestIdMiddleware`` is a Starlette/FastAPI middleware that
reads/mints the id and writes it back on the response. ``RequestIdLogFilter``
injects it into every log record under the ``trace_id`` attribute so loggers
can print ``%(trace_id)s`` without the caller having to pass it through.

The HTTP header itself stays ``X-Request-Id`` — that's the cross-industry
convention operators already grep for.
"""
from __future__ import annotations

import logging
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID_HEADER = "X-Request-Id"
_TRACE_ID_MAX_LEN = 128  # defensive cap on client-supplied values

# "-" is a cheap sentinel meaning "no request context"; log formatters
# can match it to decide whether to show the field.
_trace_id_var: ContextVar[str] = ContextVar("vantage_trace_id", default="-")


def current_trace_id() -> str:
    """Return the trace-id attached to the current task, or ``"-"`` if none."""
    return _trace_id_var.get()


def new_trace_id() -> str:
    """Mint a fresh short id. 12 hex chars = 48 bits of entropy."""
    return secrets.token_hex(6)


@contextmanager
def use_trace_id(trace_id: str) -> Iterator[str]:
    """Attach ``trace_id`` to the current context for the duration of the
    ``with`` block. Used by non-HTTP entry points (e.g. background tasks)
    that want their log lines correlated with an upstream trigger."""
    token = _trace_id_var.set(trace_id)
    try:
        yield trace_id
    finally:
        _trace_id_var.reset(token)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach ``X-Request-Id`` to every request + response.

    Behavior:
      - If the incoming request carries a sane ``X-Request-Id``, reuse it.
      - Otherwise mint a fresh one.
      - Bind it to the request-scoped trace-id ``ContextVar`` for the
        duration of the handler, and echo it on the response.

    "Sane" means: non-empty, ≤ 128 chars, only safe ASCII (letters, digits,
    ``-``, ``_``). Reject anything else and mint a replacement — we don't
    want unsanitized log injection via a client header.
    """

    _SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(_REQUEST_ID_HEADER, "").strip()
        tid = incoming if self._is_sane(incoming) else new_trace_id()

        token = _trace_id_var.set(tid)
        try:
            response: Response = await call_next(request)
        finally:
            _trace_id_var.reset(token)

        # Always echo the id the server used; this is what the client should
        # cite when reporting bugs, even if its own header was ignored.
        response.headers[_REQUEST_ID_HEADER] = tid
        return response

    @classmethod
    def _is_sane(cls, value: str) -> bool:
        if not value:
            return False
        if len(value) > _TRACE_ID_MAX_LEN:
            return False
        return all(c in cls._SAFE_CHARS for c in value)


class RequestIdLogFilter(logging.Filter):
    """Add the current trace-id to every log record under ``trace_id``.

    Install once on the root logger (or on individual handlers). Formatters
    can then reference ``%(trace_id)s``; records produced outside any
    request get ``"-"``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id()
        return True
