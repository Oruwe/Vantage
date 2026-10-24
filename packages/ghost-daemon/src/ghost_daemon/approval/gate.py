from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from vantage.schemas import (
    ApprovalSource, ApprovedPayload, CommitReport, DraftPayload, PRDraft,
    QuickNote, StandupBullets, WeeklyDigest,
)
from ghost_daemon.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class ApprovalGate:
    """The single point where DraftPayload becomes ApprovedPayload.

    All outbound transport functions accept only ApprovedPayload.
    ApprovedPayload may only be constructed here — not by callers directly.
    """

    def __init__(self, store: SQLiteStore, developer_id: str) -> None:
        self._store = store
        self._developer_id = developer_id

    def submit(self, draft: DraftPayload) -> str:
        """Persist a draft for manual or policy review. Returns draft_id."""
        payload_json = draft.payload.model_dump_json() if hasattr(draft.payload, "model_dump_json") else json.dumps(draft.payload)
        period_start = getattr(draft.payload, "period_start", datetime.utcnow())
        period_end = getattr(draft.payload, "period_end", datetime.utcnow())
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
        """Approve a pending draft and return a typed ApprovedPayload."""
        row = self._store.get_draft(draft_id)
        if row is None:
            raise KeyError(f"Draft {draft_id!r} not found")
        if row.status == "approved":
            raise ValueError(f"Draft {draft_id!r} is already approved")

        self._store.approve(draft_id, edited_payload, source.value)

        final_json = edited_payload if edited_payload is not None else row.payload_json
        if isinstance(final_json, str):
            final_data = json.loads(final_json)
        else:
            final_data = final_json

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
        """Return an ApprovedPayload for an already-approved draft (e.g. for share/re-push)."""
        row = self._store.get_draft(draft_id)
        if row is None:
            raise KeyError(f"Draft {draft_id!r} not found")
        if row.status != "approved":
            raise ValueError(f"Draft {draft_id!r} is not approved (status={row.status!r})")
        final_data = json.loads(row.edited_payload_json or row.payload_json)
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
    return data
