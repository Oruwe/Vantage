from __future__ import annotations

from datetime import datetime

import pytest

from vantage.schemas import Turn
from ghost_daemon.agents.activity_extractor import (
    extract_activity_signals,
    _extract_branch,
    _extract_path,
)


def _turn(role: str, content: str, tool: str | None = None, ts: datetime | None = None) -> Turn:
    return Turn(role=role, content_excerpt=content, tool_name=tool, timestamp=ts)


# ── File extraction ────────────────────────────────────────────────────────────

def test_files_from_json_style_edit():
    turns = [_turn("tool_use", '{"file_path": "src/auth.py", "old_string": "x", "new_string": "y"}', tool="Edit")]
    sig = extract_activity_signals(turns)
    assert "auth.py" in sig.files_recently_touched


def test_files_from_write_tool():
    turns = [_turn("tool_use", '"path": "tests/test_login.py"', tool="Write")]
    sig = extract_activity_signals(turns)
    assert "test_login.py" in sig.files_recently_touched


def test_files_deduped():
    turns = [
        _turn("tool_use", '"file_path": "auth.py"', tool="Edit"),
        _turn("tool_use", '"file_path": "auth.py"', tool="Edit"),
        _turn("tool_use", '"file_path": "login.html"', tool="Edit"),
    ]
    sig = extract_activity_signals(turns)
    assert sig.files_recently_touched.count("auth.py") == 1
    assert "login.html" in sig.files_recently_touched


def test_files_capped_at_eight():
    turns = [
        _turn("tool_use", f'"file_path": "file{i}.py"', tool="Edit")
        for i in range(15)
    ]
    sig = extract_activity_signals(turns)
    assert len(sig.files_recently_touched) <= 8


# ── Branch detection ───────────────────────────────────────────────────────────

def test_branch_from_git_branch_output():
    content = "[assistant] \n  main\n* feature/login-form\n  dev"
    assert _extract_branch(content) == "feature/login-form"


def test_branch_from_on_branch():
    content = "[tool_use] On branch feature/add-auth"
    assert _extract_branch(content) == "feature/add-auth"


def test_branch_from_checkout():
    content = "[assistant] git checkout -b fix/typo"
    assert _extract_branch(content) == "fix/typo"


def test_branch_from_switched():
    content = "Switched to a new branch 'feature/dark-mode'"
    assert _extract_branch(content) == "feature/dark-mode"


def test_branch_ignores_head():
    content = "* HEAD"
    assert _extract_branch(content) == ""


def test_branch_in_turns():
    turns = [_turn("tool_use", "On branch main\nYour branch is up to date.", tool="Bash")]
    sig = extract_activity_signals(turns)
    assert sig.inferred_branch == "main"


# ── User intent ────────────────────────────────────────────────────────────────

def test_last_user_intent_extracted():
    turns = [
        _turn("user", "[user] write a login form with email and password fields"),
        _turn("assistant", "[assistant] Here's the form..."),
    ]
    sig = extract_activity_signals(turns)
    assert "login form" in sig.last_user_intent


def test_last_user_intent_strips_prefix():
    turns = [_turn("user", "[user] fix the broken test")]
    sig = extract_activity_signals(turns)
    assert sig.last_user_intent == "fix the broken test"


def test_last_user_intent_uses_most_recent():
    turns = [
        _turn("user", "[user] first message"),
        _turn("user", "[user] second message — this should win"),
    ]
    sig = extract_activity_signals(turns)
    assert "second message" in sig.last_user_intent


def test_last_user_intent_capped():
    turns = [_turn("user", "[user] " + "x" * 200)]
    sig = extract_activity_signals(turns)
    assert len(sig.last_user_intent) <= 150


# ── Activity classification ────────────────────────────────────────────────────

def test_classify_editing():
    turns = [
        _turn("tool_use", '"file_path": "a.py"', tool="Edit"),
        _turn("tool_use", '"file_path": "b.py"', tool="Edit"),
        _turn("tool_use", '"file_path": "c.py"', tool="Write"),
    ]
    assert extract_activity_signals(turns).primary_activity == "editing"


def test_classify_debugging():
    turns = [
        _turn("tool_use", "pytest tests/", tool="Bash"),
        _turn("tool_use", "python main.py", tool="Bash"),
        _turn("tool_use", "echo $PATH", tool="Bash"),
    ]
    assert extract_activity_signals(turns).primary_activity == "debugging"


def test_classify_reviewing():
    turns = [
        _turn("tool_use", '"file_path": "auth.py"', tool="Read"),
        _turn("tool_use", '"file_path": "config.py"', tool="Read"),
        _turn("tool_use", "*.py", tool="Glob"),
    ]
    assert extract_activity_signals(turns).primary_activity == "reviewing"


def test_classify_unknown_no_tools():
    turns = [
        _turn("user", "[user] hello"),
        _turn("assistant", "[assistant] hi"),
    ]
    assert extract_activity_signals(turns).primary_activity == "unknown"


# ── Timestamp / session_active_since ──────────────────────────────────────────

def test_session_active_since_is_oldest():
    t1 = datetime(2026, 10, 24, 9, 0)
    t2 = datetime(2026, 10, 24, 10, 0)
    t3 = datetime(2026, 10, 24, 11, 0)
    turns = [
        _turn("user", "[user] first", ts=t2),
        _turn("assistant", "[assistant] reply", ts=t3),
        _turn("user", "[user] earlier", ts=t1),
    ]
    sig = extract_activity_signals(turns)
    assert sig.session_active_since == t1


def test_session_active_since_none_when_no_timestamps():
    turns = [_turn("user", "[user] hello")]
    assert extract_activity_signals(turns).session_active_since is None


# ── Empty / edge cases ────────────────────────────────────────────────────────

def test_empty_turns():
    sig = extract_activity_signals([])
    assert sig.files_recently_touched == []
    assert sig.inferred_branch == ""
    assert sig.last_user_intent == ""
    assert sig.primary_activity == "unknown"
    assert sig.session_active_since is None


def test_cwd_used_for_active_repo_when_no_files():
    sig = extract_activity_signals([], cwd="/home/dev/projects/auth-service")
    assert sig.active_repo == "auth-service"


def test_infer_repo_skips_src_dir():
    turns = [_turn("tool_use", '"file_path": "/projects/auth-service/src/auth.py"', tool="Edit")]
    sig = extract_activity_signals(turns)
    assert sig.active_repo == "auth-service"


def test_infer_repo_skips_tests_dir():
    turns = [_turn("tool_use", '"file_path": "/projects/my-app/tests/test_login.py"', tool="Read")]
    sig = extract_activity_signals(turns)
    assert sig.active_repo == "my-app"


def test_no_false_positive_version_number():
    # "1.2.3" should NOT appear as a file path
    turns = [_turn("assistant", "[assistant] version 1.2.3 is required")]
    sig = extract_activity_signals(turns)
    assert sig.files_recently_touched == []
