"""SQLite-backed conversation memory for the manager agent.

Scopes conversations per-manager via a stable hash of the `vsm_...` bearer
token. Enforces caps: 50 turns per conversation, 10 conversations per manager
(auto-prune oldest). The schema is deliberately small — this is a session
memory, not a knowledge base.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from vantage.schemas import ChatConversation, ChatTurn

_MAX_TURNS_PER_CONVO = 50
_MAX_CONVOS_PER_MANAGER = 10


def hash_manager_token(token: str) -> str:
    """Stable, non-reversible identifier for a manager token.

    Used as the partitioning key so one manager can never read another's thread
    even if conversation IDs collide. We never store the raw token.
    """
    return hashlib.sha256(("vantage-manager:" + (token or "")).encode("utf-8")).hexdigest()[:32]


class ChatMemory:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    manager_token_hash TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_turn_at TEXT NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_conv_mgr ON conversations(manager_token_hash, last_turn_at DESC);

                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    tool_call_id TEXT NOT NULL DEFAULT '',
                    tool_input_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_turns_convo ON conversation_turns(conversation_id, seq);
                """
            )

    # ── Conversation lifecycle ────────────────────────────────────────────────

    def get_or_create(
        self,
        manager_token_hash: str,
        conversation_id: str | None = None,
        title_seed: str = "",
    ) -> ChatConversation:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            if conversation_id:
                row = c.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ? AND manager_token_hash = ?",
                    (conversation_id, manager_token_hash),
                ).fetchone()
                if row is not None:
                    return _row_to_convo(row)
            convo = ChatConversation(
                manager_token_hash=manager_token_hash,
                title=_title_from_seed(title_seed),
            )
            c.execute(
                "INSERT INTO conversations(conversation_id, manager_token_hash, title, created_at, last_turn_at, turn_count) "
                "VALUES(?, ?, ?, ?, ?, 0)",
                (
                    convo.conversation_id,
                    convo.manager_token_hash,
                    convo.title,
                    now,
                    now,
                ),
            )
            self._prune_old_conversations(c, manager_token_hash)
            return convo

    def list_conversations(self, manager_token_hash: str, limit: int = 20) -> list[ChatConversation]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM conversations WHERE manager_token_hash = ? "
                "ORDER BY last_turn_at DESC LIMIT ?",
                (manager_token_hash, max(1, min(limit, 100))),
            ).fetchall()
            return [_row_to_convo(r) for r in rows]

    def delete_conversation(self, manager_token_hash: str, conversation_id: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM conversations WHERE conversation_id = ? AND manager_token_hash = ?",
                (conversation_id, manager_token_hash),
            )
            return cur.rowcount > 0

    # ── Turns ─────────────────────────────────────────────────────────────────

    def append_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_name: str = "",
        tool_call_id: str = "",
        tool_input_json: str = "",
    ) -> ChatTurn:
        now = datetime.now(timezone.utc).isoformat()
        turn = ChatTurn(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_input_json=tool_input_json,
        )
        with self._lock, self._conn() as c:
            seq_row = c.execute(
                "SELECT COALESCE(MAX(seq), 0) AS mx FROM conversation_turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            seq = int(seq_row["mx"]) + 1 if seq_row else 1

            c.execute(
                "INSERT INTO conversation_turns(turn_id, conversation_id, role, content, tool_name, tool_call_id, tool_input_json, created_at, seq) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn.turn_id,
                    conversation_id,
                    role,
                    content,
                    tool_name,
                    tool_call_id,
                    tool_input_json,
                    now,
                    seq,
                ),
            )
            c.execute(
                "UPDATE conversations SET last_turn_at = ?, turn_count = turn_count + 1 WHERE conversation_id = ?",
                (now, conversation_id),
            )
            self._prune_old_turns(c, conversation_id)
        return turn

    def list_turns(self, conversation_id: str, limit: int | None = None) -> list[ChatTurn]:
        with self._lock, self._conn() as c:
            q = "SELECT * FROM conversation_turns WHERE conversation_id = ? ORDER BY seq ASC"
            params: tuple = (conversation_id,)
            if limit is not None:
                q += " LIMIT ?"
                params = (conversation_id, max(1, limit))
            rows = c.execute(q, params).fetchall()
            return [_row_to_turn(r) for r in rows]

    def update_title_if_blank(self, conversation_id: str, title: str) -> None:
        if not title:
            return
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE conversations SET title = ? WHERE conversation_id = ? AND (title = '' OR title IS NULL)",
                (_title_from_seed(title), conversation_id),
            )

    # ── Pruning ───────────────────────────────────────────────────────────────

    def _prune_old_conversations(self, c: sqlite3.Connection, manager_token_hash: str) -> None:
        rows = c.execute(
            "SELECT conversation_id FROM conversations WHERE manager_token_hash = ? "
            "ORDER BY last_turn_at DESC",
            (manager_token_hash,),
        ).fetchall()
        if len(rows) <= _MAX_CONVOS_PER_MANAGER:
            return
        kill = [r["conversation_id"] for r in rows[_MAX_CONVOS_PER_MANAGER:]]
        c.executemany(
            "DELETE FROM conversations WHERE conversation_id = ?",
            [(cid,) for cid in kill],
        )

    def _prune_old_turns(self, c: sqlite3.Connection, conversation_id: str) -> None:
        rows = c.execute(
            "SELECT turn_id, seq FROM conversation_turns WHERE conversation_id = ? ORDER BY seq ASC",
            (conversation_id,),
        ).fetchall()
        if len(rows) <= _MAX_TURNS_PER_CONVO:
            return
        kill = [r["turn_id"] for r in rows[: len(rows) - _MAX_TURNS_PER_CONVO]]
        c.executemany(
            "DELETE FROM conversation_turns WHERE turn_id = ?",
            [(tid,) for tid in kill],
        )
        c.execute(
            "UPDATE conversations SET turn_count = (SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?) "
            "WHERE conversation_id = ?",
            (conversation_id, conversation_id),
        )


def _row_to_convo(row: sqlite3.Row) -> ChatConversation:
    return ChatConversation(
        conversation_id=row["conversation_id"],
        manager_token_hash=row["manager_token_hash"],
        title=row["title"] or "",
        created_at=_parse_iso(row["created_at"]),
        last_turn_at=_parse_iso(row["last_turn_at"]),
        turn_count=int(row["turn_count"] or 0),
    )


def _row_to_turn(row: sqlite3.Row) -> ChatTurn:
    return ChatTurn(
        turn_id=row["turn_id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"] or "",
        tool_name=row["tool_name"] or "",
        tool_call_id=row["tool_call_id"] or "",
        tool_input_json=row["tool_input_json"] or "",
        created_at=_parse_iso(row["created_at"]),
    )


def _parse_iso(s: str) -> datetime:
    try:
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def _title_from_seed(seed: str) -> str:
    s = (seed or "").strip().splitlines()[0] if seed else ""
    return s[:80]
