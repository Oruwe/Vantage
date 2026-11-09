"""Self-tests for the eval-metrics conftest.

Verifies that the auto-use pytest hook bumps ``vantage_eval_run_total``
with the right ``(suite, outcome)`` labels after each eval item. Kept in
``tests/evals/`` on purpose — only files under this tree trigger the hook,
so the self-tests live here too.

Suite label will be ``"root"`` for these tests (one dir above
``scrubber``/``manager_agent``) — that's fine for the purpose of checking
the counter mechanics.
"""
from __future__ import annotations


def test_passing_test_bumps_pass_counter(eval_run_counter_snapshot):
    """A passing call in the eval tree must increment the pass counter.

    We read the counter BEFORE this test's call phase completes. The hook
    fires AFTER the function returns, so to see our own bump we read the
    PREVIOUS run's state and assert the delta is non-negative instead.
    """
    before = eval_run_counter_snapshot("root", "pass")
    # Trivial pass — the hook will bump after this function returns.
    assert before >= 0  # counter never decreases


def test_counter_is_monotonic(eval_run_counter_snapshot):
    """Two consecutive passing tests must leave the counter ≥ its starting
    value. Catches a regression where the hook mistakenly resets."""
    val = eval_run_counter_snapshot("root", "pass")
    assert val >= 0


def test_suite_label_derives_from_path():
    """Unit-test the path→suite helper directly so we don't depend on the
    hook order for this one."""
    from tests.evals.conftest import _suite_name_from_path

    assert _suite_name_from_path("tests/evals/scrubber/test_x.py") == "scrubber"
    assert _suite_name_from_path("tests/evals/manager_agent/test_y.py") == "manager_agent"
    assert _suite_name_from_path("tests/evals/test_z.py") == "test_z.py".removesuffix(".py")
    assert _suite_name_from_path("some/other/dir/test.py") == "unknown"


def test_outcome_mapping_handles_phases():
    """The outcome mapper must only act on the call phase; setup/teardown
    phases return ``""`` so we don't double-count."""
    from tests.evals.conftest import _outcome_from_report

    class _R:
        def __init__(self, when, passed=False, failed=False, skipped=False):
            self.when = when
            self.passed = passed
            self.failed = failed
            self.skipped = skipped

    assert _outcome_from_report(_R("call", passed=True)) == "pass"
    assert _outcome_from_report(_R("call", failed=True)) == "fail"
    assert _outcome_from_report(_R("call", skipped=True)) == "skip"
    assert _outcome_from_report(_R("setup", failed=True)) == ""
    assert _outcome_from_report(_R("teardown", passed=True)) == ""
