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
    from ghost_daemon.models.registry import LLMProvider
    from ghost_daemon.subscribers.registry import SubscriberRegistry

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
