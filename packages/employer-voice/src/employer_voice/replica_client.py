"""Thin async HTTP client for the shadow-replica manager-agent endpoints.

Covers:
  POST /ui/api/chat           — one-shot turn, returns the final assistant text
  POST /ui/api/chat/stream    — SSE round-by-round stream
  GET  /ui/api/chat/conversations
  GET  /ui/api/chat/conversations/{id}

All methods expect a manager token (``vsm_...``) and raise on non-2xx.
SSE events are yielded as typed dicts of the form ``{"event": name, ...payload}``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx


class ReplicaError(RuntimeError):
    """Raised when the replica returns a non-2xx response."""


@dataclass
class ChatResult:
    conversation_id: str
    assistant_message: str
    tool_calls: list[dict]
    stop_reason: str
    loops: int
    model: str
    budget_exhausted: bool

    @classmethod
    def from_json(cls, data: dict) -> "ChatResult":
        return cls(
            conversation_id=data.get("conversation_id", ""),
            assistant_message=data.get("assistant_message", ""),
            tool_calls=list(data.get("tool_calls", [])),
            stop_reason=data.get("stop_reason", ""),
            loops=int(data.get("loops", 0)),
            model=data.get("model", ""),
            budget_exhausted=bool(data.get("budget_exhausted", False)),
        )


class ReplicaClient:
    def __init__(self, base_url: str, manager_token: str, timeout: float = 120.0) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not manager_token:
            raise ValueError("manager_token is required")
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {manager_token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    # ── Non-streaming chat ────────────────────────────────────────────────────
    async def chat(
        self,
        message: str,
        conversation_id: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id

        async def _do(c: httpx.AsyncClient) -> ChatResult:
            r = await c.post(f"{self._base_url}/ui/api/chat", headers=self._headers, json=body)
            if r.status_code // 100 != 2:
                raise ReplicaError(f"chat failed: {r.status_code} {r.text[:300]}")
            return ChatResult.from_json(r.json())

        if client is not None:
            return await _do(client)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            return await _do(c)

    # ── Streaming chat (SSE) ──────────────────────────────────────────────────
    async def chat_stream(
        self,
        message: str,
        conversation_id: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> AsyncIterator[dict]:
        """Yield SSE events as dicts. The caller consumes until ``event=done`` or
        ``event=error``. Each dict always has an ``"event"`` key plus the
        decoded JSON payload."""
        body: dict[str, Any] = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id

        if client is not None:
            async for ev in _stream_sse(client, f"{self._base_url}/ui/api/chat/stream", self._headers, body):
                yield ev
            return
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            async for ev in _stream_sse(c, f"{self._base_url}/ui/api/chat/stream", self._headers, body):
                yield ev

    # ── Conversations ─────────────────────────────────────────────────────────
    async def list_conversations(self, limit: int = 20, *, client: httpx.AsyncClient | None = None) -> list[dict]:
        async def _do(c: httpx.AsyncClient) -> list[dict]:
            r = await c.get(
                f"{self._base_url}/ui/api/chat/conversations",
                headers=self._headers,
                params={"limit": limit},
            )
            if r.status_code // 100 != 2:
                raise ReplicaError(f"list_conversations failed: {r.status_code} {r.text[:300]}")
            return list(r.json().get("conversations", []))

        if client is not None:
            return await _do(client)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            return await _do(c)

    async def get_conversation(self, conversation_id: str, *, client: httpx.AsyncClient | None = None) -> list[dict]:
        async def _do(c: httpx.AsyncClient) -> list[dict]:
            r = await c.get(
                f"{self._base_url}/ui/api/chat/conversations/{conversation_id}",
                headers=self._headers,
            )
            if r.status_code // 100 != 2:
                raise ReplicaError(f"get_conversation failed: {r.status_code} {r.text[:300]}")
            return list(r.json().get("turns", []))

        if client is not None:
            return await _do(client)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            return await _do(c)


async def _stream_sse(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: dict,
) -> AsyncIterator[dict]:
    """Minimal SSE reader: a block is a run of ``field: value`` lines terminated
    by a blank line. We only care about ``event`` and ``data`` fields. The data
    is always JSON here (manager_agent always ``json.dumps``-es it)."""
    async with client.stream("POST", url, headers=headers, json=body) as r:
        if r.status_code // 100 != 2:
            text = (await r.aread()).decode("utf-8", "replace")
            raise ReplicaError(f"chat_stream failed: {r.status_code} {text[:300]}")

        event_name = "message"
        data_parts: list[str] = []

        async for raw_line in r.aiter_lines():
            line = raw_line.rstrip("\r")
            if line == "":
                if data_parts:
                    joined = "\n".join(data_parts)
                    try:
                        payload = json.loads(joined)
                    except json.JSONDecodeError:
                        payload = {"raw": joined}
                    if not isinstance(payload, dict):
                        payload = {"value": payload}
                    yield {"event": event_name, **payload}
                event_name = "message"
                data_parts = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_parts.append(line.split(":", 1)[1].lstrip())
