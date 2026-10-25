"""Tests for compute_friction + build_beacon."""
from __future__ import annotations

from ghost_daemon.agents.activity_extractor import (
    build_beacon, compute_friction, extract_activity_signals,
)
from vantage.schemas import Turn


def _t(role: str, content: str, tool: str = ""):
    return Turn(role=role, content_excerpt=content, tool_name=tool or None)


def test_friction_zero_on_clean_edit_session():
    turns = [
        _t("user", "please add a logout button"),
        _t("assistant", "ok, editing"),
        _t("tool", '"file_path": "/repo/src/auth.py"', tool="Edit"),
        _t("tool", "command succeeded, no output", tool="Bash"),
    ]
    r = compute_friction(turns, developer_id="alice", minutes_since_commit=5)
    assert r.score < 0.15
    assert r.summary == "flow"


def test_friction_rises_with_failed_tool_calls():
    turns = [
        _t("user", "run the build"),
        _t("tool", "command failed with exit code 1", tool="Bash"),
        _t("tool", "command failed non-zero exit code", tool="Bash"),
        _t("tool", "Error: build command errored out", tool="Bash"),
    ]
    r = compute_friction(turns, developer_id="bob", minutes_since_commit=0)
    assert r.failed_tool_calls == 3
    assert r.score > 0.2
    assert "failed" in r.summary


def test_friction_counts_repeated_error_signatures():
    turns = [
        _t("tool", "Traceback: TypeError: foo", tool="Bash"),
        _t("tool", "Error: something failed", tool="Bash"),
        _t("tool", "exception thrown — failed again", tool="Bash"),
    ]
    r = compute_friction(turns, developer_id="carl")
    assert r.repeated_errors >= 1


def test_friction_adds_penalty_for_long_time_since_commit():
    turns = [_t("user", "working") for _ in range(2)]
    r_short = compute_friction(turns, "d", minutes_since_commit=10)
    r_long = compute_friction(turns, "d", minutes_since_commit=240)
    assert r_long.score > r_short.score


def test_build_beacon_strips_to_essentials():
    turns = [
        _t("user", "refactor the User session code"),
        _t("tool", '"file_path": "/repo/src/user_session.py"', tool="Edit"),
        _t("tool", "git status\n* feature/users", tool="Bash"),
    ]
    sig = extract_activity_signals(turns, cwd="/repo")
    beacon = build_beacon(sig, developer_id="alice", turn_count=len(turns))
    assert beacon.developer_id == "alice"
    assert "user_session.py" in beacon.files_touched
    assert beacon.turn_count == 3
    # Intent hint comes from last_user_intent
    assert "User session" in beacon.intent_hint or "refactor" in beacon.intent_hint
