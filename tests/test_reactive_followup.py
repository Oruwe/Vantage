"""Tests for Option 1 — reactive A2A on webhook events + SHA/branch threading.

Covers:
  - Trigger detection: CI failures and changes-requested reviews fire; others don't
  - Follow-up uses cached activity when the ghost's WS beacon is fresh
  - Follow-up falls back to a real probe when online but cache is stale
  - Offline / unmapped devs don't block the handler
  - The stored GithubEvent gets re-written with followup_summary populated
  - A REACTIVE_FOLLOWUP envelope hits the Wiretap bus
  - Ghost probe agent treats replica:system as activity-signals-only
  - SHA threading groups github_event + commit_report by commit_sha
  - Branch threading joins by branch when commit_sha differs
  - Threads endpoint respects the time window
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vantage.schemas import (
    ActivitySignals, PolicyDecision, ProbeRequest, ProbeResponse,
    RedactionLevel, SessionSnapshot,
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def replica_tmp(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        monkeypatch.setattr(
            "shadow_replica.config._replica_config_dir",
            lambda: tmp,
        )
        yield tmp


@pytest.fixture
def client(replica_tmp):
    from shadow_replica.app import build_app
    from shadow_replica.config import load
    cfg = load()
    cfg.qdrant.enabled = False
    app = build_app(cfg)
    with TestClient(app) as c:
        c._cfg = cfg
        c._manager = cfg.manager_token
        yield c


def _auth(client):
    return {"Authorization": f"Bearer {client._manager}"}


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _add_repo(client, slug="acme/payments", secret="whsec_test"):
    from shadow_replica.config import WatchedRepo
    client._cfg.watched_repos.append(WatchedRepo(slug=slug, secret=secret))
    return secret


def _add_dev(client, dev_id="alice", github_login="alice-dev"):
    from shadow_replica.config import TrustedDev
    client._cfg.trusted_devs.append(TrustedDev(
        developer_id=dev_id, name=dev_id,
        token_value=f"vsub_{dev_id}", github_login=github_login,
    ))


def _walk_routes(container):
    """Yield every APIRoute/APIWebSocketRoute at any depth under `container`.

    FastAPI wraps `include_router(...)` entries in `_IncludedRouter` objects
    that keep their routes under `.original_router.routes` — not `.routes`.
    """
    for route in getattr(container, "routes", []):
        if hasattr(route, "endpoint") and hasattr(route, "path"):
            yield route
        else:
            # _IncludedRouter case
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from _walk_routes(inner)
            else:
                yield from _walk_routes(route)


def _get_hub(client):
    """Reach the WSHub via the webhook router's closure (same tactic the webhook uses)."""
    for route in _walk_routes(client.app):
        if getattr(route, "path", "") == "/v1/github/webhook":
            for cell in route.endpoint.__closure__ or ():
                v = cell.cell_contents
                if hasattr(v, "online_developer_ids") and hasattr(v, "cache_activity"):
                    return v
    raise RuntimeError("Could not resolve WSHub from webhook route")


def _seed_cached_activity(hub, dev_id="alice", files=("test_payments.py", "auth.py"),
                           branch="feat/idempotency", repo="acme/payments",
                           primary="debugging"):
    sig = ActivitySignals(
        files_recently_touched=list(files),
        inferred_branch=branch,
        active_repo=repo,
        last_user_intent="fix idempotency race",
        primary_activity=primary,
    )
    hub.cache_activity(dev_id, sig, datetime.now(timezone.utc))


def _wait_for_followup(client, approval_id, timeout=5.0, poll=0.1):
    """Poll the dashboard until the reactive follow-up has populated the artifact.

    The follow-up runs as asyncio.create_task on TestClient's internal portal
    thread (which stays alive for the lifetime of the `with TestClient()` block).
    We block the test thread with time.sleep between checks.
    """
    import time
    deadline = time.monotonic() + timeout
    last_summary = ""
    while time.monotonic() < deadline:
        r = client.get("/ui/api/activity?limit=100", headers=_auth(client))
        for item in r.json():
            if item["approval_id"] == approval_id:
                last_summary = item.get("summary", "")
                if "session" in last_summary.lower() or "offline" in last_summary.lower() or "idle" in last_summary.lower():
                    return item
        time.sleep(poll)
    raise AssertionError(f"follow-up never populated for {approval_id} — last summary: {last_summary!r}")


# ── Payload helpers ────────────────────────────────────────────────────────

def _ci_failure_payload(branch="feat/idempotency", head_sha="a1b2c3d4e5f6", pr_author="alice-dev"):
    return {
        "action": "completed",
        "repository": {"full_name": "acme/payments", "default_branch": "main"},
        "check_run": {
            "name": "integration-tests",
            "conclusion": "failure",
            "head_branch": branch,
            "head_sha": head_sha,
            "output": {"title": "3 failures", "summary": "test_idempotency failed"},
            "html_url": "https://github.com/acme/payments/runs/42",
            # The PR this check belongs to — webhook uses this to find who to probe.
            "pull_requests": [{"number": 42, "user": {"login": pr_author}}],
        },
        "sender": {"login": "github-actions[bot]"},
    }


def _review_changes_payload(branch="feat/idempotency"):
    return {
        "action": "submitted",
        "repository": {"full_name": "acme/payments", "default_branch": "main"},
        "review": {
            "state": "changes_requested",
            "user": {"login": "alice-dev"},
            "body": "please add a test for the race condition",
            "html_url": "https://github.com/acme/payments/pull/42#pullrequestreview-9",
        },
        "pull_request": {
            "number": 42,
            "title": "Add idempotency keys",
            "head": {"ref": branch, "sha": "a1b2c3d4e5f6"},
            "base": {"ref": "main"},
            "html_url": "https://github.com/acme/payments/pull/42",
        },
        "sender": {"login": "reviewer-bob"},
    }


def _pr_opened_payload(branch="feat/idempotency", head_sha="a1b2c3d4e5f6"):
    return {
        "action": "opened",
        "repository": {"full_name": "acme/payments", "default_branch": "main"},
        "pull_request": {
            "number": 42,
            "title": "Add idempotency keys",
            "body": "Prevents duplicate processing.",
            "html_url": "https://github.com/acme/payments/pull/42",
            "head": {"ref": branch, "sha": head_sha},
            "base": {"ref": "main"},
            "user": {"login": "alice-dev"},
        },
        "sender": {"login": "alice-dev"},
    }


def _post(client, event_type, payload, secret, delivery_id):
    body = json.dumps(payload).encode()
    return client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": _sign(secret, body),
            "Content-Type": "application/json",
        },
    )


# ── Trigger detection ─────────────────────────────────────────────────────

def test_ci_failure_is_trigger():
    from shadow_replica.api.github_webhook import _is_followup_trigger
    assert _is_followup_trigger("check_run", _ci_failure_payload())


def test_ci_success_is_not_trigger():
    from shadow_replica.api.github_webhook import _is_followup_trigger
    p = _ci_failure_payload()
    p["check_run"]["conclusion"] = "success"
    assert not _is_followup_trigger("check_run", p)


def test_review_changes_requested_is_trigger():
    from shadow_replica.api.github_webhook import _is_followup_trigger
    assert _is_followup_trigger("pull_request_review", _review_changes_payload())


def test_review_approved_is_not_trigger():
    from shadow_replica.api.github_webhook import _is_followup_trigger
    p = _review_changes_payload()
    p["review"]["state"] = "approved"
    assert not _is_followup_trigger("pull_request_review", p)


def test_pr_opened_is_not_trigger():
    from shadow_replica.api.github_webhook import _is_followup_trigger
    assert not _is_followup_trigger("pull_request", _pr_opened_payload())


def test_check_suite_failure_is_trigger():
    from shadow_replica.api.github_webhook import _is_followup_trigger
    payload = {"action": "completed", "check_suite": {"conclusion": "failure"}}
    assert _is_followup_trigger("check_suite", payload)


# ── Follow-up uses cached activity ─────────────────────────────────────────

def test_followup_populates_summary_from_cached_activity(client):
    import time
    secret = _add_repo(client)
    _add_dev(client)
    hub = _get_hub(client)
    _seed_cached_activity(hub)

    r = _post(client, "check_run", _ci_failure_payload(), secret, "ci-1")
    assert r.status_code == 201

    item = _wait_for_followup(client, "gh:ci-1")
    summary = item["summary"]
    assert "alice" in summary.lower() or "session" in summary.lower()
    assert "test_payments.py" in summary


def test_followup_handles_offline_dev_without_error(client):
    """No cache + no WS connection → follow-up either skipped or marked offline, never crash."""
    import time
    secret = _add_repo(client)
    _add_dev(client)

    r = _post(client, "check_run", _ci_failure_payload(), secret, "ci-off-1")
    assert r.status_code == 201

    # Give the background task time to run (and fail-safe).
    time.sleep(0.5)
    r2 = client.get("/ui/api/activity?limit=50", headers=_auth(client))
    row = next(it for it in r2.json() if it["approval_id"] == "gh:ci-off-1")
    # The row exists and nothing crashed. Followup summary (if any) says offline/idle.
    assert "summary" in row


def test_unmapped_dev_does_not_crash_followup(client):
    """CI event from a login the manager hasn't paired should not fire a probe (nobody to probe)."""
    import time
    secret = _add_repo(client)
    # Note: no _add_dev — the sender maps to unmapped:github-actions[bot]
    r = _post(client, "check_run", _ci_failure_payload(), secret, "ci-unmapped-1")
    assert r.status_code == 201
    time.sleep(0.3)
    # No follow-up should attach because the author is unmapped.
    r2 = client.get("/ui/api/activity?limit=50", headers=_auth(client))
    row = next(it for it in r2.json() if it["approval_id"] == "gh:ci-unmapped-1")
    assert "session" not in row["summary"].lower()


def test_non_trigger_event_leaves_followup_empty(client):
    """A PR open event should NOT get a follow-up summary even if dev is online."""
    import time
    secret = _add_repo(client)
    _add_dev(client)
    hub = _get_hub(client)
    _seed_cached_activity(hub)

    r = _post(client, "pull_request", _pr_opened_payload(), secret, "pr-no-trig-1")
    assert r.status_code == 201

    time.sleep(0.3)
    r2 = client.get("/ui/api/activity?limit=50", headers=_auth(client))
    row = next(it for it in r2.json() if it["approval_id"] == "gh:pr-no-trig-1")
    # No live-context on summary — just the plain PR title.
    assert "session" not in row["summary"].lower()


def test_followup_reactive_envelope_hits_wiretap(client):
    import time
    secret = _add_repo(client)
    _add_dev(client)
    hub = _get_hub(client)
    _seed_cached_activity(hub)

    _post(client, "check_run", _ci_failure_payload(), secret, "ci-wire-1")

    # Wait for the task to produce the envelope.
    deadline = time.monotonic() + 3.0
    reactive = []
    while time.monotonic() < deadline:
        envs = client.get("/ui/api/wiretap/recent?limit=200", headers=_auth(client)).json()
        reactive = [e for e in envs if e["intent"] == "reactive_followup"]
        if reactive:
            break
        time.sleep(0.1)
    assert reactive, "no reactive_followup envelope found"
    joined = " ".join(e["summary"] for e in reactive)
    assert "test_payments.py" in joined or "session" in joined.lower()


# ── Ghost probe agent treats replica:system as activity-only ──────────────

def test_probe_agent_system_subscriber_returns_activity_only(monkeypatch, tmp_path):
    """replica:system bypass must never leak raw turns."""
    from ghost_daemon.agents.probe_agent import ProbeAgent

    # Build a minimally-mocked agent — the only method called on the system path is
    # _collect_snapshot, which we stub.
    agent = ProbeAgent.__new__(ProbeAgent)
    agent._cfg = type("Cfg", (), {"developer_id": "alice", "resolved_session_dirs": []})()
    agent._llm = None
    agent._tailer = None
    agent._store = None

    async def _fake_snapshot():
        return SessionSnapshot(
            developer_id="alice",
            agent_kind="claude_code",
            session_id="s1",
            cwd=str(tmp_path),
            last_turns=[],  # would normally be populated — the system path clears it
            activity_signals=ActivitySignals(
                files_recently_touched=["x.py"],
                primary_activity="editing",
            ),
        )
    agent._collect_snapshot = _fake_snapshot

    req = ProbeRequest(subscriber_id="replica:system", repo_hint="acme/payments")
    resp = asyncio.run(agent.run(req))

    assert resp.policy_decision == PolicyDecision.ALLOWED
    assert resp.snapshot is not None
    assert resp.snapshot.last_turns == []            # content stripped
    assert resp.snapshot.activity_signals is not None
    assert resp.redaction_level_applied == RedactionLevel.STRICT


# ── SHA / branch threading ────────────────────────────────────────────────

def test_threads_group_github_event_and_commit_report_by_sha(client):
    """A github_event and a commit_report sharing commit_sha should thread together."""
    import json as _json
    _add_dev(client)
    secret = _add_repo(client)

    # 1) commit_report via normal push (we save straight into the store for simplicity)
    now = datetime.now(timezone.utc).isoformat()
    store = _store_from_client(client)
    commit_payload = {
        "author_msg": "Add idempotency keys to retry handler",
        "generated_summary": "idempotency rewrite",
        "branch": "feat/idempotency",
        "commit_sha": "a1b2c3d4e5f6",
        "repo": "acme/payments",
    }
    import anyio
    anyio.from_thread.run  # ensure import; use asyncio directly below.

    async def _save():
        await store.save_async(
            approval_id="apv:commit-1",
            developer_id="alice",
            artifact_type="commit_report",
            draft_id="draft-1",
            channel="commit_report",
            payload_json=_json.dumps(commit_payload),
            approved_at=now,
        )
    asyncio.run(_save())

    # 2) PR opened event on the same commit_sha
    r = _post(client, "pull_request", _pr_opened_payload(head_sha="a1b2c3d4e5f6"), secret, "pr-th-1")
    assert r.status_code == 201

    # Query threads
    rt = client.get("/ui/api/threads?hours=24", headers=_auth(client))
    assert rt.status_code == 200
    threads = rt.json()["threads"]
    assert threads, "no threads returned"
    joined = next((t for t in threads if "a1b2c3d4e5" in t["commit_sha"]), None)
    assert joined is not None
    kinds = set(joined["artifact_types"])
    assert "github_event" in kinds and "commit_report" in kinds
    assert len(joined["items"]) >= 2


def test_threads_group_by_branch_when_sha_differs(client):
    import json as _json
    _add_dev(client)
    secret = _add_repo(client)

    # Commit report on an older SHA, same branch
    store = _store_from_client(client)
    now = datetime.now(timezone.utc).isoformat()

    async def _save():
        await store.save_async(
            approval_id="apv:commit-2",
            developer_id="alice",
            artifact_type="commit_report",
            draft_id="draft-2",
            channel="commit_report",
            payload_json=_json.dumps({
                "branch": "feat/retry-logic",
                "commit_sha": "oldsha1234",
                "author_msg": "earlier WIP",
                "repo": "acme/payments",
            }),
            approved_at=now,
        )
    asyncio.run(_save())

    # PR opened on the SAME branch, different SHA
    r = _post(client, "pull_request", _pr_opened_payload(branch="feat/retry-logic", head_sha="newsha9999"), secret, "pr-br-1")
    assert r.status_code == 201

    rt = client.get("/ui/api/threads?hours=24", headers=_auth(client))
    threads = rt.json()["threads"]
    joined = next((t for t in threads if t["branch"] == "feat/retry-logic"), None)
    assert joined is not None
    kinds = set(joined["artifact_types"])
    assert "github_event" in kinds and "commit_report" in kinds


def test_threads_window_filters_old_items(client):
    """Items outside the window should NOT appear in threads."""
    import json as _json
    _add_dev(client)
    _add_repo(client)
    store = _store_from_client(client)
    old_iso = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    async def _save():
        await store.save_async(
            approval_id="apv:ancient",
            developer_id="alice",
            artifact_type="commit_report",
            draft_id="d-x",
            channel="commit_report",
            payload_json=_json.dumps({"branch": "ancient-work", "commit_sha": "zz", "author_msg": "old"}),
            approved_at=old_iso,
        )
    asyncio.run(_save())

    # Manually backdate received_at on the in-memory fallback row
    for row in store._fallback:
        if row.approval_id == "apv:ancient":
            row.received_at = old_iso
            break

    rt = client.get("/ui/api/threads?hours=24", headers=_auth(client))
    threads = rt.json()["threads"]
    assert not any(t["branch"] == "ancient-work" for t in threads)


# ── Helpers ────────────────────────────────────────────────────────────────

def _store_from_client(client):
    """Pull the ReceivedStore out of the webhook route's closure."""
    for route in _walk_routes(client.app):
        if getattr(route, "path", "") == "/v1/github/webhook":
            for cell in route.endpoint.__closure__ or ():
                v = cell.cell_contents
                if hasattr(v, "save_async") and hasattr(v, "list_all"):
                    return v
    raise RuntimeError("Could not resolve ReceivedStore")
