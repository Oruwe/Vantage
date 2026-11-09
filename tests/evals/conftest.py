"""Shared fixtures for the eval suites.

Bumps the ``vantage_eval_run_total`` Prometheus counter once per eval item
so CI can scrape eval health off the normal ``/metrics`` surface and plot
pass/fail trends over time.

Auto-use: every test under ``tests/evals/`` gets the counter stamp without
having to opt in. Suite label is derived from the test's package
(``tests/evals/scrubber`` → ``scrubber``, ``tests/evals/manager_agent`` → ``manager_agent``).
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _suite_name_from_path(path: str) -> str:
    """tests/evals/scrubber/test_x.py  →  "scrubber"
    tests/evals/manager_agent/test_x.py  →  "manager_agent"
    Falls back to the parent dir name for anything deeper/shallower.
    """
    parts = Path(path).resolve().parts
    try:
        idx = parts.index("evals")
    except ValueError:
        return "unknown"
    # directory immediately after "evals" is the suite
    if idx + 1 < len(parts):
        return parts[idx + 1].removesuffix(".py")
    return "root"


def _outcome_from_report(report) -> str:
    """Map pytest's (outcome, failed) tuple into our label values.

    Terminology: pytest reports ``passed`` / ``failed`` / ``skipped`` /
    ``error``. We collapse ``error`` (setup/teardown failure) and ``failed``
    into distinct labels so a flaky harness doesn't look like a scrubber
    regression.
    """
    # pytest stashes the actual failure reason in report.outcome, but setup
    # errors come in as outcome="failed" with when="setup". Split them.
    if report.when != "call":
        # Only act on the call phase; setup/teardown reports are duplicates.
        return ""
    if report.passed:
        return "pass"
    if report.skipped:
        return "skip"
    if report.failed:
        return "fail"
    return "error"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Stamp the eval-outcome counter after each call phase."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    label = _outcome_from_report(report)
    if not label:
        return

    try:
        from shadow_replica.metrics import eval_run_total
    except Exception:
        # If prometheus_client isn't importable in this test environment
        # (unlikely — it's a hard dep), the eval still runs normally.
        return

    suite = _suite_name_from_path(str(item.fspath))
    eval_run_total.labels(suite=suite, outcome=label).inc()


@pytest.fixture
def eval_run_counter_snapshot():
    """Return a function that reads the counter for (suite, outcome).

    Used by the self-tests in test_eval_metrics.py. Not part of the normal
    eval surface — regular eval tests ignore it.
    """
    from shadow_replica.metrics import eval_run_total

    def _read(suite: str, outcome: str) -> float:
        # prometheus_client's Counter stores a per-labelset _value; access
        # through the labels() call to get the child's current value.
        return eval_run_total.labels(suite=suite, outcome=outcome)._value.get()

    return _read
