"""Few-shot example selector: turn recorded edit-diffs into prompt context.

When an agent is about to draft a new artifact, we pull the dev's most recent
approved edits of the same type and inject them as examples. The agent sees
"here's what you proposed, here's what the dev actually kept" and the next
draft drifts toward the dev's voice.

Design notes:
  - Pure local read — never calls out.
  - Bounded: cap by example count AND total characters so the prompt doesn't
    balloon with a decade of edits.
  - Deterministic order: newest first, so the model weights recent taste.
  - Opt-in: ``EditDiffStore`` defaults to ``None`` on agents that don't want
    this yet; tests stay stable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ghost_daemon.insights.edit_store import EditDiff, EditDiffStore

logger = logging.getLogger(__name__)

# Caps chosen to keep injected context under ~1.5k tokens total.
_DEFAULT_MAX_EXAMPLES = 3
_DEFAULT_MAX_CHARS = 2400
_PER_EXAMPLE_PAYLOAD_CHARS = 350


@dataclass
class FewShotExample:
    """One (before, after) pair ready for prompt injection."""
    proposed: str          # JSON-string of what the agent originally wrote
    approved: str          # JSON-string of what the dev actually kept
    changed_fields: list[str]
    approved_at: str

    def render(self, idx: int) -> str:
        """Format this example for a system prompt."""
        return (
            f"EXAMPLE {idx}:\n"
            f"You proposed:\n{self.proposed}\n\n"
            f"The developer approved:\n{self.approved}\n\n"
            f"Changed fields: {', '.join(self.changed_fields) or '(none)'}"
        )


def select_examples(
    store: EditDiffStore | None,
    artifact_type: str,
    *,
    max_examples: int = _DEFAULT_MAX_EXAMPLES,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> list[FewShotExample]:
    """Return up to ``max_examples`` recent edits of ``artifact_type``.

    Returns an empty list if the store is missing, has no edits, or if the
    resulting prompt chunk would exceed ``max_chars`` — in that case we yield
    nothing rather than a partial, confusing context.
    """
    if store is None:
        return []
    try:
        recent = store.read_recent(limit=max_examples * 4)
    except Exception as exc:
        logger.debug("few_shot: read_recent failed: %s", exc)
        return []

    matching = [d for d in recent if d.artifact_type == artifact_type]
    if not matching:
        return []

    examples: list[FewShotExample] = []
    running_chars = 0
    for diff in matching[:max_examples]:
        proposed = _compact_json(diff.original, _PER_EXAMPLE_PAYLOAD_CHARS)
        approved = _compact_json(diff.edited, _PER_EXAMPLE_PAYLOAD_CHARS)
        running_chars += len(proposed) + len(approved)
        if running_chars > max_chars:
            break
        examples.append(FewShotExample(
            proposed=proposed,
            approved=approved,
            changed_fields=list(diff.changed_fields),
            approved_at=diff.approved_at,
        ))
    return examples


def render_system_suffix(examples: list[FewShotExample]) -> str:
    """Produce the text to append to an agent's base system prompt.

    Returns an empty string when there are no examples, so callers can
    unconditionally concatenate without a conditional branch.
    """
    if not examples:
        return ""

    blocks = [ex.render(i + 1) for i, ex in enumerate(examples)]
    body = "\n\n".join(blocks)
    return (
        "\n\n---\n"
        "Here are recent cases where this developer edited your drafts "
        "before approving. Match their observed preferences (phrasing, "
        "brevity, structure) in your next draft:\n\n"
        f"{body}\n"
    )


def _compact_json(obj: dict | EditDiff, max_chars: int) -> str:
    """Serialise ``obj`` to compact JSON, truncating gracefully on overflow."""
    try:
        s = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    except TypeError:
        s = str(obj)
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."
