"""Shared fixtures for the manager-agent eval suite.

The main job here is a **cost guardrail** for the live-LLM eval. Without it,
accidentally running the full ``-m llm_eval`` selection — or a flaky loop —
can burn real money against the Anthropic API.

Two knobs:
  - ``VANTAGE_EVAL_MAX_TOKENS`` (default: 50_000) — the hard session cap
    across *all* live-LLM tests in one pytest run. Exceeding it raises.
  - ``VANTAGE_EVAL_PER_CALL_MAX_TOKENS`` (default: 20_000) — per-call cap
    enforced by inspecting ``response.usage`` right after each Anthropic
    completion. Protects against one scenario accidentally looping.

If ``ANTHROPIC_API_KEY`` isn't set the fixtures no-op (nothing to guard).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest


_DEFAULT_SESSION_CAP = 50_000
_DEFAULT_PER_CALL_CAP = 20_000


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


@dataclass
class _TokenBudget:
    session_cap: int
    per_call_cap: int
    consumed: int = 0

    def charge(self, input_tokens: int, output_tokens: int) -> None:
        call_total = int(input_tokens) + int(output_tokens)
        if call_total > self.per_call_cap:
            raise pytest.fail.Exception(
                f"Live-LLM eval single-call budget exceeded: "
                f"{call_total} tokens > per-call cap {self.per_call_cap}. "
                f"Set VANTAGE_EVAL_PER_CALL_MAX_TOKENS to raise it."
            )
        self.consumed += call_total
        if self.consumed > self.session_cap:
            raise pytest.fail.Exception(
                f"Live-LLM eval session budget exceeded: "
                f"{self.consumed} tokens > session cap {self.session_cap}. "
                f"Set VANTAGE_EVAL_MAX_TOKENS to raise it, or narrow the "
                f"test selection with -k."
            )


@pytest.fixture(scope="session")
def eval_token_budget() -> _TokenBudget:
    return _TokenBudget(
        session_cap=_int_env("VANTAGE_EVAL_MAX_TOKENS", _DEFAULT_SESSION_CAP),
        per_call_cap=_int_env("VANTAGE_EVAL_PER_CALL_MAX_TOKENS", _DEFAULT_PER_CALL_CAP),
    )


@pytest.fixture(autouse=True)
def _enforce_llm_budget(request, eval_token_budget, monkeypatch):
    """Wrap ``AsyncAnthropic.messages.create`` so every real call charges
    the session budget. Only activates when the test carries the
    ``llm_eval`` marker — deterministic scenario tests aren't affected."""
    if "llm_eval" not in request.keywords:
        yield
        return
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        yield
        return

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        yield
        return

    original = AsyncAnthropic.messages.__get__(AsyncAnthropic).__class__.create  # type: ignore[attr-defined]
    # Patch at the Messages resource class level so every client built in
    # the test (fixtures create fresh clients) picks it up.
    from anthropic.resources import messages as _msgs_mod

    orig_create = _msgs_mod.AsyncMessages.create

    async def _budgeted_create(self, *args, **kwargs):
        resp = await orig_create(self, *args, **kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            eval_token_budget.charge(
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
            )
        return resp

    monkeypatch.setattr(_msgs_mod.AsyncMessages, "create", _budgeted_create)
    yield
