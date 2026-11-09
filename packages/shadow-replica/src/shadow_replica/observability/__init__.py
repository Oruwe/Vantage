"""Observability helpers for the shadow-replica.

Exports a trace-id surface:
  - ``current_trace_id()`` reads the id attached to the current task/thread
  - ``RequestIdMiddleware`` tags every FastAPI request with one (HTTP header
    stays ``X-Request-Id`` — standard) and echoes it in the response
  - ``RequestIdLogFilter`` injects it into every ``logging`` record under
    ``trace_id`` so ``%(trace_id)s`` in a formatter works out of the box
  - ``use_trace_id(tid)`` scopes a trace-id for non-HTTP code paths

Note the naming: the probe/WebSocket protocol separately uses ``request_id``
as a wire-level identifier. The two live on different layers and are kept
semantically distinct — grep ``trace_id`` for HTTP, ``request_id`` for probes.
"""
from __future__ import annotations

from shadow_replica.observability.trace import (
    RequestIdLogFilter,
    RequestIdMiddleware,
    current_trace_id,
    new_trace_id,
    use_trace_id,
)

__all__ = [
    "RequestIdLogFilter",
    "RequestIdMiddleware",
    "current_trace_id",
    "new_trace_id",
    "use_trace_id",
]
