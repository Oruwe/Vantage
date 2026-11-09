"""Tier-0 safety eval: scrubber precision floor (no false positives).

Every fixture with an empty ``expected_kinds`` is a "must-NOT-redact" case —
benign content that an over-eager scrubber would mangle. For each one:
  1. Every substring in ``require_substrings`` must survive untouched.
  2. No redactions should be produced.

Precision floor: >= 90% of negative fixtures pass. Below that, the scrubber
is eating real content and the UX becomes noisy.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghost_daemon.redaction.scrubber import scrub

FIXTURES_PATH = Path(__file__).parent / "fixtures.jsonl"


def _load_negative_fixtures() -> list[dict]:
    cases: list[dict] = []
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            if not case.get("expected_kinds"):
                cases.append(case)
    return cases


NEGATIVE_CASES = _load_negative_fixtures()


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c["id"] for c in NEGATIVE_CASES])
def test_benign_content_survives_scrubbing(case: dict) -> None:
    ctx = scrub(case["input"])

    for required in case.get("require_substrings", []):
        assert required in ctx.scrubbed_text, (
            f"[{case['id']}] FALSE POSITIVE — benign substring {required!r} was redacted.\n"
            f"  input:   {case['input']!r}\n"
            f"  output:  {ctx.scrubbed_text!r}\n"
            f"  kinds:   {[r.kind for r in ctx.redactions]}"
        )


def test_overall_precision_floor() -> None:
    """Aggregate precision: >= 90% of benign fixtures produce zero redactions.

    The scrubber may still over-match on a handful of exotic strings (e.g.
    content that genuinely looks like a secret). We allow a small tail of
    over-matches rather than hard-failing at 100%, because the asymmetric
    cost favours recall over precision — but if precision collapses we want
    to know fast.
    """
    clean_count = 0
    for case in NEGATIVE_CASES:
        ctx = scrub(case["input"])
        if not ctx.redactions:
            clean_count += 1

    precision = clean_count / len(NEGATIVE_CASES) if NEGATIVE_CASES else 1.0
    assert precision >= 0.90, (
        f"precision floor breached: {clean_count}/{len(NEGATIVE_CASES)} "
        f"benign fixtures passed cleanly ({precision:.0%}); require >= 90%"
    )


def test_precision_fixture_count_floor() -> None:
    assert len(NEGATIVE_CASES) >= 15, (
        f"only {len(NEGATIVE_CASES)} negative fixtures present (expected >= 15)"
    )
