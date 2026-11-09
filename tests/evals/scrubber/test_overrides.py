"""Precision eval driven by dev-reported overrides.

If a ``VANTAGE_OVERRIDES_DB`` env var points at a vantage.db, we load its
``scrubber_overrides`` table and run each as a precision case: the recorded
pattern must NOT be redacted by the current scrubber, OR we tolerate it with
a warning (depending on policy).

Default behaviour when the env var is unset: skip, nothing to assert against.

The intent is to let a developer's local false-positive reports flow into CI
as regression fixtures, closing the loop on the ``/privacy`` page.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def _load_overrides() -> list[tuple[str, str]]:
    db_path = os.environ.get("VANTAGE_OVERRIDES_DB", "").strip()
    if not db_path:
        return []
    p = Path(db_path)
    if not p.exists():
        return []
    try:
        conn = sqlite3.connect(p, timeout=2.0)
        rows = conn.execute(
            "SELECT pattern, kind FROM scrubber_overrides"
        ).fetchall()
        conn.close()
        return [(pat, kind) for pat, kind in rows]
    except sqlite3.Error:
        return []


OVERRIDE_CASES = _load_overrides()


@pytest.mark.skipif(
    not OVERRIDE_CASES,
    reason="no VANTAGE_OVERRIDES_DB set or no overrides recorded yet",
)
@pytest.mark.parametrize(
    "pattern,kind",
    OVERRIDE_CASES,
    ids=[f"override-{i}" for i in range(len(OVERRIDE_CASES))],
)
def test_dev_reported_false_positive_is_not_over_matched(pattern: str, kind: str) -> None:
    """A dev-reported false positive must not be redacted as ``kind``.

    If the current scrubber still matches, this fails loud — exactly what we
    want: the scrubber needs tightening for a pattern the dev already called out.
    """
    from ghost_daemon.redaction.scrubber import scrub

    ctx = scrub(pattern)
    offending = [r.kind for r in ctx.redactions if r.kind == kind]
    assert not offending, (
        f"dev reported pattern {pattern!r} as a false-positive for kind "
        f"{kind!r}, but the current scrubber still matches it "
        f"({len(offending)} hit(s))"
    )
