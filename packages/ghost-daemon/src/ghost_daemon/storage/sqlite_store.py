from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, create_engine, select


class DigestDraftRow(SQLModel, table=True):
    __tablename__ = "digest_drafts"

    id: str = Field(primary_key=True)
    developer_id: str
    artifact_type: str
    period_start: str  # ISO datetime
    period_end: str
    payload_json: str
    status: str = "pending"  # pending | approved | rejected
    edited_payload_json: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    reviewed_at: str | None = None
    approval_source: str | None = None


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(self._engine)

    def save_draft(self, draft_id: str, developer_id: str, artifact_type: str,
                   payload: Any, period_start: datetime, period_end: datetime) -> None:
        row = DigestDraftRow(
            id=draft_id,
            developer_id=developer_id,
            artifact_type=artifact_type,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            payload_json=json.dumps(payload) if not isinstance(payload, str) else payload,
        )
        with Session(self._engine) as session:
            session.add(row)
            session.commit()

    def list_pending(self) -> list[DigestDraftRow]:
        with Session(self._engine) as session:
            return list(session.exec(
                select(DigestDraftRow).where(DigestDraftRow.status == "pending")
            ).all())

    def get_draft(self, draft_id: str) -> DigestDraftRow | None:
        with Session(self._engine) as session:
            return session.get(DigestDraftRow, draft_id)

    def approve(self, draft_id: str, edited_payload: Any | None,
                approval_source: str) -> None:
        with Session(self._engine) as session:
            row = session.get(DigestDraftRow, draft_id)
            if not row:
                raise KeyError(f"Draft {draft_id!r} not found")
            row.status = "approved"
            row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            row.approval_source = approval_source
            if edited_payload is not None:
                row.edited_payload_json = (
                    json.dumps(edited_payload)
                    if not isinstance(edited_payload, str)
                    else edited_payload
                )
            session.add(row)
            session.commit()

    def list_recent(self, limit: int = 20) -> list[DigestDraftRow]:
        with Session(self._engine) as session:
            return list(session.exec(
                select(DigestDraftRow)
                .where(DigestDraftRow.status != "pending")
                .order_by(DigestDraftRow.created_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            ).all())

    def reject(self, draft_id: str) -> None:
        with Session(self._engine) as session:
            row = session.get(DigestDraftRow, draft_id)
            if not row:
                raise KeyError(f"Draft {draft_id!r} not found")
            row.status = "rejected"
            row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            session.add(row)
            session.commit()

    def count_approved_by_type_since(self, since: datetime) -> dict[str, int]:
        """Return number of approved drafts per artifact_type since ``since``.

        Used by the /privacy page to compute edit-rate = edits / approvals.
        """
        since_iso = since.isoformat()
        counts: dict[str, int] = {}
        with Session(self._engine) as session:
            rows = session.exec(
                select(DigestDraftRow)
                .where(DigestDraftRow.status == "approved")
                .where(DigestDraftRow.reviewed_at >= since_iso)  # type: ignore[operator]
            ).all()
            for r in rows:
                counts[r.artifact_type] = counts.get(r.artifact_type, 0) + 1
        return counts

    def count_approved_by_type_in_window(
        self, start: datetime, end: datetime,
    ) -> dict[str, int]:
        """Approvals per artifact_type where ``start <= reviewed_at < end``."""
        if end <= start:
            return {}
        start_iso = start.isoformat()
        end_iso = end.isoformat()
        counts: dict[str, int] = {}
        with Session(self._engine) as session:
            rows = session.exec(
                select(DigestDraftRow)
                .where(DigestDraftRow.status == "approved")
                .where(DigestDraftRow.reviewed_at >= start_iso)  # type: ignore[operator]
                .where(DigestDraftRow.reviewed_at < end_iso)  # type: ignore[operator]
            ).all()
            for r in rows:
                counts[r.artifact_type] = counts.get(r.artifact_type, 0) + 1
        return counts
