"""End-to-end tests for GitHub webhook ingestion.

Covers the full Option C contract:
  - HMAC-SHA256 signature verification (good + bad)
  - 1000-UUID delivery ring buffer dedupe
  - `ping` handshake does NOT write an artifact
  - Noise gate drops CI matrix spam, feature-branch pushes, PR synchronize
  - Attribution via github_login → developer_id, fallback to unmapped:<login>
  - Retroactive claim when a dev is paired AFTER the first events arrive
  - Pairing code carries github_login
  - Repo CRUD endpoints (add, list, delete)
  - Semantic excerpt extracts engineering text, not JSON URL boilerplate
  - Brief + Initiatives include github_event artifacts
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ────────────────────────────────────────────────────────────────

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


def _add_repo(client, slug: str = "acme/payments", secret: str = "whsec_test_secret_12345") -> str:
    """Register a repo directly against the live cfg (skip the HTTP add)."""
    from shadow_replica.config import WatchedRepo
    client._cfg.watched_repos.append(WatchedRepo(slug=slug, secret=secret))
    return secret


def _add_dev(client, developer_id: str, github_login: str = "") -> None:
    from shadow_replica.config import TrustedDev
    client._cfg.trusted_devs.append(TrustedDev(
        developer_id=developer_id,
        name=developer_id,
        token_value=f"vsub_{developer_id}_token",
        github_login=github_login,
    ))


def _post_hook(client, event_type: str, payload: dict, secret: str, delivery_id: str = "d-0001"):
    body = json.dumps(payload).encode("utf-8")
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


# ── Repo CRUD ──────────────────────────────────────────────────────────────

def test_add_repo_generates_secret_and_returns_webhook_url(client):
    r = client.post("/ui/api/repos/add", json={"slug": "acme/payments"}, headers=_auth(client))
    assert r.status_code == 200
    d = r.json()
    assert d["slug"] == "acme/payments"
    assert d["webhook_url"].endswith("/v1/github/webhook")
    assert d["secret"].startswith("whsec_")
    assert "github.com/acme/payments/settings/hooks/new" in d["settings_url"]


def test_add_repo_strips_github_com_prefix(client):
    r = client.post("/ui/api/repos/add", json={"slug": "https://github.com/acme/billing"}, headers=_auth(client))
    assert r.status_code == 200
    assert r.json()["slug"] == "acme/billing"


def test_add_repo_rejects_malformed_slug(client):
    r = client.post("/ui/api/repos/add", json={"slug": "no-slash"}, headers=_auth(client))
    assert r.status_code == 400


def test_list_repos_returns_watched(client):
    _add_repo(client, "acme/payments")
    _add_repo(client, "acme/billing", "whsec_other")
    r = client.get("/ui/api/repos", headers=_auth(client))
    assert r.status_code == 200
    slugs = [row["slug"] for row in r.json()]
    assert "acme/payments" in slugs and "acme/billing" in slugs


def test_remove_repo(client):
    _add_repo(client)
    r = client.delete("/ui/api/repos/acme/payments", headers=_auth(client))
    assert r.status_code == 200
    assert not any(w.slug == "acme/payments" for w in client._cfg.watched_repos)


def test_remove_repo_404(client):
    r = client.delete("/ui/api/repos/nope/nope", headers=_auth(client))
    assert r.status_code == 404


# ── Signature verification + dedupe ────────────────────────────────────────

def test_webhook_rejects_bad_signature(client):
    secret = _add_repo(client)
    body = json.dumps({"zen": "hi", "repository": {"full_name": "acme/payments"}}).encode()
    r = client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": "sha256=00" * 32,  # wrong digest
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 401


def test_webhook_rejects_unwatched_repo(client):
    body = json.dumps({"zen": "hi", "repository": {"full_name": "stranger/repo"}}).encode()
    r = client.post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": _sign("whsec_x", body),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 404


def test_webhook_dedupes_by_delivery_id(client):
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="alice-dev")
    payload = _pr_opened_payload()
    r1 = _post_hook(client, "pull_request", payload, secret, delivery_id="dup-1")
    r2 = _post_hook(client, "pull_request", payload, secret, delivery_id="dup-1")
    assert r1.status_code == 201
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate"


# ── Ping ───────────────────────────────────────────────────────────────────

def test_ping_updates_heartbeat_but_writes_no_artifact(client):
    secret = _add_repo(client)
    payload = {"zen": "Keep it logically awesome.", "repository": {"full_name": "acme/payments"}}
    r = _post_hook(client, "ping", payload, secret, delivery_id="ping-1")
    assert r.status_code == 200
    assert r.json()["status"] == "pong"

    # No github_event artifact should have been written: use the dashboard activity endpoint.
    r_act = client.get("/ui/api/activity?limit=100", headers=_auth(client))
    assert r_act.status_code == 200
    assert not any(item["artifact_type"] == "github_event" for item in r_act.json())

    # But the repo's heartbeat moved
    watched = next(r_ for r_ in client._cfg.watched_repos if r_.slug == "acme/payments")
    assert watched.last_event_at
    assert watched.event_count >= 1


# ── Noise gate ─────────────────────────────────────────────────────────────

def test_push_to_feature_branch_filtered(client):
    secret = _add_repo(client)
    body = {
        "ref": "refs/heads/feature/foo",
        "repository": {"full_name": "acme/payments", "default_branch": "main"},
        "pusher": {"name": "alice-dev"},
        "commits": [{"message": "wip"}],
    }
    r = _post_hook(client, "push", body, secret, delivery_id="push-feat-1")
    assert r.status_code == 200
    assert r.json()["status"] == "filtered"


def test_push_to_default_branch_ingested(client):
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="alice-dev")
    body = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "acme/payments", "default_branch": "main"},
        "pusher": {"name": "alice-dev"},
        "commits": [{"message": "ship rate limit"}],
        "after": "deadbeef1234",
        "compare": "https://github.com/acme/payments/compare/old...new",
    }
    r = _post_hook(client, "push", body, secret, delivery_id="push-main-1")
    assert r.status_code == 201


def test_check_run_in_progress_filtered(client):
    secret = _add_repo(client)
    body = {
        "action": "in_progress",
        "repository": {"full_name": "acme/payments"},
        "check_run": {"name": "tests", "status": "in_progress"},
    }
    r = _post_hook(client, "check_run", body, secret, delivery_id="cr-1")
    assert r.status_code == 200
    assert r.json()["status"] == "filtered"


def test_check_run_completed_success_filtered(client):
    secret = _add_repo(client)
    body = {
        "action": "completed",
        "repository": {"full_name": "acme/payments"},
        "check_run": {"name": "tests", "conclusion": "success"},
    }
    r = _post_hook(client, "check_run", body, secret, delivery_id="cr-2")
    assert r.status_code == 200
    assert r.json()["status"] == "filtered"


def test_check_run_completed_failure_ingested(client):
    secret = _add_repo(client)
    body = {
        "action": "completed",
        "repository": {"full_name": "acme/payments"},
        "check_run": {"name": "tests", "conclusion": "failure",
                       "output": {"title": "3 failures", "summary": "test_x failed"}},
        "sender": {"login": "github-actions[bot]"},
    }
    r = _post_hook(client, "check_run", body, secret, delivery_id="cr-3")
    assert r.status_code == 201


def test_pr_synchronize_filtered(client):
    secret = _add_repo(client)
    body = _pr_opened_payload()
    body["action"] = "synchronize"
    r = _post_hook(client, "pull_request", body, secret, delivery_id="pr-sync-1")
    assert r.status_code == 200
    assert r.json()["status"] == "filtered"


# ── Attribution ────────────────────────────────────────────────────────────

def test_pr_attributed_to_mapped_developer(client):
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="alice-dev")
    r = _post_hook(client, "pull_request", _pr_opened_payload(), secret, delivery_id="pr-attr-1")
    assert r.status_code == 201
    assert r.json()["developer_id"] == "alice"


def test_pr_from_unknown_login_goes_to_unmapped(client):
    secret = _add_repo(client)
    r = _post_hook(client, "pull_request", _pr_opened_payload(), secret, delivery_id="pr-attr-2")
    assert r.status_code == 201
    assert r.json()["developer_id"] == "unmapped:alice-dev"


def test_github_login_match_is_case_insensitive(client):
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="Alice-Dev")  # dev's config has mixed case
    payload = _pr_opened_payload()
    payload["sender"]["login"] = "ALICE-DEV"            # GitHub sends uppercase
    payload["pull_request"]["user"]["login"] = "alice-DEV"
    r = _post_hook(client, "pull_request", payload, secret, delivery_id="pr-case-1")
    assert r.status_code == 201
    assert r.json()["developer_id"] == "alice"


# ── Retroactive claim ──────────────────────────────────────────────────────

def test_pairing_with_github_login_reclaims_unmapped_events(client):
    secret = _add_repo(client)
    # Alice makes a PR BEFORE she's paired
    r = _post_hook(client, "pull_request", _pr_opened_payload(), secret, delivery_id="pr-reclaim-1")
    assert r.json()["developer_id"] == "unmapped:alice-dev"

    # Now pair Alice with her github_login — the add endpoint should reassign.
    r2 = client.post(
        "/ui/api/developers/add",
        json={
            "developer_id": "alice", "name": "Alice",
            "token": "vsub_alicepair", "github_login": "alice-dev",
        },
        headers=_auth(client),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["reassigned_artifacts"] >= 1


# ── Pairing code carries github_login ──────────────────────────────────────

def test_pairing_code_roundtrip_carries_github_login(client):
    from ghost_daemon.__main__ import _build_pairing_code

    class _StubCfg:
        developer_id = "bob"
        display_name = "Bob"

    class _StubIdentity:
        subscriber_id = "sub_bob"
        token_value = "vsub_bobtoken"
        receive_url = "http://replica:7475"

    code = _build_pairing_code(_StubCfg(), _StubIdentity(), github_login="bob-eng")
    r = client.post("/ui/api/pair/decode", json={"pairing_code": code}, headers=_auth(client))
    assert r.status_code == 200
    assert r.json()["github_login"] == "bob-eng"


# ── Semantic excerpt ────────────────────────────────────────────────────────

def test_semantic_excerpt_captures_pr_body_not_url_boilerplate():
    from shadow_replica.api.github_webhook import _extract_semantic_excerpt

    payload = {
        "action": "opened",
        "pull_request": {
            "title": "Add idempotency keys to retry handler",
            "body": "Prevents duplicate processing when upstream times out.\n\nFixes #412",
            "html_url": "https://github.com/acme/payments/pull/42",
            "statuses_url": "https://api.github.com/repos/…/statuses/…",
            "commits_url": "https://api.github.com/repos/…/commits",
        },
    }
    text = _extract_semantic_excerpt("pull_request", payload)
    assert "idempotency keys" in text
    assert "duplicate processing" in text
    # Should not be dominated by URL noise
    assert "statuses_url" not in text and "api.github.com" not in text


def test_semantic_excerpt_truncated_to_2kb():
    from shadow_replica.api.github_webhook import _extract_semantic_excerpt
    payload = {"pull_request": {"title": "x", "body": "a" * 5000}}
    text = _extract_semantic_excerpt("pull_request", payload)
    assert len(text) <= 2048


def test_semantic_excerpt_handles_push_commits():
    from shadow_replica.api.github_webhook import _extract_semantic_excerpt
    payload = {
        "commits": [
            {"message": "Fix auth bug in token refresh"},
            {"message": "Add tests for refresh race"},
        ],
    }
    text = _extract_semantic_excerpt("push", payload)
    assert "token refresh" in text
    assert "refresh race" in text


# ── Brief & Initiatives integration ────────────────────────────────────────

def test_github_event_shipped_in_brief_when_merged(client):
    from datetime import datetime, timezone, timedelta
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="alice-dev")

    payload = _pr_opened_payload()
    payload["action"] = "closed"
    payload["pull_request"]["merged"] = True
    payload["pull_request"]["title"] = "Add rate-limit middleware"

    r = _post_hook(client, "pull_request", payload, secret, delivery_id="pr-merged-1")
    assert r.status_code == 201

    rb = client.get("/ui/api/brief?hours=24", headers=_auth(client))
    assert rb.status_code == 200
    data = rb.json()
    shipped = "\n".join(data["shipped"])
    assert "rate-limit middleware" in shipped or "merged PR" in shipped


def test_github_event_check_failure_lands_in_bottlenecks(client):
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="alice-dev")
    body = {
        "action": "completed",
        "repository": {"full_name": "acme/payments"},
        "check_run": {"name": "integration-tests", "conclusion": "failure",
                       "output": {"title": "DB migration failed", "summary": "exit 1"}},
        "sender": {"login": "alice-dev"},
    }
    r = _post_hook(client, "check_run", body, secret, delivery_id="cr-brief-1")
    assert r.status_code == 201

    rb = client.get("/ui/api/brief?hours=24", headers=_auth(client))
    data = rb.json()
    bottlenecks = "\n".join(data["bottlenecks"])
    assert "check" in bottlenecks.lower()


def test_github_event_clusters_into_initiative(client):
    secret = _add_repo(client)
    _add_dev(client, "alice", github_login="alice-dev")

    # Two github events on the same theme should cluster together.
    for i, title in enumerate(["Add rate-limit middleware for payments",
                                "Tune rate-limit thresholds in payments API"]):
        p = _pr_opened_payload()
        p["pull_request"]["title"] = title
        p["pull_request"]["body"] = "rate limiting changes to payments API"
        p["pull_request"]["number"] = 100 + i
        _post_hook(client, "pull_request", p, secret, delivery_id=f"init-{i}")

    r = client.get("/ui/api/initiatives?hours=168", headers=_auth(client))
    assert r.status_code == 200
    items = r.json().get("items", r.json())  # support either shape
    # Flatten shipped/in_flight bullets across all initiatives
    all_text = json.dumps(items)
    assert "rate-limit" in all_text or "rate limit" in all_text


# ── Helpers ────────────────────────────────────────────────────────────────

def _pr_opened_payload() -> dict:
    return {
        "action": "opened",
        "repository": {"full_name": "acme/payments", "default_branch": "main"},
        "pull_request": {
            "number": 42,
            "title": "Add idempotency keys",
            "body": "Prevents duplicate processing on upstream timeouts.",
            "html_url": "https://github.com/acme/payments/pull/42",
            "merged": False,
            "head": {"ref": "feat/idempotency", "sha": "abc1234"},
            "base": {"ref": "main"},
            "user": {"login": "alice-dev"},
        },
        "sender": {"login": "alice-dev"},
    }
