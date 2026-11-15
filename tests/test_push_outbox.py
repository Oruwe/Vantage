"""Regression tests for the push outbox retry system.

The pilot runs with the manager's replica on a laptop that may be offline when
a developer approves something. These tests pin the three promised behaviours:

  - a push that fails while the replica is down is retried later and the
    replica ends up with exactly ONE copy of the artifact, and dispatch_all
    (the notification fan-out) runs ONCE — not once per retry;
  - a retry carries the SAME approval_id as the original push (so the replica's
    dedupe survives even when the retry happens to land before the first push
    does);
  - a row is removed on success and kept with attempts+1 on failure.

These tests must fail until the outbox is wired in.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

import httpx


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_approved():
    """Build a minimal ApprovedPayload suitable for push()."""
    from vantage.schemas import ApprovalSource, ApprovedPayload, QuickNote

    return ApprovedPayload(
        draft_id="draft-1",
        artifact_type="quick_note",
        payload=QuickNote(developer_id="dev-1", message="hi"),
        developer_id="dev-1",
        approval_source=ApprovalSource.POLICY,
    )


def _make_subscriber(name: str = "Replica"):
    from vantage.schemas import Channel, SubscriberIdentity

    return SubscriberIdentity(
        subscriber_id="sub-1",
        name=name,
        token_value="vsub_" + secrets.token_hex(12),
        receive_url="http://replica.test",
        channels=[Channel.QUICK_NOTE],
    )


class _ReplicaDown:
    """httpx MockTransport handler that simulates a dead replica."""
    def __call__(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("replica is down")


class _ReplicaUp:
    """Replica that accepts pushes and dedupes by approval_id."""
    def __init__(self):
        self.seen_approval_ids: list[str] = []
        self.dispatch_calls: int = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        approval_id = body["approval_id"]
        if approval_id in self.seen_approval_ids:
            return httpx.Response(200, json={"status": "duplicate", "approval_id": approval_id})
        self.seen_approval_ids.append(approval_id)
        self.dispatch_calls += 1
        return httpx.Response(201, json={"status": "ok", "approval_id": approval_id})


def _mock_httpx(push_mod, handler) -> None:
    """Patch push_mod._AsyncClient so push_body() hits a MockTransport.

    The AsyncClient used by ``push_body`` is bound at module scope as
    ``_AsyncClient`` precisely so tests can swap it without mutating the
    global ``httpx.AsyncClient`` — which would also affect any ASGI
    TestClient / ASGITransport running in the same loop.
    """
    transport = httpx.MockTransport(handler)
    orig = push_mod._AsyncClient

    class _MockClient(orig):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    push_mod._AsyncClient = _MockClient
    return orig


# ─────────────────────────────────────────────────────────────────────────────
# PushOutbox storage: a row is removed on success, kept with attempts+1 on fail
# ─────────────────────────────────────────────────────────────────────────────

def test_outbox_row_removed_after_success_kept_after_failure(tmp_path: Path) -> None:
    """A successful retry deletes the row; a failed retry increments attempts."""
    from ghost_daemon.storage.outbox import PushOutbox

    outbox = PushOutbox(tmp_path / "vantage.db")

    approved = _make_approved()
    sub = _make_subscriber()
    body = approved.model_dump_json().encode("utf-8")
    outbox.enqueue(
        approval_id=approved.approval_id,
        subscriber_id=sub.subscriber_id,
        body=body,
        last_error="connect error",
    )

    # One pending row. Use list_all here — list_due respects backoff and
    # wouldn't return a row whose next_try_at is still 30s away.
    all_rows = outbox.list_all()
    assert len(all_rows) == 1
    row = all_rows[0]
    assert row.approval_id == approved.approval_id
    assert row.subscriber_id == sub.subscriber_id
    assert row.attempts == 1  # one failed attempt already
    assert row.body_json == body.decode("utf-8")

    # Simulate a failed retry — row stays, attempts+1, next_try_at pushed.
    outbox.mark_failed(row.id, error="still down")
    due_after_fail = outbox.list_all()
    assert len(due_after_fail) == 1
    assert due_after_fail[0].attempts == 2
    assert due_after_fail[0].last_error == "still down"

    # Successful retry — row is deleted.
    outbox.mark_sent(row.id)
    assert outbox.list_all() == []
    assert outbox.count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Enqueue-and-retry: the end-to-end promise
# ─────────────────────────────────────────────────────────────────────────────

def test_replica_down_then_up_retry_yields_exactly_one_artifact(
    tmp_path: Path, monkeypatch,
) -> None:
    """Replica down → approve → replica up → retry runs → ONE artifact, ONE dispatch."""
    from ghost_daemon.storage.outbox import PushOutbox
    from ghost_daemon.transport import push as push_mod
    from ghost_daemon.transport.push import push_body

    outbox = PushOutbox(tmp_path / "vantage.db")
    approved = _make_approved()
    sub = _make_subscriber()
    body = approved.model_dump_json().encode("utf-8")
    original_approval_id = approved.approval_id

    # ── 1. Replica is down. First push fails and enqueues. ────────────────────
    down = _ReplicaDown()
    orig = _mock_httpx(push_mod, down)
    try:
        ok = asyncio.run(push_body(body, sub, developer_id="dev-1"))
        assert ok is False
        outbox.enqueue(
            approval_id=original_approval_id,
            subscriber_id=sub.subscriber_id,
            body=body,
            last_error="replica down",
        )
    finally:
        push_mod._AsyncClient = orig

    # ── 2. Replica comes up. The WebSocket reconnect path flushes the queue
    #      for this subscriber immediately, bypassing backoff. ───────────────
    up = _ReplicaUp()
    orig = _mock_httpx(push_mod, up)
    try:
        for row in outbox.list_due_for_subscriber(sub.subscriber_id):
            sent = asyncio.run(push_body(row.body_json.encode("utf-8"), sub, developer_id="dev-1"))
            assert sent is True
            outbox.mark_sent(row.id)
    finally:
        push_mod._AsyncClient = orig

    # ── 3. Promise: exactly one copy on the replica, dispatch_all once. ──────
    assert up.seen_approval_ids == [original_approval_id]
    assert up.dispatch_calls == 1
    assert outbox.count() == 0


def test_retried_push_carries_same_approval_id(tmp_path: Path) -> None:
    """The retry MUST reuse the original body (same approval_id), not rebuild it.

    This is the dedupe hinge in shadow_replica/api/receive.py: the replica
    returns 200 "duplicate" only when the approval_id matches a stored one.
    Rebuilding an ApprovedPayload picks up a fresh UUID via default_factory,
    which would defeat dedupe.
    """
    from ghost_daemon.storage.outbox import PushOutbox

    outbox = PushOutbox(tmp_path / "vantage.db")
    approved = _make_approved()
    sub = _make_subscriber()
    original_body = approved.model_dump_json().encode("utf-8")
    original_approval_id = approved.approval_id

    outbox.enqueue(
        approval_id=original_approval_id,
        subscriber_id=sub.subscriber_id,
        body=original_body,
        last_error="fail",
    )

    rows = outbox.list_all()
    assert len(rows) == 1
    retried_body = rows[0].body_json.encode("utf-8")

    # Byte-for-byte identical — HMAC signature over the retried body will match
    # whatever the first push would have sent.
    assert retried_body == original_body

    # The approval_id parsed back out of the retried body is the ORIGINAL.
    retried_approval_id = json.loads(retried_body)["approval_id"]
    assert retried_approval_id == original_approval_id
