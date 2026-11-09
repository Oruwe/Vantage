"""Session orchestrator: one or many text turns against the manager agent.

Three surfaces:
  - ``ask_once`` — pure text in / text out, non-streaming.
  - ``ask_stream`` — text in, SSE events out (tool_call / tool_result / message_delta).
  - ``resume_approval`` — continues a paused turn after the manager approves
    or denies a dangerous tool call.

All three persist the ``conversation_id`` back to the config so follow-up
turns continue the same chat.
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
    pending_tool_name: str = ""
    pending_tool_args: dict = field(default_factory=dict)


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
        _apply_event(summary, text_parts, ev, on_event)

    summary.assistant_text = "".join(text_parts).strip()

    if summary.conversation_id:
        _cfg.update_conversation_id(summary.conversation_id, config_path)
    return summary


async def resume_approval(
    tool_use_id: str,
    approved: bool,
    *,
    cfg: _cfg.VoiceConfig | None = None,
    config_path: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> StreamSummary:
    """Resume a paused turn after the manager says yes/no on a dangerous tool.

    Yields the same shape of events as ``ask_stream`` and returns the same
    StreamSummary. The caller can chain further resumes if the continuation
    itself pauses on another approval.
    """
    cfg = cfg or _cfg.load(config_path)
    client = _client_from_config(cfg)

    summary = StreamSummary()
    text_parts: list[str] = []

    async for ev in client.chat_approve_stream(tool_use_id, approved):
        _apply_event(summary, text_parts, ev, on_event)

    summary.assistant_text = "".join(text_parts).strip()

    if summary.conversation_id:
        _cfg.update_conversation_id(summary.conversation_id, config_path)
    return summary


def _apply_event(
    summary: StreamSummary,
    text_parts: list[str],
    ev: dict,
    on_event: Callable[[dict], None] | None,
) -> None:
    """Fold one SSE event into ``summary``. Shared by ask_stream + resume_approval."""
    if on_event is not None:
        on_event(ev)
    name = ev.get("event")
    if name == "started":
        summary.conversation_id = ev.get("conversation_id", "")
    elif name == "message_delta":
        text_parts.append(ev.get("content", ""))
    elif name == "tool_call":
        call = {
            "id": ev.get("id"),
            "tool": ev.get("tool"),
            "args": ev.get("args", {}),
            "requires_confirmation": bool(ev.get("requires_confirmation")),
        }
        summary.tool_calls.append(call)
        if call["requires_confirmation"]:
            # Remember the last pending call so the summary carries full
            # context even if the agent streams more text before the done.
            summary.pending_tool_name = call["tool"] or ""
            summary.pending_tool_args = call["args"] or {}
    elif name == "done":
        summary.conversation_id = ev.get("conversation_id", summary.conversation_id)
        summary.stop_reason = ev.get("stop_reason", "")
        summary.loops = int(ev.get("loops", 0))
        summary.budget_exhausted = bool(ev.get("budget_exhausted"))
        summary.pending_approval = bool(ev.get("pending_approval"))
        summary.pending_tool_use_id = ev.get("pending_tool_use_id", "")
    elif name == "error":
        raise RuntimeError(ev.get("detail", "stream error"))
