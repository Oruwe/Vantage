"""Scenario replay harness for the manager agent.

The real ManagerAgent is driven by two external things:
  1. An Anthropic client (returns tool_use blocks / text / stop_reason)
  2. A ToolContext + the tools module (returns tool results)

Replacing #1 with a scripted mock and #2 with a canned-response table lets us
run the full agent control flow deterministically. The scenarios assert:

  - expected_tools     — every tool_name that should be called
  - forbidden_tools    — must not appear in the run
  - max_loops          — the LLM-side loop count must stay within budget
  - must_mention       — the final assistant text must contain each string
  - expect_budget_ok   — budget_exhausted must match this flag

This file is the shared harness. See scenarios.jsonl + test_scenarios.py for
the actual assertions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Mock Anthropic SDK shapes ───────────────────────────────────────────────


@dataclass
class MockBlock:
    """One block in an assistant message — text OR tool_use."""
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class MockResponse:
    """A single Anthropic messages.create() return — content + stop_reason."""
    content: list[MockBlock]
    stop_reason: str = "end_turn"


class _MockMessages:
    def __init__(self, parent: "MockAnthropicClient") -> None:
        self._parent = parent

    async def create(self, **kwargs) -> MockResponse:
        self._parent.calls.append(kwargs)
        if not self._parent._script:
            raise AssertionError(
                "MockAnthropicClient ran out of scripted responses — "
                f"call #{len(self._parent.calls)}; add another entry to the script"
            )
        return self._parent._script.pop(0)


class MockAnthropicClient:
    """Minimal stand-in for ``anthropic.AsyncAnthropic``.

    The manager agent only touches ``.messages.create(...)`` — we don't need
    to mimic any other surface.
    """

    def __init__(self, script: list[MockResponse]) -> None:
        self._script = list(script)
        self.messages = _MockMessages(self)
        self.calls: list[dict] = []


# ── Scenario / assertions ───────────────────────────────────────────────────


@dataclass
class Scenario:
    id: str
    question: str
    script: list[MockResponse]            # mocked LLM responses
    tool_outputs: dict[str, Any]          # tool_name -> canned result
    expected_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    max_loops: int = 8
    must_mention: list[str] = field(default_factory=list)
    expect_budget_ok: bool = True
    note: str = ""


def _parse_block(d: dict) -> MockBlock:
    return MockBlock(
        type=d["type"],
        text=d.get("text", ""),
        id=d.get("id", ""),
        name=d.get("name", ""),
        input=d.get("input", {}),
    )


def _parse_response(d: dict) -> MockResponse:
    return MockResponse(
        content=[_parse_block(b) for b in d.get("content", [])],
        stop_reason=d.get("stop_reason", "end_turn"),
    )


def load_scenarios(path: Path) -> list[Scenario]:
    """Load all scenarios from a JSONL file."""
    out: list[Scenario] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            out.append(Scenario(
                id=raw["id"],
                question=raw["question"],
                script=[_parse_response(r) for r in raw["script"]],
                tool_outputs=raw.get("tool_outputs", {}),
                expected_tools=list(raw.get("expected_tools", [])),
                forbidden_tools=list(raw.get("forbidden_tools", [])),
                max_loops=int(raw.get("max_loops", 8)),
                must_mention=list(raw.get("must_mention", [])),
                expect_budget_ok=bool(raw.get("expect_budget_ok", True)),
                note=raw.get("note", ""),
            ))
    return out


# ── Agent runner wired up with mocks ────────────────────────────────────────


async def run_scenario(scenario: Scenario, tmp_dir: Path):
    """Build a fresh ManagerAgent wired to mocks + run the scripted turn."""
    from shadow_replica.agent import tools as toolmod
    from shadow_replica.agent.chat_memory import ChatMemory
    from shadow_replica.agent.manager_agent import ManagerAgent

    memory = ChatMemory(tmp_dir / "chat.db")
    tool_outputs = dict(scenario.tool_outputs)
    # Track call counts so list-valued outputs can act as "sequential" scripts
    # (first call → tool_outputs[name][0], second → [1], etc.).
    call_counters: dict[str, int] = {}

    original_invoke = toolmod.invoke

    async def _fake_invoke(_ctx, name: str, args: dict):
        if name not in tool_outputs:
            return {"error": f"no canned output for tool {name!r}"}
        out = tool_outputs[name]
        if isinstance(out, list):
            idx = call_counters.get(name, 0)
            call_counters[name] = idx + 1
            return out[idx] if idx < len(out) else out[-1]
        if callable(out):
            return out(args)
        return out

    toolmod.invoke = _fake_invoke  # type: ignore[assignment]

    try:
        client = MockAnthropicClient(scenario.script)
        # ToolContext expects real stores; we pass sentinels because _fake_invoke
        # never dereferences them.
        ctx = toolmod.ToolContext(store=None, hub=None, cfg=None, agent=None)  # type: ignore[arg-type]
        agent = ManagerAgent(tool_ctx=ctx, memory=memory, anthropic_client=client)
        result = await agent.run_turn(
            manager_token="vsm_test_eval",
            message=scenario.question,
        )
    finally:
        toolmod.invoke = original_invoke  # type: ignore[assignment]

    return result
