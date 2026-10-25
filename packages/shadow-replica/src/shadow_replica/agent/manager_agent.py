"""Tool-calling manager agent with conversation memory.

Drives the Anthropic tool-use loop: send user turn → model emits text or
tool_use → run the tool → send tool_result back → loop until the model stops
or we hit the budget. Prompt caching keeps the system + tool defs free on
every turn after the first.

Phase 1: non-streaming. The endpoint builds the final response in one shot and
also emits one A2AEnvelope per tool call so the wiretap stays live.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent
from vantage.schemas import ChatTurn

from shadow_replica.agent.chat_memory import ChatMemory, hash_manager_token
from shadow_replica.agent import tools as toolmod

if TYPE_CHECKING:
    from shadow_replica.wiretap.bus import WiretapBus

logger = logging.getLogger(__name__)

_MAX_LOOPS = 8
_MAX_OUTPUT_TOKENS = 4096
_DEFAULT_MODEL = "claude-sonnet-4-6"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """You are Vantage, the engineering-manager agent for a small team.

You have memory of this conversation and five read-only investigation tools:
- search_artifacts: semantic search across approved commits, standups, digests, PRs, bug resolutions, GitHub events
- get_timeline: one developer's recent artifacts
- get_brief: team daily brief over a time window
- list_developers: who's on the team + their online/offline status
- get_friction: AI-friction scores + hot files per developer

Investigation style:
- When a question is open-ended, start by orienting (list_developers or get_brief) then drill in.
- When a question names a developer, read their timeline first, then search for cross-team context.
- Chain tools when needed — do not stop after one call if the question isn't fully answered.
- Prefer specific facts (names, SHAs, PR numbers, file paths) over paraphrase. Cite them.
- If a tool returns no results, try a reformulated query before giving up.
- Final answers should be short (3-6 sentences unless the manager asked for depth). Never pad.

Hard rules:
- Do not invent artifacts, developers, SHAs, PR numbers, or facts not in tool output.
- If the data isn't there, say so plainly and suggest what to look at next.
- You are a read-only agent in this phase — never promise to send messages, probe live, or modify state."""


@dataclass
class ToolCallRecord:
    tool_use_id: str
    tool_name: str
    tool_input: dict
    tool_output_preview: str = ""
    latency_ms: int = 0


@dataclass
class AgentTurnResult:
    conversation_id: str
    assistant_message: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    stop_reason: str = ""
    loops: int = 0
    model: str = ""
    budget_exhausted: bool = False


class ManagerAgent:
    """Owns one agent instance; call `run_turn` per user message."""

    def __init__(
        self,
        tool_ctx: toolmod.ToolContext,
        memory: ChatMemory,
        anthropic_client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        bus: "WiretapBus | None" = None,
    ) -> None:
        self._tool_ctx = tool_ctx
        self._memory = memory
        self._client = anthropic_client
        self._model = model
        self._bus = bus
        # Pending approvals keyed by tool_use_id. Each entry is the full state
        # needed to resume the agent loop after the manager says yes/no.
        self._pending_approvals: dict[str, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_turn(
        self,
        manager_token: str,
        message: str,
        conversation_id: str | None = None,
    ) -> AgentTurnResult:
        if not message.strip():
            raise ValueError("message is required")
        if self._client is None:
            raise RuntimeError("Anthropic client not configured; set [llm] in replica.toml with provider=anthropic")

        mgr_hash = hash_manager_token(manager_token)
        convo = self._memory.get_or_create(mgr_hash, conversation_id=conversation_id, title_seed=message)

        self._memory.append_turn(convo.conversation_id, role="user", content=message)
        if not convo.title:
            self._memory.update_title_if_blank(convo.conversation_id, message)

        history_turns = self._memory.list_turns(convo.conversation_id)
        messages = _turns_to_anthropic_messages(history_turns)

        tool_calls: list[ToolCallRecord] = []
        last_text = ""
        stop_reason = ""
        loops = 0
        budget_exhausted = False

        for _ in range(_MAX_LOOPS):
            loops += 1
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=_cached_system_blocks(),
                tools=_cached_tool_blocks(),
                messages=messages,
            )
            stop_reason = getattr(resp, "stop_reason", "") or ""
            blocks = list(getattr(resp, "content", []) or [])

            # Collect any text + any tool_use blocks the model emitted this round.
            text_this_round = _extract_text(blocks)
            tool_uses = [b for b in blocks if _block_type(b) == "tool_use"]

            if text_this_round:
                last_text = text_this_round

            # Record the assistant message exactly as returned (serialized).
            messages.append({"role": "assistant", "content": _serialize_blocks(blocks)})

            if not tool_uses:
                # Model is done.
                if last_text:
                    self._memory.append_turn(convo.conversation_id, role="assistant", content=last_text)
                break

            # Run the tool calls and append tool_result blocks.
            tool_results_payload: list[dict] = []
            for tu in tool_uses:
                tu_id = _attr(tu, "id") or ""
                tu_name = _attr(tu, "name") or ""
                tu_input = _attr(tu, "input") or {}
                if not isinstance(tu_input, dict):
                    try:
                        tu_input = dict(tu_input)
                    except Exception:
                        tu_input = {}
                t0 = time.monotonic()
                if toolmod.requires_confirmation(tu_name):
                    # Non-streaming endpoint can't prompt for confirmation —
                    # synthesize a denial so the agent recovers gracefully.
                    result = {
                        "status": "requires_streaming_endpoint",
                        "message": (
                            f"The tool '{tu_name}' requires manager confirmation. "
                            "Use the streaming endpoint (/ui/api/chat/stream) so "
                            "the manager can approve or deny this call interactively."
                        ),
                    }
                else:
                    try:
                        result = await toolmod.invoke(self._tool_ctx, tu_name, tu_input)
                    except Exception as exc:
                        result = {"error": str(exc)}
                latency_ms = int((time.monotonic() - t0) * 1000)

                try:
                    result_json = json.dumps(result, default=str)
                except Exception:
                    result_json = json.dumps({"error": "unserializable tool result"})

                preview = result_json[:400]
                tool_calls.append(ToolCallRecord(
                    tool_use_id=tu_id,
                    tool_name=tu_name,
                    tool_input=tu_input,
                    tool_output_preview=preview,
                    latency_ms=latency_ms,
                ))

                # Persist the tool-result turn in memory so follow-up turns inherit it.
                self._memory.append_turn(
                    convo.conversation_id,
                    role="tool",
                    content=result_json,
                    tool_name=tu_name,
                    tool_call_id=tu_id,
                    tool_input_json=json.dumps(tu_input, default=str),
                )

                tool_results_payload.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": result_json,
                })

                # Wiretap: emit one call + one result envelope per tool invocation.
                if self._bus is not None:
                    try:
                        agent_ref = A2AAgentRef(kind="manager-agent", label="Manager Agent")
                        tool_ref = A2AAgentRef(kind="shadow-replica", label=tu_name)
                        call_env = A2AEnvelope(
                            intent=A2AIntent.AGENT_TOOL_CALL,
                            from_agent=agent_ref,
                            to_agent=tool_ref,
                            session_id=convo.conversation_id,
                            summary=f"{tu_name}({_short_args(tu_input)})",
                            payload={"tool": tu_name, "input": _coerce_jsonable(tu_input)},
                        )
                        self._bus.publish(call_env)
                        self._bus.publish(A2AEnvelope(
                            intent=A2AIntent.AGENT_TOOL_RESULT,
                            from_agent=tool_ref,
                            to_agent=agent_ref,
                            session_id=convo.conversation_id,
                            parent_turn_id=call_env.turn_id,
                            latency_ms=latency_ms,
                            summary=f"{tu_name} → {preview[:120]}",
                            payload={"tool": tu_name, "preview": preview},
                        ))
                    except Exception as exc:
                        logger.debug("Wiretap publish for tool call failed: %s", exc)

            messages.append({"role": "user", "content": tool_results_payload})

            if stop_reason != "tool_use":
                # Model wanted to stop but still emitted tool_use — rare but respect it:
                # keep looping until the loop cap or until end_turn.
                pass
        else:
            budget_exhausted = True
            notice = (
                "I hit the per-turn investigation budget (8 tool steps). "
                "Here's what I found so far:\n\n" + (last_text or "(no partial text)")
            )
            self._memory.append_turn(convo.conversation_id, role="assistant", content=notice)
            last_text = notice

        if self._bus is not None and last_text:
            try:
                self._bus.publish(A2AEnvelope(
                    intent=A2AIntent.AGENT_RESPONSE,
                    from_agent=A2AAgentRef(kind="manager-agent", label="Manager Agent"),
                    to_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                    session_id=convo.conversation_id,
                    summary=last_text[:160],
                    payload={"text": last_text[:1200], "loops": loops},
                ))
            except Exception as exc:
                logger.debug("Wiretap publish for agent response failed: %s", exc)

        return AgentTurnResult(
            conversation_id=convo.conversation_id,
            assistant_message=last_text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            loops=loops,
            model=self._model,
            budget_exhausted=budget_exhausted,
        )

    async def run_turn_stream(
        self,
        manager_token: str,
        message: str,
        conversation_id: str | None = None,
    ):
        """Streaming variant of run_turn — yields chunk dicts round by round.

        Chunk shapes:
          {"type": "started", "conversation_id": "..."}
          {"type": "tool_call", "id": "...", "tool": "...", "args": {...},
                                "requires_confirmation": bool}
          {"type": "tool_result", "id": "...", "tool": "...", "preview": "...",
                                "latency_ms": N, "approved": bool}
          {"type": "message_delta", "content": "..."}
          {"type": "done", "conversation_id": "...", "loops": N, "stop_reason": "...",
                                "budget_exhausted": bool, "pending_approval": bool,
                                "pending_tool_use_id": "..."}
          {"type": "error", "detail": "..."}

        Round-level streaming, not token-level. For dangerous tools (those with
        requires_confirmation=True) the stream pauses after yielding the
        tool_call chunk and emits a done chunk with pending_approval=True; the
        caller must invoke resume_after_approval() to continue.
        """
        if not message.strip():
            yield {"type": "error", "detail": "message is required"}
            return
        if self._client is None:
            yield {"type": "error", "detail": "Anthropic client not configured"}
            return

        mgr_hash = hash_manager_token(manager_token)
        convo = self._memory.get_or_create(mgr_hash, conversation_id=conversation_id, title_seed=message)
        self._memory.append_turn(convo.conversation_id, role="user", content=message)
        if not convo.title:
            self._memory.update_title_if_blank(convo.conversation_id, message)

        yield {"type": "started", "conversation_id": convo.conversation_id}

        history_turns = self._memory.list_turns(convo.conversation_id)
        messages = _turns_to_anthropic_messages(history_turns)

        async for chunk in self._run_loop(
            convo=convo,
            mgr_hash=mgr_hash,
            messages=messages,
            loops_so_far=0,
            last_text="",
        ):
            yield chunk

    async def resume_after_approval(
        self,
        manager_token: str,
        tool_use_id: str,
        approved: bool,
    ):
        """Finish a tool call that was paused for manager confirmation.

        Looks up the pending state keyed by `tool_use_id`, runs (or synthesizes
        a denial for) the tool, feeds the result back to the model, and
        continues streaming the agent loop. Yields the same chunk types as
        run_turn_stream, starting with a tool_result for the resolved call.
        """
        pending = self._pending_approvals.pop(tool_use_id, None)
        if pending is None:
            yield {"type": "error", "detail": f"no pending approval for {tool_use_id}"}
            return

        if pending["manager_token_hash"] != hash_manager_token(manager_token):
            # Replay-attack guard — don't let a different manager approve
            # someone else's pending action. Put it back and refuse.
            self._pending_approvals[tool_use_id] = pending
            yield {"type": "error", "detail": "not authorized for this pending approval"}
            return

        if self._client is None:
            yield {"type": "error", "detail": "Anthropic client not configured"}
            return

        convo = self._memory.get_or_create(
            pending["manager_token_hash"],
            conversation_id=pending["conversation_id"],
        )
        messages: list[dict] = list(pending["messages_before_results"])
        tool_results_payload: list[dict] = list(pending["tool_results_so_far"])
        tool_name = pending["tool_name"]
        tool_input = pending["tool_input"]
        last_text = pending.get("last_text", "")
        loops = int(pending.get("loops", 0))

        # Resolve the paused tool.
        t0 = time.monotonic()
        if approved:
            try:
                result = await toolmod.invoke(self._tool_ctx, tool_name, tool_input)
            except Exception as exc:
                result = {"error": f"tool failed: {exc}"}
        else:
            result = {
                "status": "denied_by_manager",
                "message": (
                    "The manager declined to execute this tool call. "
                    "Explain what you'd learn from it and ask again, or try a "
                    "safe read-only alternative."
                ),
            }
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            result_json = json.dumps(result, default=str)
        except Exception:
            result_json = json.dumps({"error": "unserializable tool result"})
        preview = result_json[:400]

        self._memory.append_turn(
            convo.conversation_id,
            role="tool",
            content=result_json,
            tool_name=tool_name,
            tool_call_id=tool_use_id,
            tool_input_json=json.dumps(tool_input, default=str),
        )
        tool_results_payload.append({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": result_json,
        })

        yield {
            "type": "tool_result",
            "id": tool_use_id,
            "tool": tool_name,
            "preview": preview,
            "latency_ms": latency_ms,
            "approved": bool(approved),
        }

        if self._bus is not None:
            try:
                agent_ref = A2AAgentRef(kind="manager-agent", label="Manager Agent")
                tool_ref = A2AAgentRef(kind="shadow-replica", label=tool_name)
                self._bus.publish(A2AEnvelope(
                    intent=A2AIntent.AGENT_TOOL_RESULT,
                    from_agent=tool_ref,
                    to_agent=agent_ref,
                    session_id=convo.conversation_id,
                    latency_ms=latency_ms,
                    summary=f"{tool_name} → {'approved' if approved else 'denied'}",
                    payload={"tool": tool_name, "preview": preview, "approved": bool(approved)},
                ))
            except Exception as exc:
                logger.debug("Wiretap publish on resume failed: %s", exc)

        # If more dangerous tools were queued behind this one, re-pause on the
        # next one. Otherwise push the accumulated results back to the model.
        remaining = list(pending.get("remaining_tool_uses", []))
        while remaining:
            tu = remaining.pop(0)
            tu_id = tu.get("id") or ""
            tu_name = tu.get("name") or ""
            tu_input = tu.get("input") or {}
            if toolmod.requires_confirmation(tu_name):
                yield {
                    "type": "tool_call",
                    "id": tu_id,
                    "tool": tu_name,
                    "args": _coerce_jsonable(tu_input),
                    "requires_confirmation": True,
                }
                self._pending_approvals[tu_id] = {
                    "conversation_id": convo.conversation_id,
                    "manager_token_hash": pending["manager_token_hash"],
                    "tool_use_id": tu_id,
                    "tool_name": tu_name,
                    "tool_input": tu_input,
                    "messages_before_results": messages,
                    "tool_results_so_far": tool_results_payload,
                    "remaining_tool_uses": remaining,
                    "last_text": last_text,
                    "loops": loops,
                }
                yield {
                    "type": "done",
                    "conversation_id": convo.conversation_id,
                    "loops": loops,
                    "stop_reason": "awaiting_approval",
                    "budget_exhausted": False,
                    "pending_approval": True,
                    "pending_tool_use_id": tu_id,
                    "model": self._model,
                }
                return
            # Safe tool left over from the paused round — execute inline.
            yield {"type": "tool_call", "id": tu_id, "tool": tu_name, "args": _coerce_jsonable(tu_input)}
            t0 = time.monotonic()
            try:
                r2 = await toolmod.invoke(self._tool_ctx, tu_name, tu_input)
            except Exception as exc:
                r2 = {"error": str(exc)}
            latency_ms2 = int((time.monotonic() - t0) * 1000)
            try:
                r2_json = json.dumps(r2, default=str)
            except Exception:
                r2_json = json.dumps({"error": "unserializable"})
            self._memory.append_turn(
                convo.conversation_id, role="tool", content=r2_json,
                tool_name=tu_name, tool_call_id=tu_id,
                tool_input_json=json.dumps(tu_input, default=str),
            )
            tool_results_payload.append({
                "type": "tool_result", "tool_use_id": tu_id, "content": r2_json,
            })
            yield {
                "type": "tool_result", "id": tu_id, "tool": tu_name,
                "preview": r2_json[:400], "latency_ms": latency_ms2,
            }

        # All of this round's tool uses have been resolved — hand the results
        # back to the model and continue the loop.
        messages.append({"role": "user", "content": tool_results_payload})
        async for chunk in self._run_loop(
            convo=convo,
            mgr_hash=pending["manager_token_hash"],
            messages=messages,
            loops_so_far=loops,
            last_text=last_text,
        ):
            yield chunk

    def pending_approval(self, manager_token: str, tool_use_id: str) -> dict | None:
        """Peek at a pending approval (for the UI to show tool/args before confirming)."""
        p = self._pending_approvals.get(tool_use_id)
        if p is None:
            return None
        if p["manager_token_hash"] != hash_manager_token(manager_token):
            return None
        return {
            "tool_use_id": tool_use_id,
            "tool_name": p["tool_name"],
            "tool_input": p["tool_input"],
            "conversation_id": p["conversation_id"],
        }

    async def _run_loop(
        self,
        convo,
        mgr_hash: str,
        messages: list[dict],
        loops_so_far: int,
        last_text: str,
    ):
        """Shared agent-loop body. Yields chunks until a terminal state.

        Terminal states: natural end-of-turn (yields `done`), budget exhausted
        (yields `done` with budget_exhausted=True), or paused on a dangerous
        tool (yields `done` with pending_approval=True).
        """
        loops = loops_so_far
        stop_reason = ""
        budget_exhausted = False
        pending_tool_use_id: str | None = None

        while loops < _MAX_LOOPS:
            loops += 1
            try:
                resp = await self._client.messages.create(
                    model=self._model,
                    max_tokens=_MAX_OUTPUT_TOKENS,
                    system=_cached_system_blocks(),
                    tools=_cached_tool_blocks(),
                    messages=messages,
                )
            except Exception as exc:
                yield {"type": "error", "detail": f"model call failed: {exc}"}
                return

            stop_reason = getattr(resp, "stop_reason", "") or ""
            blocks = list(getattr(resp, "content", []) or [])
            text_this_round = _extract_text(blocks)
            tool_uses = [b for b in blocks if _block_type(b) == "tool_use"]

            if text_this_round:
                last_text = text_this_round
                yield {"type": "message_delta", "content": text_this_round}

            messages.append({"role": "assistant", "content": _serialize_blocks(blocks)})

            if not tool_uses:
                if last_text:
                    self._memory.append_turn(convo.conversation_id, role="assistant", content=last_text)
                break

            tool_results_payload: list[dict] = []
            paused = False
            for i, tu in enumerate(tool_uses):
                tu_id = _attr(tu, "id") or ""
                tu_name = _attr(tu, "name") or ""
                tu_input = _attr(tu, "input") or {}
                if not isinstance(tu_input, dict):
                    try:
                        tu_input = dict(tu_input)
                    except Exception:
                        tu_input = {}

                if toolmod.requires_confirmation(tu_name):
                    yield {
                        "type": "tool_call",
                        "id": tu_id,
                        "tool": tu_name,
                        "args": _coerce_jsonable(tu_input),
                        "requires_confirmation": True,
                    }
                    self._pending_approvals[tu_id] = {
                        "conversation_id": convo.conversation_id,
                        "manager_token_hash": mgr_hash,
                        "tool_use_id": tu_id,
                        "tool_name": tu_name,
                        "tool_input": tu_input,
                        "messages_before_results": messages,
                        "tool_results_so_far": tool_results_payload,
                        "remaining_tool_uses": [_tu_to_dict(x) for x in tool_uses[i+1:]],
                        "last_text": last_text,
                        "loops": loops,
                    }
                    if self._bus is not None:
                        try:
                            self._bus.publish(A2AEnvelope(
                                intent=A2AIntent.AGENT_TOOL_CALL,
                                from_agent=A2AAgentRef(kind="manager-agent", label="Manager Agent"),
                                to_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                                session_id=convo.conversation_id,
                                status="awaiting_approval",
                                summary=f"{tu_name}({_short_args(tu_input)}) — needs approval",
                                payload={"tool": tu_name, "input": _coerce_jsonable(tu_input),
                                         "requires_confirmation": True,
                                         "tool_use_id": tu_id},
                            ))
                        except Exception as exc:
                            logger.debug("Wiretap publish for pending tool failed: %s", exc)
                    pending_tool_use_id = tu_id
                    paused = True
                    break

                # Safe tool — execute inline.
                yield {
                    "type": "tool_call",
                    "id": tu_id,
                    "tool": tu_name,
                    "args": _coerce_jsonable(tu_input),
                }

                t0 = time.monotonic()
                try:
                    result = await toolmod.invoke(self._tool_ctx, tu_name, tu_input)
                except Exception as exc:
                    result = {"error": str(exc)}
                latency_ms = int((time.monotonic() - t0) * 1000)
                try:
                    result_json = json.dumps(result, default=str)
                except Exception:
                    result_json = json.dumps({"error": "unserializable tool result"})
                preview = result_json[:400]

                self._memory.append_turn(
                    convo.conversation_id,
                    role="tool",
                    content=result_json,
                    tool_name=tu_name,
                    tool_call_id=tu_id,
                    tool_input_json=json.dumps(tu_input, default=str),
                )

                tool_results_payload.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": result_json,
                })

                yield {
                    "type": "tool_result",
                    "id": tu_id,
                    "tool": tu_name,
                    "preview": preview,
                    "latency_ms": latency_ms,
                }

                if self._bus is not None:
                    try:
                        agent_ref = A2AAgentRef(kind="manager-agent", label="Manager Agent")
                        tool_ref = A2AAgentRef(kind="shadow-replica", label=tu_name)
                        call_env = A2AEnvelope(
                            intent=A2AIntent.AGENT_TOOL_CALL,
                            from_agent=agent_ref,
                            to_agent=tool_ref,
                            session_id=convo.conversation_id,
                            summary=f"{tu_name}({_short_args(tu_input)})",
                            payload={"tool": tu_name, "input": _coerce_jsonable(tu_input)},
                        )
                        self._bus.publish(call_env)
                        self._bus.publish(A2AEnvelope(
                            intent=A2AIntent.AGENT_TOOL_RESULT,
                            from_agent=tool_ref,
                            to_agent=agent_ref,
                            session_id=convo.conversation_id,
                            parent_turn_id=call_env.turn_id,
                            latency_ms=latency_ms,
                            summary=f"{tu_name} → {preview[:120]}",
                            payload={"tool": tu_name, "preview": preview},
                        ))
                    except Exception as exc:
                        logger.debug("Wiretap publish for tool call failed: %s", exc)

            if paused:
                yield {
                    "type": "done",
                    "conversation_id": convo.conversation_id,
                    "loops": loops,
                    "stop_reason": "awaiting_approval",
                    "budget_exhausted": False,
                    "pending_approval": True,
                    "pending_tool_use_id": pending_tool_use_id,
                    "model": self._model,
                }
                return

            messages.append({"role": "user", "content": tool_results_payload})
        else:
            budget_exhausted = True
            notice = (
                "I hit the per-turn investigation budget (8 tool steps). "
                "Here's what I found so far:\n\n" + (last_text or "(no partial text)")
            )
            self._memory.append_turn(convo.conversation_id, role="assistant", content=notice)
            last_text = notice
            yield {"type": "message_delta", "content": notice}

        if self._bus is not None and last_text:
            try:
                self._bus.publish(A2AEnvelope(
                    intent=A2AIntent.AGENT_RESPONSE,
                    from_agent=A2AAgentRef(kind="manager-agent", label="Manager Agent"),
                    to_agent=A2AAgentRef(kind="manager-ui", label="Manager"),
                    session_id=convo.conversation_id,
                    summary=last_text[:160],
                    payload={"text": last_text[:1200], "loops": loops},
                ))
            except Exception as exc:
                logger.debug("Wiretap publish for agent response failed: %s", exc)

        yield {
            "type": "done",
            "conversation_id": convo.conversation_id,
            "loops": loops,
            "stop_reason": stop_reason,
            "budget_exhausted": budget_exhausted,
            "model": self._model,
        }

    def list_conversations(self, manager_token: str, limit: int = 20) -> list[dict]:
        mgr_hash = hash_manager_token(manager_token)
        convos = self._memory.list_conversations(mgr_hash, limit=limit)
        return [
            {
                "conversation_id": c.conversation_id,
                "title": c.title,
                "created_at": c.created_at.isoformat(),
                "last_turn_at": c.last_turn_at.isoformat(),
                "turn_count": c.turn_count,
            }
            for c in convos
        ]

    def list_turns(self, manager_token: str, conversation_id: str) -> list[dict]:
        mgr_hash = hash_manager_token(manager_token)
        # Owner-check: the convo must belong to this manager.
        convo = self._memory.get_or_create(mgr_hash, conversation_id=conversation_id)
        if convo.conversation_id != conversation_id:
            # get_or_create returned a *new* convo because the ID was invalid
            # for this manager — treat as "not found".
            return []
        turns = self._memory.list_turns(conversation_id)
        return [
            {
                "turn_id": t.turn_id,
                "role": t.role,
                "content": t.content,
                "tool_name": t.tool_name,
                "tool_call_id": t.tool_call_id,
                "created_at": t.created_at.isoformat(),
            }
            for t in turns
        ]

    def delete_conversation(self, manager_token: str, conversation_id: str) -> bool:
        return self._memory.delete_conversation(hash_manager_token(manager_token), conversation_id)


# ── Factory ──────────────────────────────────────────────────────────────────

def build_manager_agent(
    tool_ctx: toolmod.ToolContext,
    memory: ChatMemory,
    cfg,
    bus=None,
) -> "ManagerAgent | None":
    """Try to build a ManagerAgent from `cfg.llm`. Returns None if not configured."""
    provider = (cfg.llm.provider or "").lower()
    if provider != "anthropic":
        logger.info("Manager agent disabled: provider=%r (needs 'anthropic')", provider)
        return None
    api_key_env = cfg.llm.api_key_env or "ANTHROPIC_API_KEY"
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        logger.info("Manager agent disabled: %s not set", api_key_env)
        return None
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)
    except Exception as exc:
        logger.warning("anthropic SDK import failed: %s", exc)
        return None
    model = cfg.llm.model or _DEFAULT_MODEL
    return ManagerAgent(tool_ctx=tool_ctx, memory=memory, anthropic_client=client, model=model, bus=bus)


# ── Anthropic request helpers ────────────────────────────────────────────────

def _cached_system_blocks() -> list[dict]:
    """Return the system prompt as a cache-eligible content block list."""
    return [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _cached_tool_blocks() -> list[dict]:
    """Tool definitions with cache_control on the last entry (caches entire list)."""
    schemas = toolmod.anthropic_schemas()
    out: list[dict] = []
    for i, s in enumerate(schemas):
        block = dict(s)
        if i == len(schemas) - 1:
            block["cache_control"] = {"type": "ephemeral"}
        out.append(block)
    return out


# ── Message replay ───────────────────────────────────────────────────────────

def _turns_to_anthropic_messages(turns: list[ChatTurn]) -> list[dict]:
    """Reconstruct the Anthropic `messages` array from stored turns.

    Groups adjacent tool-result turns into one user message (per Anthropic
    convention: tool_result blocks belong to a user turn).
    """
    msgs: list[dict] = []
    pending_tool_results: list[dict] = []

    def _flush_tool_results():
        if pending_tool_results:
            msgs.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for t in turns:
        if t.role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": t.tool_call_id,
                "content": t.content,
            })
            continue
        _flush_tool_results()
        if t.role == "user":
            msgs.append({"role": "user", "content": t.content})
        elif t.role == "assistant":
            msgs.append({"role": "assistant", "content": t.content})
    _flush_tool_results()
    return msgs


def _extract_text(blocks) -> str:
    parts = []
    for b in blocks or []:
        if _block_type(b) == "text":
            text = _attr(b, "text") or ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _serialize_blocks(blocks) -> list[dict]:
    """Serialize Anthropic SDK content blocks into plain dicts for the next request."""
    out: list[dict] = []
    for b in blocks or []:
        bt = _block_type(b)
        if bt == "text":
            out.append({"type": "text", "text": _attr(b, "text") or ""})
        elif bt == "tool_use":
            tu_input = _attr(b, "input") or {}
            if not isinstance(tu_input, dict):
                try:
                    tu_input = dict(tu_input)
                except Exception:
                    tu_input = {}
            out.append({
                "type": "tool_use",
                "id": _attr(b, "id") or "",
                "name": _attr(b, "name") or "",
                "input": tu_input,
            })
    return out


def _block_type(b) -> str:
    t = _attr(b, "type")
    return str(t) if t is not None else ""


def _attr(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _short_args(d: dict) -> str:
    try:
        parts = []
        for k, v in d.items():
            vs = str(v)
            if len(vs) > 24:
                vs = vs[:21] + "…"
            parts.append(f"{k}={vs}")
        return ", ".join(parts)[:120]
    except Exception:
        return ""


def _coerce_jsonable(d):
    try:
        return json.loads(json.dumps(d, default=str))
    except Exception:
        return {}


def _tu_to_dict(tu) -> dict:
    """Serialize a tool_use block to a plain dict so it survives a pause/resume."""
    tu_input = _attr(tu, "input") or {}
    if not isinstance(tu_input, dict):
        try:
            tu_input = dict(tu_input)
        except Exception:
            tu_input = {}
    return {
        "id": _attr(tu, "id") or "",
        "name": _attr(tu, "name") or "",
        "input": tu_input,
    }
