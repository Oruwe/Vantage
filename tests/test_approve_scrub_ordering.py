"""Regression tests for the approve-time scrubber ordering.

Before this fix, ``ApprovalGate.approve()`` wrote the raw edited_payload to
SQLite and recorded a raw edit-diff *before* scrubbing. A secret pasted into
an edit would therefore flow out through three separate channels:

1. ``store.get_draft(draft_id).edited_payload_json`` — raw in the local DB,
   and later read verbatim by ``get_approved()``.
2. ``ApprovalGate.get_approved(draft_id)`` — used by ``vantage-ghost share``
   to re-push an already-approved draft without re-running approve.
3. ``few_shot.select_examples(edits, artifact_type)`` — the recorded diff
   fed back into the system prompt of every future digest/commit/standup
   agent run, so the raw secret reaches the LLM provider on every draft.

The fix: scrub at the top of ``approve()``, store the scrubbed data, diff
scrubbed-vs-scrubbed, and build the ApprovedPayload from scrubbed data. Also
scrub on the read side in ``get_approved()`` and ``select_examples()`` so
rows written before this fix are covered on next access.
"""
from __future__ import annotations

from ghost_daemon.approval.gate import ApprovalGate
from ghost_daemon.insights.edit_store import EditDiffStore
from ghost_daemon.insights.few_shot import render_system_suffix, select_examples
from ghost_daemon.storage.sqlite_store import SQLiteStore
from vantage.schemas import DraftPayload, StandupBullets


# Canonical valid AKIA pattern used throughout the scrubber tests.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "vantage.db")
    edits = EditDiffStore(tmp_path / "edits")
    gate = ApprovalGate(store, "alice", edits=edits)
    return store, edits, gate


def test_approved_draft_row_in_sqlite_contains_no_raw_secret(tmp_path):
    store, _edits, gate = _setup(tmp_path)

    clean = DraftPayload(
        draft_id="d1",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(
            yesterday=["wrote docs"], today=["debug flaky test"], blockers=[],
        ),
    )
    gate.submit(clean)
    gate.approve(
        "d1",
        edited_payload={
            "yesterday": ["wrote docs"],
            "today": [f"accidentally pasted {_AWS_KEY} into standup"],
            "blockers": [],
        },
    )

    row = store.get_draft("d1")
    assert row is not None
    assert row.status == "approved"
    # (1) The persisted edited payload must not retain the raw AKIA key.
    assert row.edited_payload_json is not None
    assert _AWS_KEY not in row.edited_payload_json
    assert "[REDACTED:aws_access_key" in row.edited_payload_json


def test_get_approved_returns_scrubbed_payload(tmp_path):
    """`share <draft-id>` on an already-approved draft calls get_approved()."""
    store, _edits, gate = _setup(tmp_path)

    clean = DraftPayload(
        draft_id="d2",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(yesterday=["x"], today=["y"], blockers=[]),
    )
    gate.submit(clean)
    gate.approve(
        "d2",
        edited_payload={
            "yesterday": ["x"],
            "today": [f"leaked {_AWS_KEY} here"],
            "blockers": [],
        },
    )

    # (2) Re-fetch via the path share() uses.
    approved = gate.get_approved("d2")
    body = approved.model_dump_json()
    assert _AWS_KEY not in body
    assert "[REDACTED:aws_access_key" in body


def test_few_shot_examples_do_not_leak_secrets_into_system_prompt(tmp_path):
    """Recorded edits feed back into every future agent's system prompt."""
    store, edits, gate = _setup(tmp_path)

    clean = DraftPayload(
        draft_id="d3",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(
            yesterday=["pair with bob"], today=["ship M20"], blockers=[],
        ),
    )
    gate.submit(clean)
    gate.approve(
        "d3",
        edited_payload={
            "yesterday": ["pair with bob"],
            "today": [f"ship M20; key={_AWS_KEY}"],
            "blockers": [],
        },
    )

    # (3) The few-shot suffix is injected into the standup/digest/commit
    # agents' system prompts on every new draft. If it contains a raw
    # secret, that secret is sent to the LLM provider on every run.
    examples = select_examples(edits, "standup")
    assert examples, "expected at least one recorded edit for standup"
    suffix = render_system_suffix(examples)
    assert _AWS_KEY not in suffix
