"""Real-code regression tests for push-outbox follow-ups.

Where ``tests/test_push_outbox.py`` exercises ``PushOutbox`` storage in
isolation, these tests drive the actual ``GhostDaemon`` + UI approve route
+ shadow-replica app over ``httpx.ASGITransport`` — the retry path that
would silently break if someone regressed ``GhostDaemon._retry_outbox_row``
or ``GhostDaemon.flush_outbox_for_subscriber`` or the UI enqueue path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_cfg(tmp_path, monkeypatch):
    """Point VANTAGE_HOME at tmp_path and return an LLM-free ``Config``."""
    vantage_dir = tmp_path / ".vantage"
    vantage_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VANTAGE_HOME", str(vantage_dir))
    monkeypatch.setattr("ghost_daemon.config._vantage_dir", lambda: vantage_dir)
    from ghost_daemon.config import load as load_cfg
    cfg = load_cfg()
    cfg.llm.provider = ""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _build_daemon(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, monkeypatch)
    from ghost_daemon.daemon import GhostDaemon
    return GhostDaemon(cfg), cfg


def _add_subscriber(daemon, *, channels):
    return daemon.subscriber_registry.add(
        name="Manager",
        receive_url="http://replica.test",
        channels=channels,
        allow_insecure_http=True,  # loopback-free http is only OK in tests
    )


def _build_replica_app(identity, developer_id, notifications=None):
    from shadow_replica.app import build_app
    from shadow_replica.config import (
        EmbeddingsConfig,
        LLMConfig,
        QdrantConfig,
        ReplicaConfig,
        TrustedDev,
    )

    cfg = ReplicaConfig(
        replica_id="integration-test",
        manager_token="vsm_" + secrets.token_hex(8),
        trusted_devs=[
            TrustedDev(
                developer_id=developer_id,
                name="Manager Dev",
                token_value=identity.token_value,
                subscriber_id=identity.subscriber_id,
            ),
        ],
        notifications=list(notifications or []),
        qdrant=QdrantConfig(enabled=False, url="http://127.0.0.1:1"),
        embeddings=EmbeddingsConfig(
            provider="ollama", model="nomic", endpoint="http://127.0.0.1:1",
        ),
        llm=LLMConfig(provider=""),
    )
    return build_app(cfg), cfg


def _submit_digest_draft(daemon, cfg):
    from vantage.schemas import DraftPayload, WeeklyDigest

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    draft = DraftPayload(
        artifact_type="weekly_digest",
        payload=WeeklyDigest(
            period_start=now,
            period_end=now,
            highlights=["shipped the outbox"],
            commits=["abc123 fixed retry path"],
            open_threads=[],
            blockers=[],
            next_week=["watch the logs"],
            redactions_summary="",
        ),
        developer_id=cfg.developer_id,
    )
    return daemon.gate.submit(draft)


def _submit_quick_note_draft(daemon, cfg):
    from vantage.schemas import DraftPayload, QuickNote

    draft = DraftPayload(
        artifact_type="quick_note",
        payload=QuickNote(message="blocked on auth API", context="feat/auth"),
        developer_id=cfg.developer_id,
    )
    return daemon.gate.submit(draft)


class _ReplicaDown:
    def __call__(self, request):
        raise httpx.ConnectError("replica is down")


def _patch_push_httpx(monkeypatch, client_cls):
    """Swap ``ghost_daemon.transport.push._AsyncClient``, not
    ``push_mod.httpx.AsyncClient`` — the latter would mutate the global
    httpx module and break any ASGITransport or TestClient sharing the
    same process.
    """
    import ghost_daemon.transport.push as push_mod
    monkeypatch.setattr(push_mod, "_AsyncClient", client_cls)


def _asgi_client_cls(replica_app):
    transport = httpx.ASGITransport(app=replica_app)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    return _Client


def _mock_client_cls(handler):
    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    return _Client


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    assert predicate(), "timed out waiting for condition"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 — real end-to-end retry via UI approve + ASGI replica
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_then_flush_dedupes_through_real_retry_code(tmp_path, monkeypatch):
    """Approve-while-down → outbox has one row; flush with replica up →
    timeline has one item with the ORIGINAL approval_id and ``dispatch_all``
    fires exactly once; a second retry of the same body stays deduped.
    """
    from shadow_replica.notify.notifier import NotificationConfig
    from vantage.schemas import Channel

    daemon, cfg = _build_daemon(tmp_path, monkeypatch)
    identity = _add_subscriber(
        daemon, channels=[Channel.WEEKLY_DIGEST, Channel.QUICK_NOTE],
    )

    notifications = [
        NotificationConfig(type="slack_webhook", url="http://slack.test/hook"),
    ]
    replica_app, _rc = _build_replica_app(
        identity, cfg.developer_id, notifications=notifications,
    )

    dispatch_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "shadow_replica.api.receive.dispatch_all", dispatch_mock,
    )

    draft_id = _submit_digest_draft(daemon, cfg)

    async def _run():
        # ── Phase A: replica down ──────────────────────────────────────────
        _patch_push_httpx(monkeypatch, _mock_client_cls(_ReplicaDown()))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=daemon.get_app()),
            base_url="http://testserver",
        ) as ghost_client:
            r = await ghost_client.post(
                f"/draft/{draft_id}/approve",
                data={"edited_json": ""},
            )
            assert r.status_code == 303, r.text

            await _wait_until(lambda: daemon.outbox.count() == 1)

        rows = daemon.outbox.list_all()
        assert len(rows) == 1
        original_approval_id = rows[0].approval_id
        original_body = rows[0].body_json.encode("utf-8")
        assert rows[0].subscriber_id == identity.subscriber_id
        assert rows[0].attempts == 1
        assert dispatch_mock.await_count == 0

        # ── Phase B: replica up — real retry path drains the row ───────────
        _patch_push_httpx(monkeypatch, _asgi_client_cls(replica_app))

        await daemon.flush_outbox_for_subscriber(identity.subscriber_id)
        assert daemon.outbox.count() == 0

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replica_app),
            base_url="http://testserver",
        ) as replica_client:
            tl = await replica_client.get(
                f"/v1/timeline/{cfg.developer_id}",
                headers={"Authorization": f"Bearer {identity.token_value}"},
            )
            assert tl.status_code == 200, tl.text
            body = tl.json()
            assert body["total"] == 1
            assert body["items"][0]["approval_id"] == original_approval_id
            assert dispatch_mock.await_count == 1

            # ── Phase C: a second retry of the same body is a no-op ───────
            daemon.outbox.enqueue(
                approval_id=original_approval_id,
                subscriber_id=identity.subscriber_id,
                body=original_body,
                last_error="simulated second drop",
            )
            await daemon.flush_outbox_for_subscriber(identity.subscriber_id)
            assert daemon.outbox.count() == 0

            tl2 = await replica_client.get(
                f"/v1/timeline/{cfg.developer_id}",
                headers={"Authorization": f"Bearer {identity.token_value}"},
            )
            assert tl2.status_code == 200
            assert tl2.json()["total"] == 1

        # Replica dedupes BEFORE dispatch_all — still 1 call total.
        assert dispatch_mock.await_count == 1

    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 — quick note approved in the UI reaches a quick_note-only subscriber
# ─────────────────────────────────────────────────────────────────────────────

def test_quick_note_approved_via_ui_pushes_to_quick_note_only_subscriber(
    tmp_path, monkeypatch,
):
    """Without the channel_map entry, quick notes silently route to the
    WEEKLY_DIGEST default and this subscriber never sees them.
    """
    from vantage.schemas import Channel

    daemon, cfg = _build_daemon(tmp_path, monkeypatch)
    identity = _add_subscriber(daemon, channels=[Channel.QUICK_NOTE])

    seen: list[dict] = []

    def _handler(request):
        parsed = json.loads(request.content)
        seen.append({
            "path": request.url.path,
            "body": parsed,
            "auth": request.headers.get("authorization", ""),
        })
        return httpx.Response(
            201, json={"status": "ok", "approval_id": parsed["approval_id"]},
        )

    _patch_push_httpx(monkeypatch, _mock_client_cls(_handler))

    draft_id = _submit_quick_note_draft(daemon, cfg)

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=daemon.get_app()),
            base_url="http://testserver",
        ) as ghost_client:
            r = await ghost_client.post(
                f"/draft/{draft_id}/approve", data={"edited_json": ""},
            )
            assert r.status_code == 303
            await _wait_until(lambda: len(seen) >= 1, timeout=2.0)

    asyncio.run(_run())
    assert identity.subscriber_id  # tie the identity fixture into the assertion space
    assert len(seen) == 1
    assert seen[0]["path"] == "/v1/receive"
    assert seen[0]["body"]["artifact_type"] == "quick_note"
    assert seen[0]["auth"].startswith("Bearer ")


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3 — retry exceptions log at WARNING with the row id
# ─────────────────────────────────────────────────────────────────────────────

def test_outbox_retry_logs_exception_at_warning_with_row_id(
    tmp_path, monkeypatch, caplog,
):
    """A row whose push blows up must surface at WARNING, with the row id."""
    from vantage.schemas import Channel

    daemon, _cfg = _build_daemon(tmp_path, monkeypatch)
    identity = _add_subscriber(daemon, channels=[Channel.WEEKLY_DIGEST])

    row_id = daemon.outbox.enqueue(
        approval_id="approv-boom",
        subscriber_id=identity.subscriber_id,
        body=(
            b'{"approval_id":"approv-boom","draft_id":"d",'
            b'"artifact_type":"weekly_digest","payload":{},'
            b'"approved_at":"2026-01-01T00:00:00","approval_source":"manual",'
            b'"developer_id":"dev-1"}'
        ),
        last_error="initial fail",
    )

    # Force the row to be due regardless of its 30s backoff clock.
    rows = daemon.outbox.list_all()
    monkeypatch.setattr(daemon.outbox, "list_due", lambda **_kw: rows)

    import ghost_daemon.daemon as daemon_mod

    async def _boom(self, row):
        raise RuntimeError(f"boom for row {row.id}")

    monkeypatch.setattr(daemon_mod.GhostDaemon, "_retry_outbox_row", _boom)

    caplog.set_level(logging.WARNING, logger="ghost_daemon.daemon")
    asyncio.run(daemon._drain_outbox_once())

    warn_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "ghost_daemon.daemon"
    ]
    assert any(row_id in r.getMessage() for r in warn_records), (
        f"expected WARNING with row id {row_id!r}, got "
        f"{[r.getMessage() for r in warn_records]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fix 4 — _on_connect flush task has a strong reference
# ─────────────────────────────────────────────────────────────────────────────

def test_on_connect_task_reference_is_tracked_until_done(tmp_path, monkeypatch):
    """asyncio.create_task(...) only gets a *weak* loop ref; the daemon must
    keep a strong one until the task finishes, otherwise a mid-execution GC
    can drop the reconnect flush and the outbox never drains.
    """
    daemon, _cfg = _build_daemon(tmp_path, monkeypatch)
    assert hasattr(daemon, "_on_connect_tasks"), "daemon must track flush tasks"
    assert daemon._on_connect_tasks == set()

    gate = asyncio.Event()
    calls = {"count": 0}

    async def _blocked_flush(sid):
        calls["count"] += 1
        await gate.wait()

    daemon.flush_outbox_for_subscriber = _blocked_flush  # type: ignore[assignment]

    async def _run():
        on_connect = daemon._make_on_connect("sub_target")
        on_connect()

        # Yield once so the created task actually starts.
        await asyncio.sleep(0)
        assert len(daemon._on_connect_tasks) == 1
        assert calls["count"] == 1

        gate.set()
        # Two ticks: one for _blocked_flush to resume, one for the
        # done-callback to discard the task from the set.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert daemon._on_connect_tasks == set()

    asyncio.run(_run())
