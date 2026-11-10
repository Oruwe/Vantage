"""Trace-id propagation for the ghost-daemon.

Local ContextVar that any ghost code path (push, probe handler, scheduler)
can enter via ``use_trace_id(...)`` to tag the enclosed log records with a
``trace_id`` attribute. Format strings reference it via ``%(trace_id)s``;
records produced outside any trace scope get ``"-"``.

The identifier format matches the replica (12 hex chars) so a single grep
across both log streams correlates push → receive without translation.
"""
from __future__ import annotations

import logging
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_TRACE_ID_MAX_LEN = 128

_trace_id_var: ContextVar[str] = ContextVar("vantage_ghost_trace_id", default="-")

_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def current_trace_id() -> str:
    """Return the trace-id attached to the current task, or ``"-"`` if none."""
    return _trace_id_var.get()


def new_trace_id() -> str:
    """Mint a fresh short id. 12 hex chars = 48 bits of entropy."""
    return secrets.token_hex(6)


def _is_sane(value: str) -> bool:
    if not value:
        return False
    if len(value) > _TRACE_ID_MAX_LEN:
        return False
    return all(c in _SAFE_CHARS for c in value)


@contextmanager
def use_trace_id(trace_id: str) -> Iterator[str]:
    """Attach ``trace_id`` (sanitized) to the current context.

    Non-sane values are replaced with a freshly minted id to prevent
    unsanitized upstream data from reaching log lines.
    """
    tid = trace_id if _is_sane(trace_id) else new_trace_id()
    token = _trace_id_var.set(tid)
    try:
        yield tid
    finally:
        _trace_id_var.reset(token)


class RequestIdLogFilter(logging.Filter):
    """Stamp every log record with the current ghost trace-id."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id()
        return True
