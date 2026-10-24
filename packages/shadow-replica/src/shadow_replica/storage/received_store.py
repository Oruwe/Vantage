from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select


class ReceivedArtifactRow(SQLModel, table=True):
    __tablename__ = "received_artifacts"

    approval_id: str = Field(primary_key=True)
    developer_id: str
    artifact_type: str
    draft_id: str
    channel: str
    payload_json: str
    approved_at: str
    received_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ReceivedStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(self._engine)

    def save(
        self,
        approval_id: str,
        developer_id: str,
        artifact_type: str,
        draft_id: str,
        channel: str,
        payload_json: str,
        approved_at: str,
    ) -> None:
        row = ReceivedArtifactRow(
            approval_id=approval_id,
            developer_id=developer_id,
            artifact_type=artifact_type,
            draft_id=draft_id,
            channel=channel,
            payload_json=payload_json,
            approved_at=approved_at,
        )
        with Session(self._engine) as session:
            existing = session.get(ReceivedArtifactRow, approval_id)
            if existing:
                return
            session.add(row)
            session.commit()

    def list_for_developer(
        self,
        developer_id: str,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[ReceivedArtifactRow]:
        with Session(self._engine) as session:
            q = select(ReceivedArtifactRow).where(
                ReceivedArtifactRow.developer_id == developer_id
            )
            if channel:
                q = q.where(ReceivedArtifactRow.channel == channel)
            q = q.order_by(ReceivedArtifactRow.received_at.desc()).limit(limit)
            return list(session.exec(q).all())

    def list_all(
        self,
        channel: str | None = None,
        limit: int = 100,
    ) -> list[ReceivedArtifactRow]:
        """Return recent artifacts across all developers (for team dashboard)."""
        with Session(self._engine) as session:
            q = select(ReceivedArtifactRow)
            if channel:
                q = q.where(ReceivedArtifactRow.channel == channel)
            q = q.order_by(ReceivedArtifactRow.received_at.desc()).limit(limit)
            return list(session.exec(q).all())

    def list_developers(self) -> list[str]:
        """Return distinct developer IDs that have sent at least one artifact."""
        from sqlmodel import text
        with Session(self._engine) as session:
            result = session.exec(
                text("SELECT DISTINCT developer_id FROM received_artifacts ORDER BY developer_id")
            )
            return [row[0] for row in result]
