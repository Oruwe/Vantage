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
    token_value: str    # stored encrypted (v1:<b64>) after first rotation/add
    revocation_counter: int = 0
    created_at: str
    revoked_at: str | None = None


class TokenHistoryRow(SQLModel, table=True):
    __tablename__ = "token_history"

    history_id: str = Field(primary_key=True)
    subscriber_id: str
    token_value: str    # encrypted
    issued_at: str
    revoked_at: str | None = None
    revocation_reason: str = "rotated"


def _enc(value: str) -> str:
    from ghost_daemon.crypto import encrypt_field
    return encrypt_field(value)


def _dec(value: str) -> str:
    from ghost_daemon.crypto import decrypt_field
    return decrypt_field(value)


class SubscriberRegistry:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(self._engine)

    def add(self, name: str, receive_url: str, channels: list[Channel]) -> SubscriberIdentity:
        """Register a new subscriber. Returns the full identity including the generated token."""
        sub_id = f"sub_{secrets.token_hex(6)}"
        token_plain = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        channels_json = json.dumps([c.value if hasattr(c, "value") else c for c in channels])
        now = datetime.utcnow().isoformat()
        row = SubscriberRow(
            id=sub_id,
            name=name,
            receive_url=receive_url,
            channels_json=channels_json,
            token_value=_enc(token_plain),
            created_at=now,
        )
        history = TokenHistoryRow(
            history_id=secrets.token_hex(8),
            subscriber_id=sub_id,
            token_value=_enc(token_plain),
            issued_at=now,
            revocation_reason="initial",
        )
        with Session(self._engine) as session:
            session.add(row)
            session.add(history)
            session.commit()
        return SubscriberIdentity(
            subscriber_id=sub_id,
            name=name,
            receive_url=receive_url,
            channels=channels,
            token_value=token_plain,
        )

    def rotate(self, subscriber_id: str) -> str | None:
        """Issue a new token, invalidate the old one. Returns the new plaintext token or None if not found."""
        with Session(self._engine) as session:
            row = session.get(SubscriberRow, subscriber_id)
            if not row or row.revoked_at:
                return None

            now = datetime.utcnow().isoformat()
            # Record old token in history
            old_history = TokenHistoryRow(
                history_id=secrets.token_hex(8),
                subscriber_id=subscriber_id,
                token_value=row.token_value,
                issued_at=row.created_at,
                revoked_at=now,
                revocation_reason="rotated",
            )
            session.add(old_history)

            # Issue new token
            new_plain = _TOKEN_PREFIX + secrets.token_urlsafe(32)
            row.token_value = _enc(new_plain)
            row.revocation_counter += 1
            session.add(row)

            # Record new token in history
            new_history = TokenHistoryRow(
                history_id=secrets.token_hex(8),
                subscriber_id=subscriber_id,
                token_value=_enc(new_plain),
                issued_at=now,
                revocation_reason="current",
            )
            session.add(new_history)
            session.commit()

        return new_plain

    def token_history(self, subscriber_id: str) -> list[TokenHistoryRow]:
        """Return all token history rows for a subscriber (newest first)."""
        with Session(self._engine) as session:
            rows = list(session.exec(
                select(TokenHistoryRow)
                .where(TokenHistoryRow.subscriber_id == subscriber_id)
                .order_by(TokenHistoryRow.issued_at.desc())
            ).all())
            return rows

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
            now = datetime.utcnow().isoformat()
            # Record final history entry
            final_history = TokenHistoryRow(
                history_id=secrets.token_hex(8),
                subscriber_id=subscriber_id,
                token_value=row.token_value,
                issued_at=row.created_at,
                revoked_at=now,
                revocation_reason="subscriber_revoked",
            )
            session.add(final_history)
            row.revoked_at = now
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
            token_value=_dec(row.token_value),
            revocation_counter=row.revocation_counter,
            created_at=datetime.fromisoformat(row.created_at),
            revoked_at=datetime.fromisoformat(row.revoked_at) if row.revoked_at else None,
        )
