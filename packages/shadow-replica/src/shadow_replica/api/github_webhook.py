"""GitHub webhook receiver.

One endpoint — `POST /v1/github/webhook` — accepts push events from any
registered `WatchedRepo`. The replica never calls GitHub itself; events are
authenticated by per-repo HMAC-SHA256 shared secret.

Design points:
    - Signature verification is per-repo, constant-time.
    - 1000-entry UUID ring buffer dedupes replays / GitHub retries.
    - `ping` events update `last_event_at` + emit Wiretap, but do NOT write an
      artifact (otherwise the dashboard would be noisy with handshake events).
    - A noise gate filters CI matrix spam (in_progress check_runs), feature-branch
      pushes (already covered by PR events), and `pull_request.synchronize` churn.
    - The semantic excerpt is engineering text (titles, bodies, commit messages),
      NOT raw JSON — so Qdrant embeddings and search surface the useful content.
    - Unmapped logins get `developer_id="unmapped:<login>"` so events are kept
      and can be reattributed when a dev is paired with that `github_login`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from shadow_replica.notify.notifier import NotificationConfig, dispatch_all
from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent
from vantage.schemas import GithubEvent, ProbeRequest

if TYPE_CHECKING:
    from shadow_replica.config import ReplicaConfig, WatchedRepo
    from shadow_replica.connections.ws_hub import WSHub
    from shadow_replica.storage.received_store import ReceivedStore
    from shadow_replica.wiretap.bus import WiretapBus

logger = logging.getLogger(__name__)

_REPLICA_REF = A2AAgentRef(kind="shadow-replica", label="Shadow Replica")
_GITHUB_REF = A2AAgentRef(kind="github", label="GitHub")
_DELIVERY_RING_CAP = 1000
_EXCERPT_MAX = 2048
_SYSTEM_SUBSCRIBER = "replica:system"
_FOLLOWUP_PROBE_TIMEOUT = 4.0
_FOLLOWUP_ACTIVITY_FRESHNESS_SECS = 600       # cached activity within 10 min is "live"


def _followup_target_login(event_type: str, payload: dict) -> str:
    """Return the GitHub login of the person whose *session* the follow-up probes.

    For a CI failure, the sender is `github-actions[bot]` and the reviewer on a
    changes-requested review is NOT the person who needs to act — the PR
    author is. This helper routes the follow-up to the right dev.
    """
    pr = payload.get("pull_request") or {}
    if event_type == "pull_request_review":
        pr_user = (pr.get("user") or {}).get("login", "")
        if pr_user:
            return pr_user
    if event_type in ("check_run", "check_suite"):
        node = payload.get("check_run") or payload.get("check_suite") or {}
        prs = node.get("pull_requests") or []
        # check_run webhooks sometimes embed the PR author under pull_requests[0].user
        for pr_entry in prs:
            user = (pr_entry.get("user") or {}).get("login", "") if isinstance(pr_entry, dict) else ""
            if user:
                return user
    return ""


def _is_followup_trigger(event_type: str, payload: dict) -> bool:
    """Trigger a reactive follow-up on CI failures and changes-requested reviews.

    These are the two events that most often mean "the dev needs to intervene";
    everything else can wait for the next polling cycle.
    """
    action = payload.get("action", "")
    if event_type == "check_run" and action == "completed":
        conclusion = (payload.get("check_run") or {}).get("conclusion", "")
        return conclusion in {"failure", "timed_out", "action_required"}
    if event_type == "check_suite" and action == "completed":
        conclusion = (payload.get("check_suite") or {}).get("conclusion", "")
        return conclusion in {"failure", "timed_out", "action_required"}
    if event_type == "pull_request_review" and action == "submitted":
        state = (payload.get("review") or {}).get("state", "")
        return state == "changes_requested"
    return False


# ── Noise gate ──────────────────────────────────────────────────────────────

_PR_ALLOWED_ACTIONS = frozenset({
    "opened", "closed", "reopened", "ready_for_review", "review_requested",
})
_CHECK_FAIL_CONCLUSIONS = frozenset({
    "failure", "timed_out", "action_required", "cancelled",
})


def _should_ingest(event_type: str, payload: dict) -> bool:
    """Return True if this event is interesting enough to persist."""
    action = payload.get("action", "")

    if event_type == "pull_request":
        return action in _PR_ALLOWED_ACTIONS

    if event_type == "push":
        ref = payload.get("ref", "")
        default_branch = (payload.get("repository") or {}).get("default_branch", "")
        if not default_branch:
            return False
        return ref == f"refs/heads/{default_branch}"

    if event_type == "check_run":
        if action != "completed":
            return False
        conclusion = (payload.get("check_run") or {}).get("conclusion", "")
        return conclusion in _CHECK_FAIL_CONCLUSIONS

    if event_type == "check_suite":
        if action != "completed":
            return False
        conclusion = (payload.get("check_suite") or {}).get("conclusion", "")
        return conclusion in _CHECK_FAIL_CONCLUSIONS

    # Reviews, review comments, issue events, releases — let through if present.
    return True


# ── Semantic excerpt ────────────────────────────────────────────────────────

def _extract_semantic_excerpt(event_type: str, payload: dict) -> str:
    """Pull human-readable engineering text from the payload.

    GitHub webhook bodies are dominated by URL boilerplate (every PR has ~40
    `_url` fields). Naively slicing the first 2KB of JSON gives embeddings
    almost nothing to work with. This picks out just the fields a human would
    care about and concatenates them.
    """
    parts: list[str] = []
    pr = payload.get("pull_request") or {}
    review = payload.get("review") or {}
    comment = payload.get("comment") or {}
    check_run = payload.get("check_run") or {}
    check_suite = payload.get("check_suite") or {}

    if event_type == "pull_request":
        parts.append(pr.get("title", ""))
        parts.append(pr.get("body", "") or "")
    elif event_type == "pull_request_review":
        parts.append(f"review by {(review.get('user') or {}).get('login', '')}: {review.get('state', '')}")
        parts.append(review.get("body", "") or "")
        parts.append(pr.get("title", ""))
    elif event_type == "pull_request_review_comment":
        parts.append(comment.get("body", "") or "")
        parts.append(f"on {comment.get('path', '')}")
        parts.append(pr.get("title", ""))
    elif event_type == "push":
        for c in payload.get("commits", []) or []:
            msg = (c.get("message") or "").strip()
            if msg:
                parts.append(msg)
    elif event_type == "check_run":
        output = check_run.get("output") or {}
        parts.append(check_run.get("name", ""))
        parts.append(f"{check_run.get('conclusion', '')}: {output.get('title', '')}")
        parts.append(output.get("summary", "") or "")
    elif event_type == "check_suite":
        parts.append(check_suite.get("conclusion", ""))
        for pr_summary in check_suite.get("pull_requests", []) or []:
            parts.append(f"PR #{pr_summary.get('number', '')}")
    elif event_type == "issues":
        issue = payload.get("issue") or {}
        parts.append(issue.get("title", ""))
        parts.append(issue.get("body", "") or "")
    elif event_type == "release":
        release = payload.get("release") or {}
        parts.append(release.get("name", "") or release.get("tag_name", ""))
        parts.append(release.get("body", "") or "")

    text = " · ".join(p for p in parts if p).strip()
    return text[:_EXCERPT_MAX]


# ── Deterministic summaries ────────────────────────────────────────────────

def _build_summary(event_type: str, payload: dict, author_login: str) -> str:
    pr = payload.get("pull_request") or {}
    action = payload.get("action", "")
    actor = author_login or "someone"

    if event_type == "pull_request":
        num = pr.get("number", "")
        title = (pr.get("title") or "").strip()
        if action == "closed":
            verb = "merged" if pr.get("merged") else "closed"
            return f"{actor} {verb} PR #{num}: {title}"[:200]
        if action in ("opened", "reopened"):
            return f"{actor} {action} PR #{num}: {title}"[:200]
        if action == "ready_for_review":
            return f"{actor} marked PR #{num} ready: {title}"[:200]
        if action == "review_requested":
            return f"{actor} requested review on PR #{num}: {title}"[:200]
        return f"{actor} {action} PR #{num}: {title}"[:200]
    if event_type == "pull_request_review":
        review = payload.get("review") or {}
        state = review.get("state", "")
        num = pr.get("number", "")
        return f"{actor} {state} review on PR #{num}: {pr.get('title', '')[:120]}"[:200]
    if event_type == "pull_request_review_comment":
        comment = payload.get("comment") or {}
        num = pr.get("number", "")
        return f"{actor} commented on PR #{num} · {comment.get('path', '')}"[:200]
    if event_type == "push":
        commits = payload.get("commits") or []
        ref = payload.get("ref", "").replace("refs/heads/", "")
        if commits:
            first = commits[0].get("message", "").split("\n", 1)[0][:80]
            extra = f" (+{len(commits)-1} more)" if len(commits) > 1 else ""
            return f"{actor} pushed to {ref}: {first}{extra}"[:200]
        return f"{actor} pushed to {ref}"[:200]
    if event_type == "check_run":
        check = payload.get("check_run") or {}
        return f"check {check.get('conclusion', '')}: {check.get('name', '')}"[:200]
    if event_type == "check_suite":
        suite = payload.get("check_suite") or {}
        return f"check_suite {suite.get('conclusion', '')}"[:200]
    if event_type == "issues":
        issue = payload.get("issue") or {}
        return f"{actor} {action} issue #{issue.get('number', '')}: {issue.get('title', '')[:120]}"[:200]
    return f"{actor} {event_type}.{action}"[:200]


# ── Field pluckers ─────────────────────────────────────────────────────────

def _pluck_url(event_type: str, payload: dict) -> str:
    pr = payload.get("pull_request") or {}
    if event_type.startswith("pull_request"):
        if event_type == "pull_request_review":
            return (payload.get("review") or {}).get("html_url", "") or pr.get("html_url", "")
        if event_type == "pull_request_review_comment":
            return (payload.get("comment") or {}).get("html_url", "") or pr.get("html_url", "")
        return pr.get("html_url", "")
    if event_type == "push":
        return payload.get("compare", "") or ""
    if event_type in ("check_run", "check_suite"):
        node = payload.get("check_run") or payload.get("check_suite") or {}
        return node.get("html_url", "") or node.get("details_url", "")
    if event_type == "issues":
        return (payload.get("issue") or {}).get("html_url", "")
    return ""


def _pluck_branch(event_type: str, payload: dict) -> str:
    pr = payload.get("pull_request") or {}
    if event_type.startswith("pull_request"):
        return (pr.get("head") or {}).get("ref", "") or ""
    if event_type == "push":
        return payload.get("ref", "").replace("refs/heads/", "")
    if event_type in ("check_run", "check_suite"):
        node = payload.get("check_run") or payload.get("check_suite") or {}
        return node.get("head_branch", "") or ""
    return ""


def _pluck_commit_sha(event_type: str, payload: dict) -> str:
    pr = payload.get("pull_request") or {}
    if event_type.startswith("pull_request"):
        return (pr.get("head") or {}).get("sha", "") or ""
    if event_type == "push":
        return payload.get("after", "") or (payload.get("head_commit") or {}).get("id", "")
    if event_type in ("check_run", "check_suite"):
        node = payload.get("check_run") or payload.get("check_suite") or {}
        return node.get("head_sha", "") or ""
    return ""


def _pluck_pr_number(event_type: str, payload: dict) -> int | None:
    pr = payload.get("pull_request") or {}
    if pr:
        return pr.get("number")
    if event_type == "issues":
        issue = payload.get("issue") or {}
        n = issue.get("number")
        return int(n) if isinstance(n, int) else None
    return None


def _pluck_title(event_type: str, payload: dict) -> str:
    pr = payload.get("pull_request") or {}
    if pr.get("title"):
        return str(pr["title"])[:300]
    issue = payload.get("issue") or {}
    if issue.get("title"):
        return str(issue["title"])[:300]
    if event_type == "push":
        commits = payload.get("commits") or []
        if commits:
            return str(commits[0].get("message", "")).split("\n", 1)[0][:300]
    return ""


def _pluck_author_login(event_type: str, payload: dict) -> str:
    # Prefer the event author over the generic sender when meaningful.
    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        return (pr.get("user") or {}).get("login", "") or (payload.get("sender") or {}).get("login", "")
    if event_type == "pull_request_review":
        return ((payload.get("review") or {}).get("user") or {}).get("login", "") \
            or (payload.get("sender") or {}).get("login", "")
    if event_type == "pull_request_review_comment":
        return ((payload.get("comment") or {}).get("user") or {}).get("login", "") \
            or (payload.get("sender") or {}).get("login", "")
    if event_type == "push":
        pusher = payload.get("pusher") or {}
        return pusher.get("name", "") or (payload.get("sender") or {}).get("login", "")
    return (payload.get("sender") or {}).get("login", "")


# ── Attribution ────────────────────────────────────────────────────────────

def _attribute(cfg: "ReplicaConfig", author_login: str) -> tuple[str, str]:
    """Return (developer_id, developer_name) for the given GitHub login.

    If no trusted dev claims this login, return the unmapped sentinel so the
    artifact is still saved and can be reclaimed later.
    """
    if not author_login:
        return ("unmapped:unknown", f"unmapped:unknown")
    needle = author_login.lower()
    for dev in cfg.trusted_devs:
        if dev.github_login and dev.github_login.lower() == needle:
            return (dev.developer_id, dev.name or dev.developer_id)
    return (f"unmapped:{author_login}", f"unmapped:{author_login}")


# ── Signature verification ────────────────────────────────────────────────

def verify_github_signature(body: bytes, secret: str, header_value: str) -> bool:
    """Constant-time HMAC-SHA256 check against GitHub's X-Hub-Signature-256 header."""
    if not header_value or not secret:
        return False
    if not header_value.startswith("sha256="):
        return False
    provided = header_value.removeprefix("sha256=").strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(provided, expected)
    except Exception:
        return False


# ── Router ─────────────────────────────────────────────────────────────────

def make_router(
    cfg: "ReplicaConfig",
    store: "ReceivedStore",
    bus: "WiretapBus | None" = None,
    notifications: list[NotificationConfig] | None = None,
    hub: "WSHub | None" = None,
) -> APIRouter:
    r = APIRouter()
    delivery_ring: deque[str] = deque(maxlen=_DELIVERY_RING_CAP)
    _notifications = notifications or []

    @r.post("/v1/github/webhook")
    async def github_webhook(request: Request):
        body = await request.body()
        event_type = request.headers.get("X-GitHub-Event", "").strip()
        delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
        sig_header = request.headers.get("X-Hub-Signature-256", "").strip()

        if not event_type:
            raise HTTPException(400, "Missing X-GitHub-Event header")

        # Parse body first so we can read repository.full_name to pick the right secret.
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON body")

        repository = payload.get("repository") or {}
        repo_slug = repository.get("full_name", "") or ""
        watched = _find_repo(cfg, repo_slug)
        if watched is None:
            logger.warning("GitHub webhook for unwatched repo %r (event=%s)", repo_slug, event_type)
            raise HTTPException(404, f"Repo {repo_slug!r} is not watched")

        # Verify signature against the per-repo secret.
        if not verify_github_signature(body, watched.secret, sig_header):
            logger.warning("GitHub webhook signature mismatch for %s (event=%s)", repo_slug, event_type)
            raise HTTPException(401, "Bad signature")

        # Dedupe replays using GitHub's delivery UUID.
        if delivery_id:
            if delivery_id in delivery_ring:
                return JSONResponse({"status": "duplicate", "delivery_id": delivery_id}, status_code=200)
            delivery_ring.append(delivery_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        watched.last_event_at = now_iso
        watched.event_count += 1

        # ping handshake — update the repo's heartbeat, publish, but do NOT write an artifact.
        if event_type == "ping":
            if bus is not None:
                try:
                    bus.publish(A2AEnvelope(
                        intent=A2AIntent.ARTIFACT_PUSH,
                        from_agent=_GITHUB_REF,
                        to_agent=_REPLICA_REF,
                        status="ok",
                        summary=f"github ping · {repo_slug}",
                        payload={"repo": repo_slug, "event_type": "ping"},
                    ))
                except Exception as exc:
                    logger.debug("Wiretap publish failed: %s", exc)
            _persist_repo_update(cfg)
            return JSONResponse({"status": "pong", "repo": repo_slug}, status_code=200)

        # Noise gate — drop uninteresting sub-events but still ack 200 so GitHub stops retrying.
        if not _should_ingest(event_type, payload):
            _persist_repo_update(cfg)
            return JSONResponse({"status": "filtered", "event_type": event_type}, status_code=200)

        action = payload.get("action", "")
        full_event_type = f"{event_type}.{action}" if action else event_type

        author_login = _pluck_author_login(event_type, payload)
        developer_id, developer_name = _attribute(cfg, author_login)

        event = GithubEvent(
            event_type=full_event_type,
            action=action,
            repo=repo_slug,
            pr_number=_pluck_pr_number(event_type, payload),
            title=_pluck_title(event_type, payload),
            url=_pluck_url(event_type, payload),
            author_login=author_login,
            branch=_pluck_branch(event_type, payload),
            commit_sha=_pluck_commit_sha(event_type, payload),
            summary=_build_summary(event_type, payload, author_login),
            raw_payload_excerpt=_extract_semantic_excerpt(event_type, payload),
        )

        payload_json = event.model_dump_json()
        # Scope the approval_id by delivery_id so GitHub replays dedupe at the store layer too.
        approval_id = f"gh:{delivery_id or event.event_id}"

        await store.save_async(
            approval_id=approval_id,
            developer_id=developer_id,
            artifact_type="github_event",
            draft_id="",
            channel="github_event",
            payload_json=payload_json,
            approved_at=event.received_at.isoformat(),
        )
        _persist_repo_update(cfg)

        # Reactive A2A: on trigger events, resolve the person *affected* by the
        # event (the PR author, not the bot/reviewer who sent the webhook) and
        # schedule a background follow-up that enriches the stored event with
        # that dev's live session context — "Alice is editing test_payments.py".
        if hub is not None and _is_followup_trigger(event_type, payload):
            target_login = _followup_target_login(event_type, payload) or author_login
            target_dev_id, target_dev_name = _attribute(cfg, target_login)
            if not target_dev_id.startswith("unmapped:"):
                asyncio.create_task(_followup(
                    hub=hub, store=store, bus=bus,
                    event=event, approval_id=approval_id,
                    developer_id=target_dev_id, developer_name=target_dev_name,
                ))

        if bus is not None:
            try:
                bus.publish(A2AEnvelope(
                    intent=A2AIntent.ARTIFACT_PUSH,
                    from_agent=_GITHUB_REF,
                    to_agent=_REPLICA_REF,
                    summary=event.summary,
                    payload={
                        "event_type": full_event_type,
                        "repo": repo_slug,
                        "developer_id": developer_id,
                        "author_login": author_login,
                        "url": event.url,
                        "pr_number": event.pr_number,
                    },
                ))
            except Exception as exc:
                logger.debug("Wiretap publish failed: %s", exc)

        if _notifications:
            await dispatch_all(
                _notifications,
                artifact_type="github_event",
                developer_name=developer_name,
                developer_id=developer_id,
                payload_json=payload_json,
            )

        return JSONResponse(
            {"status": "ok", "event_type": full_event_type, "developer_id": developer_id},
            status_code=201,
        )

    return r


def _find_repo(cfg: "ReplicaConfig", slug: str) -> "WatchedRepo | None":
    for repo in cfg.watched_repos:
        if repo.slug == slug:
            return repo
    return None


def _persist_repo_update(cfg: "ReplicaConfig") -> None:
    """Fire-and-forget persistence of in-memory counter updates. Non-fatal."""
    try:
        from pathlib import Path as _P
        from shadow_replica.config import _write, _replica_config_dir
        _write(cfg, _P(_replica_config_dir()) / "replica.toml")
    except Exception as exc:
        logger.debug("replica.toml write skipped: %s", exc)


async def _followup(
    hub: "WSHub",
    store: "ReceivedStore",
    bus: "WiretapBus | None",
    event: GithubEvent,
    approval_id: str,
    developer_id: str,
    developer_name: str,
) -> None:
    """Enrich a stored GithubEvent with the dev's live session context.

    Prefers the WS hub's cached activity (updated by periodic IntentBeacons) —
    no round-trip needed when the ghost is actively broadcasting. Falls back to
    a real reactive probe when the dev is online but the cache is stale.
    """
    try:
        summary, status = await _build_followup_summary(hub, developer_id, developer_name, event)
        if not summary:
            # Nothing useful to add. Leave the event as-is.
            return

        # Rewrite the stored artifact with the enriched summary. We re-save under
        # the same approval_id; the Qdrant upsert replaces the point in place.
        event.followup_summary = summary
        event.followup_status = status
        await store.save_async(
            approval_id=approval_id,
            developer_id=developer_id,
            artifact_type="github_event",
            draft_id="",
            channel="github_event",
            payload_json=event.model_dump_json(),
            approved_at=event.received_at.isoformat(),
        )

        if bus is not None:
            try:
                bus.publish(A2AEnvelope(
                    intent=A2AIntent.REACTIVE_FOLLOWUP,
                    from_agent=_REPLICA_REF,
                    to_agent=A2AAgentRef(
                        kind="manager-ui",
                        label=developer_name or developer_id,
                    ),
                    status=status,
                    summary=f"follow-up · {event.summary[:80]} → {summary[:100]}",
                    payload={
                        "approval_id": approval_id,
                        "developer_id": developer_id,
                        "event_type": event.event_type,
                        "branch": event.branch,
                        "commit_sha": event.commit_sha,
                        "followup_summary": summary,
                        "followup_status": status,
                    },
                ))
            except Exception as exc:
                logger.debug("Wiretap publish failed: %s", exc)
    except Exception as exc:
        logger.warning("Reactive follow-up failed for %s: %s", developer_id, exc)


async def _build_followup_summary(
    hub: "WSHub",
    developer_id: str,
    developer_name: str,
    event: GithubEvent,
) -> tuple[str, str]:
    """Return (human-readable summary, status token)."""
    online = hub.is_online(developer_id)
    cached = hub.last_activity(developer_id)
    if cached is not None:
        signals, captured_at = cached
        age = (datetime.now(timezone.utc) - _tz(captured_at)).total_seconds()
        if age <= _FOLLOWUP_ACTIVITY_FRESHNESS_SECS:
            return (_format_activity(signals, developer_name, age_seconds=age), "online_cached")

    if not online:
        return (f"{developer_name} is offline — no live context available.", "offline")

    # Online but no fresh cached activity — issue a real reactive probe.
    try:
        resp = await hub.probe(
            developer_id,
            ProbeRequest(
                subscriber_id=_SYSTEM_SUBSCRIBER,
                repo_hint=event.repo,
            ),
            timeout=_FOLLOWUP_PROBE_TIMEOUT,
        )
    except Exception as exc:
        logger.debug("Reactive probe raised: %s", exc)
        resp = None

    if resp is None or resp.snapshot is None or resp.snapshot.activity_signals is None:
        return (f"{developer_name} is online but their session is idle.", "online_probed")

    sig = resp.snapshot.activity_signals
    captured_at = resp.snapshot.captured_at
    age = (datetime.now(timezone.utc) - _tz(captured_at)).total_seconds()
    return (_format_activity(sig, developer_name, age_seconds=age), "online_probed")


def _tz(dt: datetime) -> datetime:
    """Make a datetime timezone-aware UTC for safe subtraction."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_activity(sig, developer_name: str, age_seconds: float) -> str:
    """Pack ActivitySignals into one scanable line for the bottleneck card."""
    bits: list[str] = []
    if sig.primary_activity and sig.primary_activity != "unknown":
        bits.append(sig.primary_activity)
    if sig.active_repo:
        bits.append(sig.active_repo)
    if sig.inferred_branch:
        bits.append(sig.inferred_branch)
    files = sig.files_recently_touched[:3] if sig.files_recently_touched else []
    tail = ""
    if files:
        tail = " · editing " + ", ".join(files)
    age_label = _ago(age_seconds)
    head = f"{developer_name}'s session · "
    if bits:
        return f"{head}{' · '.join(bits)}{tail} ({age_label})"
    return f"{head}active{tail} ({age_label})"


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
