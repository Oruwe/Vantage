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

import json

from ghost_daemon.approval.gate import ApprovalGate
from ghost_daemon.insights.edit_store import EditDiffStore, compute_diff
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


# ---------------------------------------------------------------------------
# Legacy-read coverage: rows persisted by earlier builds (which skipped the
# approve-time scrub entirely) must still be cleaned on the next access.
# ---------------------------------------------------------------------------


def test_legacy_sqlite_row_is_rescrubbed_by_get_approved(tmp_path):
    """Simulate a row written by an older build that bypassed the gate.

    We persist the raw edited payload directly through ``SQLiteStore.approve``
    — the exact shape an earlier release of ``ApprovalGate.approve`` left
    behind. ``gate.get_approved`` must still hand back a clean payload.
    """
    store, _edits, gate = _setup(tmp_path)

    clean = DraftPayload(
        draft_id="legacy1",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(yesterday=["x"], today=["y"], blockers=[]),
    )
    gate.submit(clean)

    # Bypass ApprovalGate.approve entirely; write raw JSON straight into
    # SQLite the way the pre-fix code did.
    raw_edit_json = json.dumps({
        "yesterday": ["x"],
        "today": [f"legacy paste: {_AWS_KEY}"],
        "blockers": [],
    })
    store.approve("legacy1", raw_edit_json, "manual")

    # The row on disk is deliberately raw.
    row = store.get_draft("legacy1")
    assert row is not None and row.edited_payload_json is not None
    assert _AWS_KEY in row.edited_payload_json

    # But any caller that goes through the gate should get a clean payload.
    approved = gate.get_approved("legacy1")
    assert _AWS_KEY not in approved.model_dump_json()
    assert "[REDACTED:aws_access_key" in approved.model_dump_json()


def test_legacy_edit_diff_shard_is_rescrubbed_by_select_examples(tmp_path):
    """Simulate a JSONL shard row written by an older build.

    We call ``edits.record`` with a raw ``EditDiff`` directly — mirroring the
    pre-fix state where ``compute_diff`` ran against a raw ``edited``. The
    shard content is deliberately dirty on disk. Still, nothing dirty may
    reach the few-shot system-prompt suffix.
    """
    _store, edits, _gate = _setup(tmp_path)

    raw_diff = compute_diff(
        draft_id="legacy_shard",
        developer_id="alice",
        artifact_type="standup",
        original={"yesterday": ["x"], "today": ["y"], "blockers": []},
        edited={
            "yesterday": ["x"],
            "today": [f"old shard with {_AWS_KEY} baked in"],
            "blockers": [],
        },
    )
    assert raw_diff is not None
    edits.record(raw_diff)

    # Shard on disk is raw — that's the whole point of the simulation.
    shards = list((tmp_path / "edits").glob("*.jsonl"))
    assert shards, "expected a shard file to be written"
    assert any(_AWS_KEY in p.read_text(encoding="utf-8") for p in shards)

    # But the few-shot path must never surface the raw key.
    examples = select_examples(edits, "standup")
    assert examples, "expected the legacy diff to be selectable"
    suffix = render_system_suffix(examples)
    assert _AWS_KEY not in suffix
    assert "[REDACTED:aws_access_key" in suffix


def test_edit_diff_shard_never_contains_raw_secret_after_fresh_approval(tmp_path):
    """Write-side coverage: scan every JSONL in the edits dir on disk.

    This is the strong post-condition: once ``gate.approve`` has run, no file
    in the edits directory may contain the raw secret anywhere — not inside
    ``edited``, not inside ``original``, not in a ``changed_fields`` summary.
    """
    _store, _edits, gate = _setup(tmp_path)

    clean = DraftPayload(
        draft_id="fresh1",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(yesterday=["x"], today=["y"], blockers=[]),
    )
    gate.submit(clean)
    gate.approve(
        "fresh1",
        edited_payload={
            "yesterday": ["x"],
            "today": [f"accidentally included {_AWS_KEY} in standup"],
            "blockers": [],
        },
    )

    edits_dir = tmp_path / "edits"
    shards = list(edits_dir.glob("*.jsonl"))
    assert shards, "expected at least one shard to be written by approve()"
    for shard in shards:
        text = shard.read_text(encoding="utf-8")
        assert _AWS_KEY not in text, (
            f"raw AKIA key found in shard {shard.name!r} — approve() must "
            f"scrub both sides before compute_diff + record"
        )
