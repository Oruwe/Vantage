"""Tests for the Deja-Vu detector."""
from __future__ import annotations

from ghost_daemon.agents.dejavu_detector import detect_resolutions, normalize_error
from vantage.schemas import Turn


def _t(role: str, content: str, tool: str = ""):
    return Turn(role=role, content_excerpt=content, tool_name=tool or None)


def test_normalize_strips_paths_lineno_uuid_hex():
    s1 = normalize_error("Error: cannot find module '/home/user/project/foo.js' line 42")
    s2 = normalize_error("Error: cannot find module '/opt/other/path/foo.js' line 999")
    assert s1 == s2
    s3 = normalize_error("Fatal: process 2718aafd-1234-5678-90ab-abcdef012345 pid: 4567 died")
    assert "<uuid>" in s3 and "<n>" in s3


def test_pattern_struggle_then_success_captures_resolution():
    # Note: all three failing turns share the same structural error shape
    # (same markers, equivalent after normalize_error strips varying paths /
    # line numbers / PIDs). The detector groups them into one bucket.
    turns = [
        _t("tool", "command failed with exit code 1\nError: cannot find module 'foo' at line 10", tool="Bash"),
        _t("tool", "command failed with exit code 1\nError: cannot find module 'foo' at line 12", tool="Bash"),
        _t("tool", "command failed with exit code 1\nError: cannot find module 'foo' at line 15", tool="Bash"),
        _t("tool", '{"file_path": "/repo/package.json"}', tool="Edit"),
        _t("tool", "installed foo@1.2.3 — all good", tool="Bash"),
    ]
    rez = detect_resolutions(turns, developer_id="alice", repo="demo")
    assert len(rez) == 1
    r = rez[0]
    assert r.attempt_count == 3
    assert r.developer_id == "alice"
    assert r.repo == "demo"
    assert "cannot find module" in r.error_signature
    assert "package.json" in r.files_touched
    assert "Bash" in r.tools_used or "Edit" in r.tools_used


def test_pattern_below_minimum_attempts_ignored():
    turns = [
        _t("tool", "Error: cannot find module 'foo'", tool="Bash"),
        _t("tool", "installed foo", tool="Bash"),  # success after only 1 failure
    ]
    assert detect_resolutions(turns, developer_id="alice") == []


def test_two_separate_resolutions_in_one_session():
    turns = [
        # First struggle: module not found
        _t("tool", "Error: cannot find module 'foo'", tool="Bash"),
        _t("tool", "Error: cannot find module 'foo'", tool="Bash"),
        _t("tool", '{"file_path": "/repo/package.json"}', tool="Edit"),
        _t("tool", "installed foo — ok", tool="Bash"),
        # Second struggle: typo
        _t("tool", "Error: ReferenceError xyz is not defined", tool="Bash"),
        _t("tool", "Error: ReferenceError xyz is not defined", tool="Bash"),
        _t("tool", '{"file_path": "/repo/src/main.ts"}', tool="Edit"),
    ]
    rez = detect_resolutions(turns, developer_id="alice")
    assert len(rez) == 2
    sigs = {r.error_signature for r in rez}
    assert any("cannot find module" in s for s in sigs)
    assert any("referenceerror" in s or "not defined" in s for s in sigs)


def test_resolution_fix_summary_is_bounded():
    turns = [
        _t("tool", "Error: X failed", tool="Bash"),
        _t("tool", "Error: X failed", tool="Bash"),
        _t("tool", '{"file_path": "/a/b/c/my_module.py"}', tool="Edit"),
    ]
    r = detect_resolutions(turns, developer_id="dev")[0]
    assert r.fix_summary
    assert len(r.fix_summary) <= 160
    assert "my_module.py" in r.fix_summary
