from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ghost_daemon.approval.gate import ApprovalGate
from ghost_daemon.audit.log import AuditLog
from ghost_daemon.storage.sqlite_store import SQLiteStore
from ghost_daemon.storage.qdrant_store import QdrantStore
from ghost_daemon.ui.local_guard import install_local_guard
from vantage.schemas import ApprovalSource, Channel

if TYPE_CHECKING:
    from ghost_daemon.config import Config
    from ghost_daemon.daemon import PairingStatus
    from ghost_daemon.insights.collision_poller import CollisionPoller
    from ghost_daemon.insights.edit_store import EditDiffStore
    from ghost_daemon.insights.scrubber_hits import ScrubberHitStore
    from ghost_daemon.models.registry import LLMProvider
    from ghost_daemon.subscribers.registry import SubscriberRegistry


# Per-kind preview shapes for the Raw X-Ray pane. Each entry is a
# fixed-length visual that mimics the look of the original secret so a human
# can tell at a glance "that was an AWS key" vs "that was a JWT". The actual
# secret value is never persisted — only this stylized placeholder is shown.
_RAW_KIND_SHAPES: dict[str, str] = {
    "aws_access_key":  "AKIA" + "▪" * 16,
    "aws_secret_key":  "▪" * 40,
    "github_token":    "ghp_" + "▪" * 36,
    "gitlab_token":    "glpat-" + "▪" * 20,
    "slack_token":     "xoxb-" + "▪" * 24,
    "anthropic_key":   "sk-ant-" + "▪" * 24,
    "openai_key":      "sk-" + "▪" * 32,
    "stripe_live":     "sk_live_" + "▪" * 24,
    "gcp_api_key":     "AIza" + "▪" * 35,
    "azure_acct_key":  "AccountKey=" + "▪" * 44,
    "jwt":             "eyJ▪▪▪.eyJ▪▪▪▪▪▪▪▪.▪▪▪▪▪▪▪▪",
    "basic_auth_url":  "https://▪▪▪▪:▪▪▪▪@host.example",
    "bearer_token":    "Bearer " + "▪" * 32,
    "dotenv_secret":   "VAR=" + "▪" * 20,
    "private_key":     "-----BEGIN PRIVATE KEY-----\n" + "▪" * 48 + "\n-----END PRIVATE KEY-----",
    "high_entropy":    "▪" * 24,
}


def _raw_shape_for(kind: str) -> str:
    """Return a visual placeholder for the Raw pane in the X-Ray view."""
    return _RAW_KIND_SHAPES.get(kind, "▪" * 16)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Use Jinja2 directly (cache_size=0) to avoid Starlette/Jinja2 version
# incompatibility where env globals (dicts) end up in the LRU cache key tuple.
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
    cache_size=0,
)


def _render(name: str, **ctx) -> str:
    return _env.get_template(name).render(**ctx)


def _empty_totals():
    """Placeholder HitTotals for when the privacy surface is called without a
    scrubber-hit store wired up (e.g. in tests or minimal daemon setups)."""
    from ghost_daemon.insights.scrubber_hits import HitTotals
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return HitTotals(by_kind={}, total=0, window_start=now, window_end=now)


def _flow_snapshot(cfg, *, days: int):
    """Compute a FlowSnapshot from the dev's local sources.

    Lives in a helper so both the HTML route and the JSON endpoint can share
    it. Takes a conservative default-days clamp so an out-of-range query
    parameter can't make the scan huge.
    """
    from ghost_daemon.insights import FlowAnalyzer
    days = max(1, min(int(days), 30))
    analyzer = FlowAnalyzer(
        session_dirs=list(cfg.resolved_session_dirs),
        sqlite_path=cfg.sqlite_path,
    )
    return analyzer.snapshot(days=days)


def _edit_rate_trend(store, edits, *, weeks: int) -> dict:
    """Per-artifact-type rolling weekly edit rates for the last ``weeks`` weeks.

    Returns::

        {
          "standup": {
            "points": [{"week_start": "...", "rate": 0.42, "approvals": 7, "edits": 3}, ...],
            "latest": 0.42,
            "previous": 0.55,
            "delta_pct_points": -13.0,
            "direction": "down" | "up" | "flat" | "unknown"
          },
          ...
        }

    ``rate`` is None when there were no approvals in that week (so the chart
    can leave a gap rather than drawing zero). ``direction`` interprets WoW:
    down = edit rate dropped (agent matching voice better),
    up   = edit rate rose (agent got worse — or voice drifted).
    """
    if store is None or edits is None:
        return {}
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Week buckets: oldest first, each 7 days wide, ending at `now`.
    buckets: list[tuple[datetime, datetime]] = []
    for i in range(weeks, 0, -1):
        end = now - timedelta(days=(i - 1) * 7)
        start = end - timedelta(days=7)
        buckets.append((start, end))

    # Collect every artifact_type we've seen in any bucket so the output is
    # stable even when a type has zero weeks of activity.
    seen_types: set[str] = set()
    per_week_approvals: list[dict[str, int]] = []
    per_week_edits: list[dict[str, int]] = []
    for start, end in buckets:
        a = store.count_approved_by_type_in_window(start, end)
        e = edits.edits_by_type_in_window(start, end)
        per_week_approvals.append(a)
        per_week_edits.append(e)
        seen_types.update(a)
        seen_types.update(e)

    trend: dict[str, dict] = {}
    for artifact_type in sorted(seen_types):
        points: list[dict] = []
        for (start, end), a_map, e_map in zip(buckets, per_week_approvals, per_week_edits):
            a = a_map.get(artifact_type, 0)
            e = e_map.get(artifact_type, 0)
            rate = (e / a) if a else None
            points.append({
                "week_start": start.date().isoformat(),
                "approvals": a,
                "edits": e,
                "rate": rate,
            })

        rates = [p["rate"] for p in points if p["rate"] is not None]
        latest = points[-1]["rate"]
        previous = points[-2]["rate"] if len(points) >= 2 else None
        direction = "unknown"
        delta_pp: float | None = None
        if latest is not None and previous is not None:
            delta = (latest - previous) * 100.0  # in percentage points
            delta_pp = round(delta, 1)
            if abs(delta) < 3.0:
                direction = "flat"
            elif delta < 0:
                direction = "down"   # edit rate dropped — agent improving
            else:
                direction = "up"     # edit rate rose — agent regressing

        trend[artifact_type] = {
            "points": points,
            "latest": latest,
            "previous": previous,
            "delta_pct_points": delta_pp,
            "direction": direction,
            "sparkline_svg": _sparkline_svg(rates),
        }
    return trend


def _sparkline_svg(rates: list[float], *, width: int = 80, height: int = 22) -> str:
    """Render a tiny inline SVG sparkline for a sequence of rates (0.0–1.0).

    Returns an empty string if there aren't enough points to draw a line.
    """
    if len(rates) < 2:
        return ""
    # Map rate in [0,1] to y in [height-2, 2] (invert so higher = worse = top).
    n = len(rates)
    step = width / (n - 1)
    pts: list[str] = []
    for i, r in enumerate(rates):
        x = i * step
        y = 2 + (height - 4) * r  # r=0 near top looks wrong — flip so low rate = low on chart
        # Actually: lower rate = better = should visually look LOWER on the chart.
        # Invert: y = (height - 2) - (height - 4) * r
        y = (height - 2) - (height - 4) * r
        pts.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(pts)
    stroke = "currentColor"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{polyline}"/>'
        f'</svg>'
    )


def _edit_rate_rows(store, edits, *, days: int) -> list[dict]:
    """Build rolling edit-rate rows for the /privacy page.

    rate = edits / approvals, per artifact_type, in the last ``days`` days.
    When few-shot is working, this number should trend downward.
    Returns an empty list if either store is missing (keeps the template simple).
    """
    if store is None or edits is None:
        return []
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    approvals = store.count_approved_by_type_since(since)
    edit_counts = edits.edits_by_type_since(since)
    rows: list[dict] = []
    for artifact_type in sorted(set(approvals) | set(edit_counts)):
        a = approvals.get(artifact_type, 0)
        e = edit_counts.get(artifact_type, 0)
        rate = (e / a) if a else None
        rows.append({
            "artifact_type": artifact_type,
            "approvals": a,
            "edits": e,
            "rate": rate,
        })
    return rows


def build_app(
    store: SQLiteStore,
    gate: ApprovalGate,
    audit_log: AuditLog,
    subscriber_registry: "SubscriberRegistry",
    cfg: "Config",
    qdrant: QdrantStore | None = None,
    llm: "LLMProvider | None" = None,
    pairing_status: "PairingStatus | None" = None,
    scrubber_hits: "ScrubberHitStore | None" = None,
    edits: "EditDiffStore | None" = None,
    collisions: "CollisionPoller | None" = None,
) -> FastAPI:
    app = FastAPI(title="Vantage", docs_url=None, redoc_url=None)
    install_local_guard(app, cfg.ui.bind_address)

    @app.exception_handler(Exception)
    async def _debug_handler(request: Request, exc: Exception):
        # Log the full traceback for operators; show the user only a short
        # error id + human-readable message. Stack traces leak file paths
        # and sometimes session content — we keep them in the log, not the
        # response body.
        import secrets as _secrets
        err_id = _secrets.token_hex(4)
        logger.exception("Unhandled error [%s] on %s", err_id, request.url)
        return HTMLResponse(
            f"""<div style="font-family:system-ui,-apple-system,sans-serif;max-width:40rem;margin:4rem auto;padding:2rem;background:#121216;border:1px solid #27272e;border-radius:10px;color:#ededf0">
<div style="font-size:1.1rem;font-weight:600;margin-bottom:0.5rem">Something went wrong.</div>
<div style="color:#a0a0a8;line-height:1.55;font-size:0.9rem">The daemon hit an error handling this request. Details are in your local Vantage log; no data was sent anywhere.</div>
<div style="color:#6c6c76;font-family:'Geist Mono',monospace;font-size:0.78rem;margin-top:1rem">error id: {err_id}</div>
<div style="margin-top:1.25rem"><a href="/" style="color:#c4b5fd;text-decoration:none;font-size:0.9rem">← Back to drafts</a></div>
</div>""",
            status_code=500,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        """Prometheus scrape endpoint. Same contract as the replica — a
        single process-wide registry exposed over plain text."""
        from fastapi.responses import Response
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        # Touch the ghost metrics module so its series are registered even
        # if no code path has imported it yet. Idempotent.
        import ghost_daemon.metrics  # noqa: F401
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        pending = gate.list_pending()
        recent = gate.list_recent(limit=10)
        return HTMLResponse(_render("index.html", request=request, drafts=pending, recent=recent))

    @app.get("/draft/{draft_id}", response_class=HTMLResponse)
    async def view_draft(request: Request, draft_id: str):
        row = store.get_draft(draft_id)
        if not row:
            return HTMLResponse("<p>Draft not found.</p>", status_code=404)
        payload = json.loads(row.edited_payload_json or row.payload_json)
        return HTMLResponse(_render(
            "draft.html",
            request=request,
            draft_id=draft_id,
            artifact_type=row.artifact_type,
            period_start=row.period_start,
            period_end=row.period_end,
            payload=payload,
            payload_json=json.dumps(payload, indent=2),
            status=row.status,
        ))

    @app.get("/draft/{draft_id}/xray", response_class=HTMLResponse)
    async def view_draft_xray(request: Request, draft_id: str):
        row = store.get_draft(draft_id)
        if not row:
            return HTMLResponse("<p>Draft not found.</p>", status_code=404)
        import html as _html
        import re
        payload = json.loads(row.edited_payload_json or row.payload_json)
        payload_json = json.dumps(payload, indent=2)
        token_re = re.compile(r"\[REDACTED:([a-zA-Z0-9_\-]+):([a-zA-Z0-9]+)\]")
        tokens = token_re.findall(payload_json)
        kind_counts: dict[str, int] = {}
        for kind, _fp in tokens:
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

        escaped = _html.escape(payload_json)

        def _hl_scrubbed(match):
            kind = match.group(1)
            fp = match.group(2)
            # Wrap the token in a styled chip with the pulsing lock badge.
            return (
                f'<span class="xr-tok" data-kind="{kind}" '
                f'title="{kind} · fingerprint {fp}">'
                f'<svg class="xr-lock" viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">'
                f'<path d="M6 10V7a6 6 0 0 1 12 0v3" fill="none" stroke="currentColor" stroke-width="2"/>'
                f'<rect x="4" y="10" width="16" height="11" rx="2" fill="currentColor" opacity=".18"/>'
                f'<rect x="4" y="10" width="16" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="2"/>'
                f'<circle cx="12" cy="15.5" r="1.6" fill="currentColor"/>'
                f'</svg>'
                f'<span class="xr-tok-kind">{kind}</span>'
                f'<span class="xr-tok-fp">{fp}</span>'
                f'</span>'
            )

        def _hl_raw(match):
            kind = match.group(1)
            shape = _html.escape(_raw_shape_for(kind))
            return (
                f'<span class="xr-raw" data-kind="{kind}" '
                f'title="This is where the original secret lived — never persisted">'
                f'{shape}</span>'
            )

        # Escape JSON once, then substitute the (now-escaped) token pattern.
        escaped_token_re = re.compile(r"\[REDACTED:([a-zA-Z0-9_\-]+):([a-zA-Z0-9]+)\]")
        scrubbed_html = escaped_token_re.sub(_hl_scrubbed, escaped)
        raw_html = escaped_token_re.sub(_hl_raw, escaped)

        turn_count = 0
        if isinstance(payload, dict):
            for key in ("highlights", "today", "yesterday", "blockers", "files_touched"):
                v = payload.get(key)
                if isinstance(v, list):
                    turn_count += len(v)
        return HTMLResponse(_render(
            "xray.html",
            request=request,
            draft_id=draft_id,
            artifact_type=row.artifact_type,
            status=row.status,
            payload_json=payload_json,
            scrubbed_html=scrubbed_html,
            raw_html=raw_html,
            tokens=len(tokens),
            kind_counts=sorted(kind_counts.items(), key=lambda x: -x[1]),
            payload_bytes=len(payload_json),
            turn_count=turn_count,
        ))

    @app.post("/draft/{draft_id}/approve")
    async def approve_draft(draft_id: str, edited_json: str = Form(default="")):
        edited = json.loads(edited_json) if edited_json.strip() else None
        try:
            approved = gate.approve(draft_id, edited_payload=edited, source=ApprovalSource.MANUAL)
            asyncio.create_task(_push_to_subscribers(approved, approved.artifact_type))
            if qdrant is not None and llm is not None:
                asyncio.create_task(_index_in_qdrant(approved))
        except Exception as exc:
            logger.error("Approve failed: %s", exc)
        return RedirectResponse("/", status_code=303)

    @app.post("/draft/{draft_id}/reject")
    async def reject_draft(draft_id: str):
        try:
            gate.reject(draft_id)
        except Exception as exc:
            logger.error("Reject failed: %s", exc)
        return RedirectResponse("/", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    async def audit(request: Request, days: int = 7):
        entries = audit_log.read_recent(days=days)
        return HTMLResponse(_render("audit.html", request=request, entries=entries, days=days))

    # ── Privacy (dev-only insights) ────────────────────────────────────────────

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy(request: Request):
        totals = scrubber_hits.weekly_totals() if scrubber_hits else None
        recent_edits = edits.read_recent(limit=20) if edits else []
        edit_count = edits.count_in_shard() if edits else 0
        rate_rows = _edit_rate_rows(store, edits, days=30)
        trend = _edit_rate_trend(store, edits, weeks=8)
        return HTMLResponse(_render(
            "privacy.html",
            request=request,
            totals=totals or _empty_totals(),
            edits=recent_edits,
            edit_count=edit_count,
            rate_rows=rate_rows,
            trend=trend,
        ))

    @app.get("/api/privacy/edit-rate")
    async def api_edit_rate(days: int = 30):
        rate_rows = _edit_rate_rows(store, edits, days=days)
        return JSONResponse({"days": days, "rows": rate_rows})

    @app.get("/api/privacy/edit-rate-trend")
    async def api_edit_rate_trend(weeks: int = 8):
        weeks = max(2, min(int(weeks), 26))
        trend = _edit_rate_trend(store, edits, weeks=weeks)
        return JSONResponse({"weeks": weeks, "trend": trend})

    # ── Flow-state (dev-only rear-view mirror) ─────────────────────────────────

    @app.get("/flow", response_class=HTMLResponse)
    async def flow(request: Request, days: int = 7):
        snap = _flow_snapshot(cfg, days=days)
        return HTMLResponse(_render(
            "flow.html",
            request=request,
            snap=snap,
            days=days,
        ))

    @app.get("/api/collisions")
    async def api_collisions(force: bool = False):
        """Dev-facing collision heads-up.

        Aggregates recent collisions reported by every manager the dev
        pushes to. Cached by the poller so this endpoint is cheap to poll
        from the browser.
        """
        if collisions is None:
            return JSONResponse({"count": 0, "collisions": [], "errors": []})
        bundle = await collisions.get(force=bool(force))
        return JSONResponse({
            "fetched_at": bundle.fetched_at,
            "count": bundle.count,
            "collisions": [
                {
                    "collision_id": c.collision_id,
                    "other_developer": c.other_developer,
                    "other_developer_name": c.other_developer_name,
                    "files": c.files,
                    "kind": c.kind,
                    "score": c.score,
                    "detected_at": c.detected_at,
                    "source_subscriber": c.source_subscriber,
                }
                for c in bundle.collisions
            ],
            "errors": bundle.errors,
        })

    @app.get("/api/flow")
    async def api_flow(days: int = 7):
        snap = _flow_snapshot(cfg, days=days)
        return JSONResponse({
            "days": snap.days,
            "total_turns": snap.total_turns,
            "active_hours": snap.active_hours,
            "total_stuck_attempts": snap.total_stuck_attempts,
            "heatmap": [
                {"day_offset": c.day_offset, "hour": c.hour, "turn_count": c.turn_count}
                for c in snap.heatmap
            ],
            "deep_windows": [
                {
                    "start": w.start.isoformat(),
                    "end": w.end.isoformat(),
                    "turn_count": w.turn_count,
                    "duration_minutes": w.duration_minutes,
                }
                for w in snap.deep_windows
            ],
            "recent_approvals": [
                {
                    "draft_id": e.draft_id,
                    "artifact_type": e.artifact_type,
                    "reviewed_at": e.reviewed_at.isoformat(),
                    "status": e.status,
                }
                for e in snap.recent_approvals
            ],
            "stuck_patterns": [
                {
                    "resolution_id": p.resolution_id,
                    "error_signature": p.error_signature,
                    "fix_summary": p.fix_summary,
                    "attempt_count": p.attempt_count,
                    "files_touched": p.files_touched,
                    "created_at": p.created_at.isoformat(),
                }
                for p in snap.stuck_patterns
            ],
        })

    @app.get("/api/privacy/totals")
    async def api_privacy_totals():
        totals = scrubber_hits.weekly_totals() if scrubber_hits else _empty_totals()
        return JSONResponse({
            "by_kind": totals.by_kind,
            "total": totals.total,
            "window_start": totals.window_start,
            "window_end": totals.window_end,
        })

    @app.post("/api/privacy/override")
    async def api_privacy_override(request: Request):
        """Record a dev-reported false positive.

        Request body: ``{"pattern": "<string>", "kind": "<redaction_kind>"}``.
        Returns whether the current scrubber actually redacts the pattern
        (so the UI can confirm the regression target is real).
        """
        if scrubber_hits is None:
            return JSONResponse({"error": "override surface unavailable"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        pattern = (body.get("pattern") or "").strip()
        kind = (body.get("kind") or "").strip()
        if not pattern:
            return JSONResponse({"error": "pattern is required"}, status_code=400)
        if not kind:
            return JSONResponse({"error": "kind is required"}, status_code=400)
        if len(pattern) > 500:
            return JSONResponse({"error": "pattern too long (max 500 chars)"}, status_code=400)

        # Confirm the current scrubber *would* redact this pattern — otherwise
        # the dev is reporting against a kind that already doesn't match, and
        # the override is a no-op.
        from ghost_daemon.redaction.scrubber import scrub
        ctx = scrub(pattern)
        still_redacted = any(r.kind == kind for r in ctx.redactions)

        scrubber_hits.record_override(pattern, kind)
        return JSONResponse({
            "recorded": True,
            "still_redacted_by_current_scrubber": still_redacted,
            "kind": kind,
        })

    @app.get("/api/privacy/overrides")
    async def api_privacy_overrides(limit: int = 50):
        if scrubber_hits is None:
            return JSONResponse({"overrides": []})
        rows = scrubber_hits.recent_overrides(limit=limit)
        return JSONResponse({
            "overrides": [
                {"pattern": p, "kind": k, "overridden_at": at}
                for p, k, at in rows
            ],
        })

    # ── Subscribers ────────────────────────────────────────────────────────────

    @app.get("/subscribers", response_class=HTMLResponse)
    async def subscribers_page(request: Request):
        rows = subscriber_registry.list_all()
        subs = [subscriber_registry.to_identity(r) for r in rows]
        return HTMLResponse(_render("subscribers.html", request=request, subscribers=subs))

    @app.post("/subscribers/{subscriber_id}/revoke")
    async def revoke_subscriber(subscriber_id: str):
        subscriber_registry.revoke(subscriber_id)
        return RedirectResponse("/subscribers", status_code=303)

    @app.get("/api/subscribers")
    async def api_subscribers():
        rows = subscriber_registry.list_all()
        return JSONResponse([
            {
                "subscriber_id": r.id,
                "name": r.name,
                "receive_url": r.receive_url,
                "channels": json.loads(r.channels_json),
                "revoked": r.revoked_at is not None,
            }
            for r in rows
        ])

    @app.get("/api/pairing-status")
    async def api_pairing_status():
        """Live WS connection state for each subscriber with live_probe enabled.

        Powers the connection pill in the local UI header. Returns an empty
        list when no subscribers have live_probe enabled (nothing to connect).
        """
        if pairing_status is None:
            return JSONResponse([])
        snap = pairing_status.snapshot()
        total = len(snap)
        connected = sum(1 for s in snap if s["connected"])
        return JSONResponse({
            "total": total,
            "connected": connected,
            "subscribers": snap,
        })

    @app.get("/api/pending-drafts")
    async def api_pending_drafts():
        """Lightweight JSON view of the pending queue — used by keyboard nav JS."""
        pending = gate.list_pending()  # list of dicts, not models
        return JSONResponse([
            {
                "draft_id": d.get("draft_id", ""),
                "artifact_type": d.get("artifact_type", ""),
                "period_start": (d.get("period_start") or "")[:10],
                "created_at": (d.get("created_at") or "")[:16],
            }
            for d in pending
        ])

    # ── API endpoints ──────────────────────────────────────────────────────────

    @app.get("/api/history")
    async def api_history(artifact_type: str | None = None, limit: int = 20):
        """Recent approved artifacts from Qdrant for context inspection."""
        if qdrant is None:
            return JSONResponse([])
        items = await qdrant.list_approved(cfg.developer_id, artifact_type=artifact_type, limit=limit)
        return JSONResponse(items)

    @app.get("/api/search")
    async def api_search(q: str, artifact_type: str | None = None, limit: int = 10):
        """Semantic search over approved artifacts."""
        if qdrant is None or llm is None:
            return JSONResponse({"error": "Qdrant or embedder not configured"}, status_code=503)
        vec = await llm.embed(q)
        results = await qdrant.search_approved(vec, cfg.developer_id, artifact_type=artifact_type, top_k=limit)
        return JSONResponse({"query": q, "results": results})

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _index_in_qdrant(approved) -> None:
        from ghost_daemon.daemon import _artifact_text
        try:
            text = _artifact_text(approved.artifact_type, approved.payload)
            vec = await llm.embed(text)
            payload_dict = approved.payload.model_dump(mode="json") if hasattr(approved.payload, "model_dump") else {}
            await qdrant.upsert_approved(
                approved_id=approved.approval_id,
                developer_id=approved.developer_id,
                artifact_type=approved.artifact_type,
                payload=payload_dict,
                text=text,
                vector=vec,
                approved_at=approved.approved_at.isoformat(),
            )
        except Exception as exc:
            logger.debug("Qdrant index skipped: %s", exc)

    # ── Internal push helper ───────────────────────────────────────────────────

    async def _push_to_subscribers(approved, artifact_type: str):
        from ghost_daemon.transport.push import push

        channel_map = {
            "weekly_digest": Channel.WEEKLY_DIGEST,
            "commit_report": Channel.COMMIT_REPORT,
            "standup": Channel.STANDUP,
            "pr_draft": Channel.PR_DRAFT,
        }
        channel_value = channel_map.get(artifact_type, Channel.WEEKLY_DIGEST).value
        targets = subscriber_registry.subscribers_for_channel(channel_value)

        for row in targets:
            identity = subscriber_registry.to_identity(row)
            ok = await push(approved, identity, cfg.developer_id)
            if not ok:
                logger.warning("Push to %s (%s) failed", row.name, row.id)

    return app
