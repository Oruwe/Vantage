"""Few-shot example selector + agent integration.

Covers:
  - select_examples returns most recent edits of the requested type
  - type filter excludes mismatched artifact types
  - max_examples cap honoured
  - max_chars cap honoured (returns fewer when big)
  - render_system_suffix is empty when examples=[]
  - render_system_suffix embeds proposed + approved JSON when examples present
  - StandupAgent's draft call includes few-shot block in its system prompt
  - No examples → system prompt is unchanged
  - Store that raises on read is absorbed silently
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ghost_daemon.insights.edit_store import EditDiff, EditDiffStore
from ghost_daemon.insights.few_shot import (
    FewShotExample,
    render_system_suffix,
    select_examples,
)


# ── Selector ────────────────────────────────────────────────────────────────


def _seed(store: EditDiffStore, *, artifact_type: str, n: int, prefix: str = "x") -> None:
    for i in range(n):
        store.record(EditDiff(
            draft_id=f"{prefix}-{i}",
            developer_id="alice",
            artifact_type=artifact_type,
            approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            changed_fields=["summary"],
            original={"summary": f"agent wrote {i}"},
            edited={"summary": f"dev wanted {i}"},
            original_chars=12,
            edited_chars=11,
        ))


def test_select_examples_returns_most_recent_newest_first(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    _seed(store, artifact_type="standup", n=5)
    examples = select_examples(store, artifact_type="standup", max_examples=3)
    assert len(examples) == 3
    # read_recent returns newest-first; the latest record is "x-4"
    assert "wanted 4" in examples[0].approved


def test_select_examples_filters_by_artifact_type(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    _seed(store, artifact_type="standup", n=2, prefix="s")
    _seed(store, artifact_type="commit_report", n=2, prefix="c")
    standup = select_examples(store, artifact_type="standup")
    commit = select_examples(store, artifact_type="commit_report")
    assert len(standup) == 2 and all("s-" in e.proposed or "wanted" in e.approved for e in standup)
    assert len(commit) == 2
    # No cross-contamination
    for e in standup:
        assert "c-" not in e.proposed
    for e in commit:
        assert "s-" not in e.proposed


def test_select_examples_honors_max_chars(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    big = "A" * 500
    for i in range(5):
        store.record(EditDiff(
            draft_id=f"b-{i}",
            developer_id="alice",
            artifact_type="standup",
            approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            original={"text": big},
            edited={"text": big},
        ))
    # Each example is ~2 × (len(big) truncated to _PER_EXAMPLE_PAYLOAD_CHARS=350) = ~700 chars.
    # With max_chars=1000, we expect 1 example before the budget breaks.
    examples = select_examples(store, artifact_type="standup", max_examples=5, max_chars=1000)
    assert 0 < len(examples) <= 2, f"expected 1-2 examples under 1000 chars, got {len(examples)}"


def test_select_examples_returns_empty_when_store_is_none() -> None:
    assert select_examples(None, artifact_type="standup") == []


def test_select_examples_returns_empty_when_no_matches(tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")
    assert select_examples(store, artifact_type="standup") == []


def test_select_examples_absorbs_store_exceptions(monkeypatch, tmp_path: Path) -> None:
    store = EditDiffStore(tmp_path / "edits")

    def _boom(limit: int = 50) -> list[EditDiff]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "read_recent", _boom)
    # Must not propagate — a broken insight read must never block an agent.
    assert select_examples(store, artifact_type="standup") == []


# ── Renderer ────────────────────────────────────────────────────────────────


def test_render_system_suffix_empty_on_no_examples() -> None:
    assert render_system_suffix([]) == ""


def test_render_system_suffix_includes_example_pairs() -> None:
    ex = FewShotExample(
        proposed='{"summary":"before"}',
        approved='{"summary":"after"}',
        changed_fields=["summary"],
        approved_at="2026-11-08T12:00:00",
    )
    out = render_system_suffix([ex])
    assert "You proposed:" in out
    assert "before" in out
    assert "after" in out
    assert "developer approved" in out


# ── Agent integration (StandupAgent) ────────────────────────────────────────


def test_standup_agent_injects_few_shot_examples_into_system_prompt(tmp_path: Path) -> None:
    """When the StandupAgent is constructed with an EditDiffStore that has
    prior edits, its system prompt to the LLM must include those examples."""
    from ghost_daemon.agents.standup_agent import StandupAgent
    from ghost_daemon.config import Config, LLMConfig

    store = EditDiffStore(tmp_path / "edits")
    store.record(EditDiff(
        draft_id="d1",
        developer_id="alice",
        artifact_type="standup",
        approved_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        changed_fields=["yesterday"],
        original={"yesterday": ["did the auth thing"], "today": [], "blockers": []},
        edited={"yesterday": ["shipped auth refactor to staging"], "today": [], "blockers": []},
    ))

    captured: dict = {}

    class _FakeLLM:
        async def complete(self, messages, max_tokens=400, **kwargs):
            captured["messages"] = messages
            return '{"yesterday":[],"today":[],"blockers":[]}'

    class _FakeMap:
        def refresh(self): pass
    class _FakeTailer:
        async def tail_recent(self, p, n=40): return []

    cfg = Config(developer_id="alice", llm=LLMConfig())
    agent = StandupAgent(cfg, _FakeLLM(), _FakeMap(), _FakeTailer(), edits=store)

    # Drive the draft node directly — the collect/scrub nodes would hit the FS.
    from ghost_daemon.redaction.scrubber import scrub
    state = {
        "developer_id": "alice",
        "yesterday_start": datetime(2026, 11, 7),
        "yesterday_end": datetime(2026, 11, 8),
        "raw_turns": [],
        "git_activity": "",
        "redacted_context": scrub("some context text that is non-empty enough"),
        "llm_output": None,
        "draft": None,
        "error": None,
    }
    import asyncio
    asyncio.run(agent._draft_bullets(state))

    sys_msg = next(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert "shipped auth refactor to staging" in sys_msg, (
        "system prompt should contain the dev's edited phrasing from history"
    )
    assert "EXAMPLE 1:" in sys_msg


def test_standup_agent_without_edits_store_has_clean_prompt(tmp_path: Path) -> None:
    """No store → no suffix injected; system prompt is exactly the baseline."""
    from ghost_daemon.agents.standup_agent import StandupAgent, _SYSTEM_PROMPT
    from ghost_daemon.config import Config, LLMConfig

    captured: dict = {}

    class _FakeLLM:
        async def complete(self, messages, max_tokens=400, **kwargs):
            captured["messages"] = messages
            return '{"yesterday":[],"today":[],"blockers":[]}'

    class _FakeMap:
        def refresh(self): pass
    class _FakeTailer:
        async def tail_recent(self, p, n=40): return []

    cfg = Config(developer_id="alice", llm=LLMConfig())
    agent = StandupAgent(cfg, _FakeLLM(), _FakeMap(), _FakeTailer())  # no edits=

    from ghost_daemon.redaction.scrubber import scrub
    state = {
        "developer_id": "alice",
        "yesterday_start": datetime(2026, 11, 7),
        "yesterday_end": datetime(2026, 11, 8),
        "raw_turns": [],
        "git_activity": "",
        "redacted_context": scrub("filler"),
        "llm_output": None,
        "draft": None,
        "error": None,
    }
    import asyncio
    asyncio.run(agent._draft_bullets(state))

    sys_msg = next(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert sys_msg == _SYSTEM_PROMPT, "no examples → suffix must be empty"
    assert "EXAMPLE" not in sys_msg


def test_standup_agent_ignores_edits_from_other_artifact_types(tmp_path: Path) -> None:
    """A dev with lots of edited commit reports but no edited standups should
    get a clean standup prompt."""
    from ghost_daemon.agents.standup_agent import StandupAgent, _SYSTEM_PROMPT
    from ghost_daemon.config import Config, LLMConfig

    store = EditDiffStore(tmp_path / "edits")
    _seed(store, artifact_type="commit_report", n=3)  # wrong type for standup

    captured: dict = {}

    class _FakeLLM:
        async def complete(self, messages, max_tokens=400, **kwargs):
            captured["messages"] = messages
            return '{"yesterday":[],"today":[],"blockers":[]}'

    class _FakeMap:
        def refresh(self): pass
    class _FakeTailer:
        async def tail_recent(self, p, n=40): return []

    cfg = Config(developer_id="alice", llm=LLMConfig())
    agent = StandupAgent(cfg, _FakeLLM(), _FakeMap(), _FakeTailer(), edits=store)

    from ghost_daemon.redaction.scrubber import scrub
    state = {
        "developer_id": "alice",
        "yesterday_start": datetime(2026, 11, 7),
        "yesterday_end": datetime(2026, 11, 8),
        "raw_turns": [],
        "git_activity": "",
        "redacted_context": scrub("filler"),
        "llm_output": None,
        "draft": None,
        "error": None,
    }
    import asyncio
    asyncio.run(agent._draft_bullets(state))

    sys_msg = next(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert sys_msg == _SYSTEM_PROMPT


# ── End-to-end flywheel (approval → next draft uses it) ─────────────────────


def test_edit_approve_then_next_standup_sees_the_edit(tmp_path: Path) -> None:
    """The flywheel: dev approves an edited draft, the next standup renders a
    system prompt containing that edit."""
    from ghost_daemon.agents.standup_agent import StandupAgent
    from ghost_daemon.approval.gate import ApprovalGate
    from ghost_daemon.config import Config, LLMConfig
    from ghost_daemon.storage.sqlite_store import SQLiteStore
    from vantage.schemas import DraftPayload, StandupBullets

    edits = EditDiffStore(tmp_path / "edits")
    store = SQLiteStore(tmp_path / "vantage.db")
    gate = ApprovalGate(store, "alice", edits=edits)

    # Step 1: dev approves an edited standup.
    draft = DraftPayload(
        draft_id="d1",
        artifact_type="standup",
        developer_id="alice",
        payload=StandupBullets(yesterday=["did a thing"], today=["more things"]),
    )
    gate.submit(draft)
    gate.approve("d1", edited_payload={
        "yesterday": ["shipped the PR"],
        "today": ["pair on auth"],
        "blockers": [],
    })

    # Step 2: next standup run picks up the edit as few-shot.
    captured: dict = {}

    class _FakeLLM:
        async def complete(self, messages, max_tokens=400, **kwargs):
            captured["messages"] = messages
            return '{"yesterday":[],"today":[],"blockers":[]}'

    class _FakeMap:
        def refresh(self): pass
    class _FakeTailer:
        async def tail_recent(self, p, n=40): return []

    agent = StandupAgent(
        Config(developer_id="alice", llm=LLMConfig()),
        _FakeLLM(), _FakeMap(), _FakeTailer(),
        edits=edits,
    )

    from ghost_daemon.redaction.scrubber import scrub
    state = {
        "developer_id": "alice",
        "yesterday_start": datetime(2026, 11, 7),
        "yesterday_end": datetime(2026, 11, 8),
        "raw_turns": [],
        "git_activity": "",
        "redacted_context": scrub("filler context long enough to proceed"),
        "llm_output": None,
        "draft": None,
        "error": None,
    }
    import asyncio
    asyncio.run(agent._draft_bullets(state))

    sys_msg = next(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert "shipped the PR" in sys_msg, "the dev's edited phrasing should appear in the next draft's prompt"
