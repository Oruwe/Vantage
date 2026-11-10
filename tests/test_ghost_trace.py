"""Tests for ghost-daemon trace-id propagation.

Covers:
  - Ghost observability module exports the mirrored trace API
  - RequestIdLogFilter stamps records with ``trace_id`` attribute
  - use_trace_id sanitizes malformed ids (log-injection defense)
  - Nested use_trace_id scopes restore the previous value
  - setup_logging installs the filter on the root logger (idempotent)
  - Ghost push inherits the outer trace_id when one is active
  - Ghost push mints a fresh trace_id when no outer context exists
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

from ghost_daemon.observability import (
    RequestIdLogFilter,
    current_trace_id,
    new_trace_id,
    use_trace_id,
)


# ── Primitives ──────────────────────────────────────────────────────────────


def test_default_trace_id_is_sentinel() -> None:
    assert current_trace_id() == "-"


def test_new_trace_id_is_hex_and_unique() -> None:
    a = new_trace_id()
    b = new_trace_id()
    assert a != b
    assert re.fullmatch(r"[0-9a-f]{12}", a)
    assert re.fullmatch(r"[0-9a-f]{12}", b)


def test_use_trace_id_nests_correctly() -> None:
    assert current_trace_id() == "-"
    with use_trace_id("outer-trace"):
        assert current_trace_id() == "outer-trace"
        with use_trace_id("inner-trace"):
            assert current_trace_id() == "inner-trace"
        assert current_trace_id() == "outer-trace"
    assert current_trace_id() == "-"


def test_use_trace_id_sanitizes_malformed_value() -> None:
    """Pattern the replica uses: refuse anything with unsafe chars + replace.
    Prevents a caller from smuggling a '; DROP TABLE' into a log line."""
    bad = "has spaces and ;drop"
    with use_trace_id(bad) as got:
        assert got != bad
        assert re.fullmatch(r"[0-9a-f]{12}", got)
        assert current_trace_id() == got


def test_use_trace_id_rejects_empty_and_overlong() -> None:
    with use_trace_id("") as a:
        assert re.fullmatch(r"[0-9a-f]{12}", a)
    with use_trace_id("x" * 300) as b:
        assert re.fullmatch(r"[0-9a-f]{12}", b)


def test_log_filter_stamps_records_inside_context() -> None:
    logger = logging.getLogger("vantage.test.ghost_trace_filter")
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
        with use_trace_id("abc123def456"):
            logger.info("inside ghost context")
        logger.info("outside ghost context")
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 2
    assert getattr(captured[0], "trace_id", "") == "abc123def456"
    assert getattr(captured[1], "trace_id", "") == "-"


# ── setup_logging wiring ────────────────────────────────────────────────────


def test_setup_logging_installs_filter_once(tmp_path: Path) -> None:
    """Idempotent: calling twice must not stack multiple filters."""
    from ghost_daemon.logging_setup import setup_logging

    root = logging.getLogger()
    # Snapshot and remove any pre-existing filters so the test is deterministic.
    original_filters = list(root.filters)
    for f in list(root.filters):
        root.removeFilter(f)
    original_handlers = list(root.handlers)
    for h in list(root.handlers):
        root.removeHandler(h)

    try:
        setup_logging(tmp_path / "logs1", level="INFO", daemon_mode=True)
        setup_logging(tmp_path / "logs2", level="INFO", daemon_mode=True)
        filters = [f for f in root.filters if isinstance(f, RequestIdLogFilter)]
        assert len(filters) == 1, "filter should be installed exactly once"
    finally:
        # Restore the pre-test state (and clean up any handlers we added).
        for h in list(root.handlers):
            if h not in original_handlers:
                try:
                    h.close()
                except Exception:
                    pass
                root.removeHandler(h)
        for f in list(root.filters):
            root.removeFilter(f)
        for f in original_filters:
            root.addFilter(f)


def test_setup_logging_formatter_uses_trace_placeholder(tmp_path: Path) -> None:
    """Confirm the format string references %(trace_id)s so logs render it."""
    from ghost_daemon.logging_setup import setup_logging

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_filters = list(root.filters)
    for h in list(root.handlers):
        root.removeHandler(h)
    for f in list(root.filters):
        root.removeFilter(f)
    try:
        setup_logging(tmp_path / "logs", level="INFO", daemon_mode=True)
        # The file handler installed by setup_logging should carry our fmt.
        formatter_found = False
        for h in root.handlers:
            if h.formatter and "%(trace_id)s" in h.formatter._fmt:  # type: ignore[attr-defined]
                formatter_found = True
                break
        assert formatter_found, "format string must include %(trace_id)s"
    finally:
        for h in list(root.handlers):
            if h not in original_handlers:
                try:
                    h.close()
                except Exception:
                    pass
                root.removeHandler(h)
        for f in list(root.filters):
            root.removeFilter(f)
        for f in original_filters:
            root.addFilter(f)


# ── Push integration: trace inheritance ────────────────────────────────────


def _run_push_capture(outer_trace_id: str | None) -> dict:
    """Invoke push() under an optional outer trace scope; return captured headers."""
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
            if outer_trace_id is not None:
                with use_trace_id(outer_trace_id):
                    return await push_mod.push(approved, sub, developer_id="dev-1")
            return await push_mod.push(approved, sub, developer_id="dev-1")
        finally:
            push_mod.httpx.AsyncClient = orig

    ok = asyncio.run(_go())
    assert ok is True
    return captured


def test_push_inherits_outer_trace_id() -> None:
    """When push() is called under use_trace_id, that id goes on the wire."""
    captured = _run_push_capture("inheritedtrace")
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers.get("x-request-id") == "inheritedtrace"


def test_push_mints_fresh_trace_id_without_context() -> None:
    """With no outer context, push mints a 12-hex id — matches the prior contract."""
    captured = _run_push_capture(None)
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert re.fullmatch(r"[0-9a-f]{12}", headers.get("x-request-id", ""))


def test_push_logs_carry_trace_id_attribute() -> None:
    """RequestIdLogFilter must stamp push log records with the pushed trace_id."""
    from ghost_daemon.transport import push as push_mod
    from vantage.schemas import (
        ApprovalSource, ApprovedPayload, Channel, QuickNote, SubscriberIdentity,
    )

    captured_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    push_logger = logging.getLogger("ghost_daemon.transport.push")
    handler = _Capture(level=logging.DEBUG)
    handler.addFilter(RequestIdLogFilter())
    push_logger.addHandler(handler)
    prev_level = push_logger.level
    push_logger.setLevel(logging.DEBUG)

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(ok_handler)

    async def _go():
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
            with use_trace_id("logtest12345"):
                await push_mod.push(approved, sub, developer_id="dev-1")
        finally:
            push_mod.httpx.AsyncClient = orig

    try:
        asyncio.run(_go())
    finally:
        push_logger.removeHandler(handler)
        push_logger.setLevel(prev_level)

    # The success log line must have been stamped with our outer trace id.
    assert captured_records, "push should have logged at least one record"
    success = [r for r in captured_records if "Pushed" in r.getMessage()]
    assert success, "expected a success log line"
    assert getattr(success[0], "trace_id", "") == "logtest12345"
