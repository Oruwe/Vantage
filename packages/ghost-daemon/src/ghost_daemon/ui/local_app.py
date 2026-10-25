from __future__ import annotations

import asyncio
import json
import logging
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ghost_daemon.approval.gate import ApprovalGate
from ghost_daemon.audit.log import AuditLog
from ghost_daemon.storage.sqlite_store import SQLiteStore
from ghost_daemon.storage.qdrant_store import QdrantStore
from vantage.schemas import ApprovalSource, Channel

if TYPE_CHECKING:
    from ghost_daemon.config import Config
    from ghost_daemon.daemon import PairingStatus
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


def build_app(
    store: SQLiteStore,
    gate: ApprovalGate,
    audit_log: AuditLog,
    subscriber_registry: "SubscriberRegistry",
    cfg: "Config",
    qdrant: QdrantStore | None = None,
    llm: "LLMProvider | None" = None,
    pairing_status: "PairingStatus | None" = None,
) -> FastAPI:
    app = FastAPI(title="Vantage", docs_url=None, redoc_url=None)

    @app.exception_handler(Exception)
    async def _debug_handler(request: Request, exc: Exception):
        tb = traceback.format_exc()
        logger.error("Unhandled error on %s: %s", request.url, exc)
        return HTMLResponse(
            f"<pre style='font-family:monospace;padding:2rem;background:#111;color:#f87171'>"
            f"{tb}</pre>",
            status_code=500,
        )

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
