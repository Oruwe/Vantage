"""Tests for the live-LLM eval token-budget guardrail.

We don't exercise the autouse monkeypatch here (it only triggers for tests
marked ``llm_eval``). Instead we hit the ``_TokenBudget`` class directly —
that's where the fail-fast logic lives.
"""
from __future__ import annotations

import pytest


def test_budget_accumulates_across_calls():
    from tests.evals.manager_agent.conftest import _TokenBudget

    b = _TokenBudget(session_cap=10_000, per_call_cap=5_000)
    b.charge(input_tokens=1_000, output_tokens=500)
    b.charge(input_tokens=2_000, output_tokens=300)
    assert b.consumed == 3_800


def test_per_call_cap_fails_fast():
    from tests.evals.manager_agent.conftest import _TokenBudget

    b = _TokenBudget(session_cap=100_000, per_call_cap=1_000)
    with pytest.raises(BaseException, match="single-call budget"):
        b.charge(input_tokens=800, output_tokens=500)


def test_session_cap_fails_after_cumulative_total():
    """One call under both caps is fine; the next call that crosses the
    session cap must fail even if it's under the per-call cap."""
    from tests.evals.manager_agent.conftest import _TokenBudget

    b = _TokenBudget(session_cap=1_500, per_call_cap=1_000)
    b.charge(input_tokens=500, output_tokens=400)  # 900 total, under both
    with pytest.raises(BaseException, match="session budget"):
        b.charge(input_tokens=400, output_tokens=400)  # pushes session to 1_700


def test_env_var_defaults_when_unset(monkeypatch):
    from tests.evals.manager_agent.conftest import _int_env, _DEFAULT_SESSION_CAP

    monkeypatch.delenv("VANTAGE_EVAL_MAX_TOKENS", raising=False)
    assert _int_env("VANTAGE_EVAL_MAX_TOKENS", _DEFAULT_SESSION_CAP) == _DEFAULT_SESSION_CAP


def test_env_var_ignored_when_non_positive(monkeypatch):
    """Zero/negative values fall back to the default — prevents a typo like
    ``VANTAGE_EVAL_MAX_TOKENS=0`` from disabling the guard silently."""
    from tests.evals.manager_agent.conftest import _int_env

    monkeypatch.setenv("VANTAGE_EVAL_MAX_TOKENS", "0")
    assert _int_env("VANTAGE_EVAL_MAX_TOKENS", 7) == 7
    monkeypatch.setenv("VANTAGE_EVAL_MAX_TOKENS", "-100")
    assert _int_env("VANTAGE_EVAL_MAX_TOKENS", 7) == 7


def test_env_var_ignored_when_malformed(monkeypatch):
    from tests.evals.manager_agent.conftest import _int_env

    monkeypatch.setenv("VANTAGE_EVAL_MAX_TOKENS", "not-a-number")
    assert _int_env("VANTAGE_EVAL_MAX_TOKENS", 42) == 42
