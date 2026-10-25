"""Tests for the CollisionDetector."""
from __future__ import annotations

from shadow_replica.collision.detector import CollisionDetector
from vantage.schemas import IntentBeacon


def _b(dev: str, repo: str, files: list[str], intent: str = ""):
    return IntentBeacon(
        developer_id=dev, repo=repo, files_touched=files, intent_hint=intent,
    )


def test_no_overlap_no_collision():
    d = CollisionDetector(threshold=0.4)
    d.ingest(_b("alice", "vantage", ["a.py", "b.py"]))
    assert d.ingest(_b("bob", "vantage", ["c.py", "d.py"])) == []


def test_strong_overlap_triggers_collision():
    d = CollisionDetector(threshold=0.4)
    d.ingest(_b("alice", "vantage", ["user_session.py", "auth.py"], "refactor UserSession"))
    cands = d.ingest(_b("bob", "vantage", ["user_session.py", "auth.py"], "add logout flow"))
    assert len(cands) == 1
    c = cands[0]
    assert sorted((c.developer_a, c.developer_b)) == ["alice", "bob"]
    assert c.overlapping_files == ["auth.py", "user_session.py"]
    assert c.jaccard == 1.0
    # Pair-stable ordering means intent_a maps to developer_a (alphabetically alice)
    assert c.intent_a == "refactor UserSession"
    assert c.intent_b == "add logout flow"


def test_different_repos_do_not_collide():
    d = CollisionDetector(threshold=0.4)
    d.ingest(_b("alice", "vantage", ["foo.py", "bar.py"]))
    assert d.ingest(_b("bob", "other-repo", ["foo.py", "bar.py"])) == []


def test_single_shared_file_in_small_set_is_below_threshold():
    d = CollisionDetector(threshold=0.4)
    d.ingest(_b("alice", "vantage", ["a.py", "b.py", "c.py", "d.py"]))
    # 1 shared / 7 union = 0.14 — below threshold
    assert d.ingest(_b("bob", "vantage", ["a.py", "e.py", "f.py", "g.py"])) == []


def test_cooldown_suppresses_repeat_alerts():
    d = CollisionDetector(threshold=0.3)
    d.ingest(_b("alice", "vantage", ["x.py", "y.py"]))
    first = d.ingest(_b("bob", "vantage", ["x.py", "y.py"]))
    assert len(first) == 1
    # Second overlap of the same pair within cooldown → no alert
    second = d.ingest(_b("alice", "vantage", ["x.py", "y.py", "z.py"]))
    assert second == []


def test_snapshot_returns_latest_per_developer():
    d = CollisionDetector()
    d.ingest(_b("alice", "r", ["one.py"]))
    d.ingest(_b("alice", "r", ["two.py"]))  # overwrites alice's entry
    snap = d.snapshot()
    assert len(snap) == 1
    assert snap[0].files_touched == ["two.py"]
