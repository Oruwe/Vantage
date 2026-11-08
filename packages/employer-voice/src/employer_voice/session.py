"""Session orchestrator: one or many text turns against the manager agent.

Two surfaces:
  - ``ask_once`` — pure text in / text out, non-streaming.
  - ``ask_stream`` — text in, SSE events out (tool_call / tool_result / message_delta).

Both persist the ``conversation_id`` back to the config so follow-up turns
continue the same chat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from employer_voice import config as _cfg
from employer_voice.replica_client import ChatResult, ReplicaClient


@dataclass
class StreamSummary:
    conversation_id: str = ""
    assistant_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    loops: int = 0
    budget_exhausted: bool = False
    pending_approval: bool = False
    pending_tool_use_id: str = ""


def _client_from_config(cfg: _cfg.VoiceConfig) -> ReplicaClient:
    if not cfg.replica.manager_token:
        raise RuntimeError(
            "No manager_token configured. Run 'employer-voice init --manager-token vsm_...' first."
        )
    return ReplicaClient(cfg.replica.url, cfg.replica.manager_token)


async def ask_once(
    message: str,
    *,
    cfg: _cfg.VoiceConfig | None = None,
    config_path: Path | None = None,
) -> ChatResult:
    cfg = cfg or _cfg.load(config_path)
    client = _client_from_config(cfg)
    result = await client.chat(
        message=message,
        conversation_id=cfg.conversation_id or None,
    )
    if result.conversation_id:
        _cfg.update_conversation_id(result.conversation_id, config_path)
    return result


async def ask_stream(
    message: str,
    *,
    cfg: _cfg.VoiceConfig | None = None,
    config_path: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> StreamSummary:
    cfg = cfg or _cfg.load(config_path)
    client = _client_from_config(cfg)

    summary = StreamSummary()
    text_parts: list[str] = []

    async for ev in client.chat_stream(
        message=message,
        conversation_id=cfg.conversation_id or None,
    ):
        if on_event is not None:
            on_event(ev)
        name = ev.get("event")
        if name == "started":
            summary.conversation_id = ev.get("conversation_id", "")
        elif name == "message_delta":
            text_parts.append(ev.get("content", ""))
        elif name == "tool_call":
            summary.tool_calls.append({
                "id": ev.get("id"),
                "tool": ev.get("tool"),
                "args": ev.get("args", {}),
                "requires_confirmation": bool(ev.get("requires_confirmation")),
            })
        elif name == "done":
            summary.conversation_id = ev.get("conversation_id", summary.conversation_id)
            summary.stop_reason = ev.get("stop_reason", "")
            summary.loops = int(ev.get("loops", 0))
            summary.budget_exhausted = bool(ev.get("budget_exhausted"))
            summary.pending_approval = bool(ev.get("pending_approval"))
            summary.pending_tool_use_id = ev.get("pending_tool_use_id", "")
        elif name == "error":
            raise RuntimeError(ev.get("detail", "stream error"))

    summary.assistant_text = "".join(text_parts).strip()

    if summary.conversation_id:
        _cfg.update_conversation_id(summary.conversation_id, config_path)
    return summary
