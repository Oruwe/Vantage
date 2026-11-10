"""Ghost-daemon observability primitives.

Mirrors the shadow-replica's ``trace`` API so operators can grep the same
``trace=...`` identifier across both sides of the ghost→replica path.
Ghost does not expose a Starlette middleware here — its UI runs on 127.0.0.1
and its outbound events are the actual thing we want to correlate.
"""
from __future__ import annotations

from .trace import (
    RequestIdLogFilter,
    current_trace_id,
    new_trace_id,
    use_trace_id,
)

__all__ = [
    "RequestIdLogFilter",
    "current_trace_id",
    "new_trace_id",
    "use_trace_id",
]
