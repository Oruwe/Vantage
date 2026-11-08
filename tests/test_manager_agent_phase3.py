"""Phase 3 — dashboard chat UI (static assertions on index.html).

The SPA is a single-file vanilla-JS app. We verify the Agent view is wired
correctly without spinning up a headless browser: nav entry, routing hook,
render function, SSE fetch call, chunk handling, and keyboard shortcuts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_INDEX = Path(__file__).resolve().parents[1] / "packages" / "shadow-replica" / "src" / "shadow_replica" / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def test_agent_fab_entry_present(html):
    # Agent is accessed via a floating action button that follows the user
    # across every dashboard view (not a sidebar item).
    assert 'id="ag-fab"' in html
    assert 'onclick="toggleAgentFloat()"' in html
    # The pulse dot used while the agent is streaming moved onto the FAB.
    assert 'id="ag-fab-dot"' in html
    # And the panel itself must be a sibling of the main shell so it floats.
    assert 'id="ag-float"' in html
    assert 'id="ag-float-body"' in html


def test_agent_view_routed_via_float_panel(html):
    # The old nav('agent') hook still exists for deep-links / command palette,
    # but it now opens the floating panel instead of swapping #content.
    assert "if(view==='agent'){openAgentFloat();return;}" in html
    # renderAgent writes into the float body, not the main content area.
    assert "getElementById('ag-float-body')" in html


def test_agent_renders_dual_column_layout(html):
    assert "class=\"ag-wrap\"" in html
    assert "class=\"ag-side\"" in html
    assert "class=\"ag-main\"" in html
    # Sidebar must have a "new thread" button + list container
    assert 'id="ag-new"' in html
    assert 'id="ag-side-list"' in html
    # Main must have a feed + input + send
    assert 'id="ag-feed"' in html
    assert 'id="ag-input"' in html
    assert 'id="ag-send"' in html


def test_agent_sends_streaming_fetch_to_sse_endpoint(html):
    # Agent must POST to the SSE endpoint with Bearer auth
    assert "/ui/api/chat/stream" in html
    assert "'Accept': 'text/event-stream'" in html
    assert "AbortController()" in html


def test_agent_handles_all_sse_chunk_types(html):
    for name in ("started", "tool_call", "tool_result", "message_delta", "done", "error"):
        assert f"ev.event === '{name}'" in html, f"missing SSE handler for '{name}'"


def test_agent_lists_and_deletes_conversations(html):
    assert "GET'" in html.replace(" ", "")  # api('GET', …) exists somewhere
    assert "/chat/conversations" in html
    assert "agDeleteConversation" in html
    assert "/chat/conversations/${encodeURIComponent(id)}" in html


def test_agent_tool_call_card_template(html):
    # Expand/collapse interaction
    assert "ag-tc-head" in html
    assert "classList.toggle('expanded')" in html
    # Latency readout + running/done state swap
    assert "ag-tc-ms" in html
    assert "classList.add('done')" in html


def test_agent_keyboard_shortcuts_wired(html):
    # Agent-specific shortcuts gate on the floating panel being open, not
    # on S.view (the agent no longer owns the main content area).
    assert "p.classList.contains('open')" in html
    # Alt+A toggles the floating panel from anywhere.
    assert "e.altKey && (e.key==='a' || e.key==='A')" in html
    # `/` focuses, Esc blurs/aborts, `?` opens help
    assert "e.key === '/'" in html
    assert "e.key === 'Escape'" in html
    assert "e.key === '?'" in html
    # Enter sends, Shift+Enter newline
    assert "e.key === 'Enter' && !e.shiftKey" in html


def test_agent_help_modal_present(html):
    assert 'id="ag-help"' in html
    assert "agHelpOpen" in html
    assert "agHelpClose" in html


def test_agent_prefill_examples(html):
    # Starter prompts in the empty state
    assert "agPrefill" in html
    # And at least one domain example
    assert "Alice" in html


def test_agent_css_namespace_exists(html):
    for css_class in (".ag-wrap", ".ag-side", ".ag-main", ".ag-bubble", ".ag-tc", ".ag-help-modal"):
        assert css_class in html, f"missing CSS class {css_class}"


def test_agent_sse_parser_present(html):
    assert "agParseSSE" in html
    assert "line.startsWith('event: ')" in html
    assert "line.startsWith('data: ')" in html


def test_agent_streaming_state_flag(html):
    assert "AG.streaming" in html
    # Abort controller for Escape-to-cancel
    assert "AG.streamCtrl" in html
    assert "AG.streamCtrl.abort()" in html


def test_agent_float_panel_label_text(html):
    # Human-visible label on the floating panel header. If the design was
    # ever renamed, the FAB/renderer pair would still work but this would
    # flag the drift.
    assert "Manager AI" in html


def test_agent_budget_exhausted_user_feedback(html):
    assert "budget_exhausted" in html
    assert "budget exhausted" in html.lower()
