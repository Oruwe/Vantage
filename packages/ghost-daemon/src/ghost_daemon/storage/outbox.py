"""Durable push outbox for retrying failed subscriber pushes.

Why this exists: the host (manager) runs the shadow-replica on their laptop.
When the laptop is off, pushes from a developer's ghost-daemon fail — and
without this outbox, those approvals never reach the replica. We persist the
EXACT serialized body of the first push, keyed by (approval_id, subscriber_id).
Retries reuse the stored bytes so the replica's approval_id dedupe keeps
working even when a retry wins the race against a very late first push.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, delete, select


# Backoff schedule (seconds) — the first entry is used after the enqueue
# happens (attempt 1 already failed). Caps at 30 minutes; once an entry is
# older than MAX_AGE it is dropped with a warning instead of retried.
_BACKOFF_SCHEDULE_S: list[int] = [30, 60, 120, 300, 600, 1200, 1800]
MAX_AGE = timedelta(days=7)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _next_try_at(attempts: int) -> str:
    """Return ISO timestamp for the next retry given how many attempts have been made."""
    idx = min(max(attempts, 1) - 1, len(_BACKOFF_SCHEDULE_S) - 1)
    delay_s = _BACKOFF_SCHEDULE_S[idx]
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay_s)).isoformat()


class PushOutboxRow(SQLModel, table=True):
    __tablename__ = "push_outbox"

    id: str = Field(primary_key=True)
    approval_id: str = Field(index=True)
    subscriber_id: str = Field(index=True)
    body_json: str       # serialized ApprovedPayload bytes — used verbatim on retry
    attempts: int = 1    # counts the initial failed send + each retry attempt
    next_try_at: str     # ISO naive-UTC; retry when now >= next_try_at
    last_error: str = ""
    created_at: str = Field(default_factory=_utcnow_iso)


class PushOutbox:
    """SQLite-backed queue of pushes that failed and should be retried.

    One row per (approval_id, subscriber_id). ``enqueue`` is idempotent for the
    same pair: a repeat call on an existing pair simply records the new error
    and bumps attempts, so the retry worker can't get fooled into running the
    exponential backoff clock from scratch after every transient failure.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        SQLModel.metadata.create_all(self._engine)

    def enqueue(
        self,
        *,
        approval_id: str,
        subscriber_id: str,
        body: bytes,
        last_error: str = "",
    ) -> str:
        """Record a failed push. Returns the outbox row id.

        If a row already exists for this (approval_id, subscriber_id) pair,
        increments attempts and reschedules — keeps the ORIGINAL body so the
        signature over the wire never shifts.
        """
        with Session(self._engine) as session:
            existing = session.exec(
                select(PushOutboxRow)
                .where(PushOutboxRow.approval_id == approval_id)
                .where(PushOutboxRow.subscriber_id == subscriber_id)
            ).first()
            if existing is not None:
                existing.attempts += 1
                existing.last_error = last_error[:500]
                existing.next_try_at = _next_try_at(existing.attempts)
                session.add(existing)
                session.commit()
                return existing.id

            row = PushOutboxRow(
                id=secrets.token_hex(8),
                approval_id=approval_id,
                subscriber_id=subscriber_id,
                body_json=body.decode("utf-8"),
                attempts=1,
                next_try_at=_next_try_at(1),
                last_error=last_error[:500],
            )
            session.add(row)
            session.commit()
            return row.id

    def list_due(self, *, now: datetime | None = None) -> list[PushOutboxRow]:
        """Return rows whose next_try_at has passed."""
        now_iso = (now or datetime.now(timezone.utc).replace(tzinfo=None)).isoformat()
        with Session(self._engine) as session:
            return list(session.exec(
                select(PushOutboxRow).where(PushOutboxRow.next_try_at <= now_iso)
            ).all())

    def list_due_for_subscriber(self, subscriber_id: str) -> list[PushOutboxRow]:
        """Return ALL rows for a subscriber, ignoring next_try_at.

        Used when a subscriber's WebSocket reconnects — if they're reachable
        now, we don't want to wait out the 30-minute backoff.
        """
        with Session(self._engine) as session:
            return list(session.exec(
                select(PushOutboxRow).where(PushOutboxRow.subscriber_id == subscriber_id)
            ).all())

    def list_all(self) -> list[PushOutboxRow]:
        with Session(self._engine) as session:
            return list(session.exec(select(PushOutboxRow)).all())

    def mark_sent(self, row_id: str) -> None:
        with Session(self._engine) as session:
            session.exec(delete(PushOutboxRow).where(PushOutboxRow.id == row_id))
            session.commit()

    def mark_failed(self, row_id: str, *, error: str = "") -> None:
        with Session(self._engine) as session:
            row = session.get(PushOutboxRow, row_id)
            if row is None:
                return
            row.attempts += 1
            row.last_error = error[:500]
            row.next_try_at = _next_try_at(row.attempts)
            session.add(row)
            session.commit()

    def drop_row(self, row_id: str) -> None:
        with Session(self._engine) as session:
            session.exec(delete(PushOutboxRow).where(PushOutboxRow.id == row_id))
            session.commit()

    def count(self) -> int:
        with Session(self._engine) as session:
            return len(list(session.exec(select(PushOutboxRow.id)).all()))

    def is_expired(self, row: PushOutboxRow) -> bool:
        """True when the row is older than MAX_AGE and should be dropped."""
        try:
            created = datetime.fromisoformat(row.created_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc).replace(tzinfo=None) - created > MAX_AGE
