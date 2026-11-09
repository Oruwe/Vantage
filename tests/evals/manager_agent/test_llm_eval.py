"""Live-LLM manager-agent eval. Opt-in.

Skipped by default. Run with::

    ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/evals/manager_agent/test_llm_eval.py -m llm_eval

These tests call Anthropic for real — they catch regressions the deterministic
scenario tests cannot, because they test the *LLM's* tool-selection behaviour
given the real system prompt and tool schemas. Each run costs a few cents.

Gate this to main-branch CI, not every PR. Treat failures as advisories for
the first few iterations until you know what score "good" vs "noisy" is.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from shadow_replica.agent import tools as toolmod
from shadow_replica.agent.chat_memory import ChatMemory
from shadow_replica.agent.manager_agent import ManagerAgent


def _canned_tools() -> dict:
    """Canned tool outputs so the live LLM has stable data to reason over.

    The LLM is still real; only the tools are stubbed, so we can assert on
    tool selection + whether the final text references the right facts.
    """
    return {
        "list_developers": {
            "developers": [
                {"name": "alice", "online": True, "last_seen": "2m ago"},
                {"name": "bob", "online": False, "last_seen": "3h ago"},
            ],
        },
        "get_timeline": {
            "artifacts": [
                {
                    "id": "c-abc123",
                    "type": "commit_report",
                    "title": "abc123: auth refactor — split middleware",
                    "developer": "alice",
                },
            ],
        },
        "get_brief": {
            "window_hours": 24,
            "summary": "Alice shipped auth refactor; Bob opened PR #42 for payment timeout.",
        },
        "search_artifacts": {
            "hits": [
                {"id": "r-1", "title": "PR #42: payment timeout fix", "developer": "bob"},
            ],
        },
        "get_friction": {
            "by_developer": {
                "alice": {"score": 0.21},
                "bob": {"score": 0.72, "hot_files": ["payments/handlers.ts"]},
            },
        },
    }


@pytest.fixture
def real_agent(tmp_path: Path):
    """Real ManagerAgent wired to a real Anthropic client + stubbed tools."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        pytest.skip("anthropic SDK not installed")

    client = AsyncAnthropic(api_key=api_key)
    memory = ChatMemory(tmp_path / "chat.db")
    ctx = toolmod.ToolContext(store=None, hub=None, cfg=None, agent=None)  # type: ignore[arg-type]

    canned = _canned_tools()
    original_invoke = toolmod.invoke

    async def _fake(_c, name: str, args: dict):
        return canned.get(name, {"error": f"no canned output for {name}"})

    toolmod.invoke = _fake  # type: ignore[assignment]
    agent = ManagerAgent(tool_ctx=ctx, memory=memory, anthropic_client=client)
    yield agent
    toolmod.invoke = original_invoke  # type: ignore[assignment]


@pytest.mark.llm_eval
def test_live_who_is_on_team(real_agent) -> None:
    """Simple orienting question — the LLM should reach for list_developers."""
    result = asyncio.run(real_agent.run_turn(
        manager_token="vsm_live_eval",
        message="Who's on the team right now?",
    ))
    tools = [c.tool_name for c in result.tool_calls]
    assert "list_developers" in tools, (
        f"expected list_developers, got {tools}"
    )
    assert "alice" in result.assistant_message.lower() or "bob" in result.assistant_message.lower()


@pytest.mark.llm_eval
def test_live_developer_question_uses_timeline(real_agent) -> None:
    """A question naming a developer should pull their timeline first."""
    result = asyncio.run(real_agent.run_turn(
        manager_token="vsm_live_eval",
        message="What has Alice been working on today?",
    ))
    tools = [c.tool_name for c in result.tool_calls]
    assert "get_timeline" in tools, f"expected get_timeline, got {tools}"
    assert "auth" in result.assistant_message.lower() or "abc123" in result.assistant_message


@pytest.mark.llm_eval
def test_live_does_not_invent_data(real_agent) -> None:
    """Agent is instructed not to invent facts. When the tool returns empty,
    the final message must acknowledge the gap rather than fabricate."""
    # Override one tool to return empty so we can check the honesty rule.
    async def _empty(_c, name: str, args: dict):
        if name == "search_artifacts":
            return {"hits": []}
        return _canned_tools().get(name, {})

    toolmod.invoke = _empty  # type: ignore[assignment]
    try:
        result = asyncio.run(real_agent.run_turn(
            manager_token="vsm_live_eval_honesty",
            message="Summarize everything we shipped on 1999-12-25.",
        ))
    finally:
        pass  # restored by fixture teardown

    text = result.assistant_message.lower()
    honesty_signals = ["no artifacts", "nothing", "no record", "couldn't find",
                       "no data", "don't have", "not found", "no results"]
    assert any(sig in text for sig in honesty_signals), (
        f"agent should admit no data; instead said: {result.assistant_message!r}"
    )
