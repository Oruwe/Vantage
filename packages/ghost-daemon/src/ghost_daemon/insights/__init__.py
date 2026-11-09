"""Local-only developer insights (never leave the ghost-daemon).

This package captures signals that help the agent improve for *this* developer:
  - edit diffs between agent-proposed drafts and dev-approved versions
  - scrubber-hit stats so the dev can see what their redactor caught

Nothing in here is ever pushed to the shadow-replica. It lives on the dev's
machine, under ``~/.vantage/``, and is readable only by the dev.
"""
from __future__ import annotations

from ghost_daemon.insights.collision_poller import (
    CollisionBundle,
    CollisionPoller,
    CollisionRecord,
)
from ghost_daemon.insights.edit_store import EditDiff, EditDiffStore
from ghost_daemon.insights.few_shot import (
    FewShotExample,
    render_system_suffix,
    select_examples,
)
from ghost_daemon.insights.flow_state import (
    ApprovalEvent,
    DeepWindow,
    FlowAnalyzer,
    FlowSnapshot,
    HourCell,
    StuckPattern,
)

__all__ = [
    "EditDiff",
    "EditDiffStore",
    "FewShotExample",
    "select_examples",
    "render_system_suffix",
    "ApprovalEvent",
    "DeepWindow",
    "FlowAnalyzer",
    "FlowSnapshot",
    "HourCell",
    "StuckPattern",
    "CollisionBundle",
    "CollisionPoller",
    "CollisionRecord",
]
