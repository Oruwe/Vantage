"""Deterministic manager-agent scenario eval.

Each row in ``scenarios.jsonl`` scripts an Anthropic conversation + the canned
tool outputs the agent should see. We assert the agent's observable behaviour:

  - every tool in ``expected_tools`` got called (at least once)
  - no tool in ``forbidden_tools`` was ever called
  - the loop count stays ≤ ``max_loops``
  - every string in ``must_mention`` appears in the final assistant message
  - ``budget_exhausted`` matches ``expect_budget_ok``

Failure here means: the agent's control flow or tool-selection scaffolding
regressed, even though the deterministic script is the same. This runs as
part of the default dev-loop (no $, no network) — it's pure plumbing.

Live-LLM scenarios live in ``test_llm_eval.py`` behind the ``llm_eval``
marker.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.evals.manager_agent.harness import Scenario, load_scenarios, run_scenario

SCENARIOS_PATH = Path(__file__).parent / "scenarios.jsonl"
SCENARIOS = load_scenarios(SCENARIOS_PATH)


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[s.id for s in SCENARIOS],
)
def test_scenario(scenario: Scenario, tmp_path: Path) -> None:
    result = asyncio.run(run_scenario(scenario, tmp_path))

    actual_tools = [c.tool_name for c in result.tool_calls]

    # Expected tools: each must appear at least once.
    missing = [t for t in scenario.expected_tools if t not in actual_tools]
    assert not missing, (
        f"[{scenario.id}] expected tool(s) not called: {missing}. "
        f"Actual tools: {actual_tools}"
    )

    # Forbidden tools: none may appear.
    bad = [t for t in actual_tools if t in scenario.forbidden_tools]
    assert not bad, (
        f"[{scenario.id}] forbidden tool(s) were called: {bad}"
    )

    # Loop budget.
    assert result.loops <= scenario.max_loops, (
        f"[{scenario.id}] loops={result.loops} exceeded max_loops={scenario.max_loops}"
    )

    # Must-mention strings in the final assistant message.
    for needle in scenario.must_mention:
        assert needle in result.assistant_message, (
            f"[{scenario.id}] final message missing required string {needle!r}.\n"
            f"Got: {result.assistant_message!r}"
        )

    # Budget flag matches expectation.
    assert result.budget_exhausted != scenario.expect_budget_ok, (
        f"[{scenario.id}] budget_exhausted={result.budget_exhausted}, "
        f"expected {'False' if scenario.expect_budget_ok else 'True'}"
    )


def test_scenario_count_floor() -> None:
    """Keep the fixture set from decaying — someone deleting scenarios
    instead of adding them should trip a CI failure."""
    assert len(SCENARIOS) >= 10, (
        f"only {len(SCENARIOS)} manager-agent scenarios defined (expected >= 10)"
    )


def test_scenarios_have_no_duplicate_ids() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(set(ids)) == len(ids), f"duplicate scenario id(s): {ids}"
