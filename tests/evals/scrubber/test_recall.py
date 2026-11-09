"""Tier-0 safety eval: scrubber must have 100% recall on known secrets.

Every fixture with a non-empty ``expected_kinds`` is a "must-redact" case.
For each one we assert:
  1. Every substring in ``forbid_substrings`` is GONE from the scrubbed text.
     (The secret must not survive — this is the kernel of the trust model.)
  2. Every kind in ``expected_kinds`` appears in the ``redactions`` list.
     (Secondary check — forbid_substrings is the real gate.)

A regression here is release-blocking. See CLAUDE.md for the trust model.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghost_daemon.redaction.scrubber import scrub

FIXTURES_PATH = Path(__file__).parent / "fixtures.jsonl"


def _load_recall_fixtures() -> list[dict]:
    cases: list[dict] = []
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            if case.get("expected_kinds"):
                cases.append(case)
    return cases


RECALL_CASES = _load_recall_fixtures()


@pytest.mark.parametrize("case", RECALL_CASES, ids=[c["id"] for c in RECALL_CASES])
def test_scrubber_removes_known_secrets(case: dict) -> None:
    ctx = scrub(case["input"])

    for forbidden in case.get("forbid_substrings", []):
        assert forbidden not in ctx.scrubbed_text, (
            f"[{case['id']}] SECRET LEAK — substring {forbidden!r} survived scrubbing.\n"
            f"  input:   {case['input']!r}\n"
            f"  output:  {ctx.scrubbed_text!r}\n"
            f"  kinds:   {[r.kind for r in ctx.redactions]}"
        )


@pytest.mark.parametrize("case", RECALL_CASES, ids=[c["id"] for c in RECALL_CASES])
def test_scrubber_labels_match_expected_kinds(case: dict) -> None:
    ctx = scrub(case["input"])
    actual_kinds = {r.kind for r in ctx.redactions}
    expected = set(case["expected_kinds"])

    missing = expected - actual_kinds
    assert not missing, (
        f"[{case['id']}] missing expected redaction kind(s): {sorted(missing)}.\n"
        f"  input:   {case['input']!r}\n"
        f"  actual:  {sorted(actual_kinds)}"
    )


def test_recall_fixture_count_floor() -> None:
    """Keep the fixture set from decaying over time — a regression here means
    somebody removed scenarios instead of adding them."""
    assert len(RECALL_CASES) >= 35, (
        f"only {len(RECALL_CASES)} recall fixtures present (expected >= 35); "
        f"someone may have deleted scenarios"
    )
