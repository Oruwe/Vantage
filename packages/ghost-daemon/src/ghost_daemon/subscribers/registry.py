from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

from vantage.schemas import Channel, SubscriberIdentity

_TOKEN_PREFIX = "vsub_"


class SubscriberRow(SQLModel, table=True):
    __tablename__ = "subscribers"

    id: str = Field(primary_key=True)
    name: str
    receive_url: str
    channels_json: str  # JSON list of Channel values
    token_value: str
    revocation_counter: int = 0
    created_at: str
    revoked_at: str | None = None


class SubscriberRegistry:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(self._engine)

    def add(self, name: str, receive_url: str, channels: list[Channel]) -> SubscriberIdentity:
        """Register a new subscriber. Returns the full identity including the generated token."""
        from vantage.schemas import SubscriberIdentity
        sub_id = f"sub_{secrets.token_hex(6)}"
        token_value = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        channels_json = json.dumps([c.value if hasattr(c, "value") else c for c in channels])
        row = SubscriberRow(
            id=sub_id,
            name=name,
            receive_url=receive_url,
            channels_json=channels_json,
            token_value=token_value,
            created_at=datetime.utcnow().isoformat(),
        )
        with Session(self._engine) as session:
            session.add(row)
            session.commit()
        return SubscriberIdentity(
            subscriber_id=sub_id,
            name=name,
            receive_url=receive_url,
            channels=channels,
            token_value=token_value,
        )

    def list_all(self) -> list[SubscriberRow]:
        with Session(self._engine) as session:
            return list(session.exec(select(SubscriberRow)).all())

    def list_active(self) -> list[SubscriberRow]:
        with Session(self._engine) as session:
            return list(session.exec(
                select(SubscriberRow).where(SubscriberRow.revoked_at == None)  # noqa: E711
            ).all())

    def get(self, subscriber_id: str) -> SubscriberRow | None:
        with Session(self._engine) as session:
            return session.get(SubscriberRow, subscriber_id)

    def get_by_name(self, name: str) -> SubscriberRow | None:
        with Session(self._engine) as session:
            active = session.exec(
                select(SubscriberRow)
                .where(SubscriberRow.name == name)
                .where(SubscriberRow.revoked_at == None)  # noqa: E711
            ).first()
            if active:
                return active
            return session.exec(
                select(SubscriberRow).where(SubscriberRow.name == name)
            ).first()

    def revoke(self, subscriber_id: str) -> bool:
        with Session(self._engine) as session:
            row = session.get(SubscriberRow, subscriber_id)
            if not row:
                return False
            row.revoked_at = datetime.utcnow().isoformat()
            row.revocation_counter += 1
            session.add(row)
            session.commit()
            return True

    def subscribers_for_channel(self, channel: str) -> list[SubscriberRow]:
        """Return active subscribers that have the given channel enabled."""
        active = self.list_active()
        result = []
        for row in active:
            channels = json.loads(row.channels_json)
            if channel in channels:
                result.append(row)
        return result

    def to_identity(self, row: SubscriberRow) -> SubscriberIdentity:
        channels = [Channel(c) for c in json.loads(row.channels_json)]
        return SubscriberIdentity(
            subscriber_id=row.id,
            name=row.name,
            receive_url=row.receive_url,
            channels=channels,
            token_value=row.token_value,
            revocation_counter=row.revocation_counter,
            created_at=datetime.fromisoformat(row.created_at),
            revoked_at=datetime.fromisoformat(row.revoked_at) if row.revoked_at else None,
        )
