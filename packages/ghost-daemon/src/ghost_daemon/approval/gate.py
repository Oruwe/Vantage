from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from vantage.schemas import (
    ApprovalSource, ApprovedPayload, BugResolution, CommitReport, DraftPayload,
    PRDraft, QuickNote, StandupBullets, WeeklyDigest,
)
from ghost_daemon.insights.edit_store import EditDiffStore, compute_diff
from ghost_daemon.redaction.scrubber import scrub_payload
from ghost_daemon.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class ApprovalGate:
    """The single point where DraftPayload becomes ApprovedPayload.

    All outbound transport functions accept only ApprovedPayload.
    ApprovedPayload may only be constructed here — not by callers directly.
    """

    def __init__(
        self,
        store: SQLiteStore,
        developer_id: str,
        edits: EditDiffStore | None = None,
    ) -> None:
        self._store = store
        self._developer_id = developer_id
        self._edits = edits

    def submit(self, draft: DraftPayload) -> str:
        """Persist a draft for manual or policy review. Returns draft_id."""
        payload_json = draft.payload.model_dump_json() if hasattr(draft.payload, "model_dump_json") else json.dumps(draft.payload)
        period_start = getattr(draft.payload, "period_start", datetime.now(timezone.utc).replace(tzinfo=None))
        period_end = getattr(draft.payload, "period_end", datetime.now(timezone.utc).replace(tzinfo=None))
        self._store.save_draft(
            draft_id=draft.draft_id,
            developer_id=self._developer_id,
            artifact_type=draft.artifact_type,
            payload=payload_json,
            period_start=period_start,
            period_end=period_end,
        )
        logger.info("Draft %s submitted for review (type=%s)", draft.draft_id, draft.artifact_type)
        return draft.draft_id

    def approve(
        self,
        draft_id: str,
        edited_payload: Any | None = None,
        source: ApprovalSource = ApprovalSource.MANUAL,
    ) -> ApprovedPayload:
        """Approve a pending draft and return a typed ApprovedPayload.

        Scrubbing happens first — before the DB write, before the edit-diff
        capture, before the ApprovedPayload is constructed. Any secret shape
        the developer pasted into an edit (or an LLM hallucinated into the
        original draft) is placeholder'd *everywhere* it could leak from:
        the SQLite row read by ``get_approved()`` + ``vantage-ghost share``,
        the JSONL edit-diff shard read by the few-shot selector, and the
        ApprovedPayload returned to the caller.
        """
        row = self._store.get_draft(draft_id)
        if row is None:
            raise KeyError(f"Draft {draft_id!r} not found")
        if row.status == "approved":
            raise ValueError(f"Draft {draft_id!r} is already approved")

        # Parse both sides up front. The original is always a JSON string
        # (serialised in submit()); the edit may be a dict, model, or string.
        original_data = json.loads(row.payload_json)
        edited_data: Any | None = None
        if edited_payload is not None:
            if isinstance(edited_payload, str):
                edited_data = json.loads(edited_payload)
            elif hasattr(edited_payload, "model_dump"):
                edited_data = edited_payload.model_dump(mode="json")
            else:
                edited_data = edited_payload

        # SECURITY: scrub BOTH sides before anything downstream sees them.
        # If we scrubbed only on return, the SQLite row and the edit-diff
        # shard would retain the raw secret, and get_approved() + the
        # few-shot selector would later surface it to the manager and LLM.
        scrubbed_original, orig_hits = scrub_payload(original_data)
        if edited_data is not None:
            scrubbed_edited, edit_hits = scrub_payload(edited_data)
        else:
            scrubbed_edited, edit_hits = None, []

        all_hits = list(orig_hits) + list(edit_hits)
        if all_hits:
            logger.warning(
                "Approval scrubber caught %d secret(s) in draft %s: %s",
                len(all_hits),
                draft_id,
                sorted({r.kind for r in all_hits}),
            )

        # Persist the scrubbed edited payload, not the raw one.
        store_payload = (
            json.dumps(scrubbed_edited) if scrubbed_edited is not None else None
        )
        self._store.approve(draft_id, store_payload, source.value)

        final_data = scrubbed_edited if scrubbed_edited is not None else scrubbed_original

        # Record the edit-diff from scrubbed inputs on both sides. Never
        # fails loud — a broken insight write must not block approval.
        if self._edits is not None and edited_data is not None:
            try:
                diff = compute_diff(
                    draft_id=draft_id,
                    developer_id=self._developer_id,
                    artifact_type=row.artifact_type,
                    original=scrubbed_original,
                    edited=final_data,
                )
                if diff is not None:
                    self._edits.record(diff)
            except Exception as exc:
                logger.debug("edit-diff capture skipped: %s", exc)

        payload = _deserialise(row.artifact_type, final_data)

        return ApprovedPayload(
            draft_id=draft_id,
            artifact_type=row.artifact_type,
            payload=payload,
            approval_source=source,
            developer_id=self._developer_id,
        )

    def reject(self, draft_id: str) -> None:
        self._store.reject(draft_id)
        logger.info("Draft %s rejected", draft_id)

    def get_approved(self, draft_id: str) -> ApprovedPayload:
        """Return an ApprovedPayload for an already-approved draft (e.g. for share/re-push).

        Re-scrubs on read as defense-in-depth: rows persisted by earlier
        builds (before scrubbing moved to the top of approve()) still get
        cleaned before leaving the gate.
        """
        row = self._store.get_draft(draft_id)
        if row is None:
            raise KeyError(f"Draft {draft_id!r} not found")
        if row.status != "approved":
            raise ValueError(f"Draft {draft_id!r} is not approved (status={row.status!r})")
        final_data = json.loads(row.edited_payload_json or row.payload_json)
        final_data, hits = scrub_payload(final_data)
        if hits:
            logger.warning(
                "get_approved re-scrubbed %d legacy secret(s) in draft %s: %s",
                len(hits), draft_id, sorted({r.kind for r in hits}),
            )
        payload = _deserialise(row.artifact_type, final_data)
        return ApprovedPayload(
            draft_id=draft_id,
            artifact_type=row.artifact_type,
            payload=payload,
            approval_source=ApprovalSource(row.approval_source or "manual"),
            developer_id=self._developer_id,
        )

    def list_pending(self) -> list[dict]:
        rows = self._store.list_pending()
        return [_row_to_dict(r) for r in rows]

    def list_recent(self, limit: int = 20) -> list[dict]:
        rows = self._store.list_recent(limit)
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(r) -> dict:
    return {
        "draft_id": r.id,
        "artifact_type": r.artifact_type,
        "created_at": r.created_at,
        "period_start": r.period_start,
        "period_end": r.period_end,
        "status": r.status,
        "reviewed_at": r.reviewed_at,
    }


def _deserialise(artifact_type: str, data: dict) -> Any:
    if artifact_type == "weekly_digest":
        return WeeklyDigest(**data)
    if artifact_type == "commit_report":
        return CommitReport(**data)
    if artifact_type == "standup":
        return StandupBullets(**data)
    if artifact_type == "pr_draft":
        return PRDraft(**data)
    if artifact_type == "quick_note":
        return QuickNote(**data)
    if artifact_type == "bug_resolution":
        return BugResolution(**data)
    return data
