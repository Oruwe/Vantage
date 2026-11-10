"""Deterministic mock LLM provider for the offline demo and tests.

Returns realistic-looking drafts (JSON matching the agent system prompts)
based on simple prompt-shape detection. No network, no API key, no latency.

`embed` returns a stable 768-dim hash of the input so semantic search stays
functional on the demo without an embedder.
"""
from __future__ import annotations

import hashlib
import json
import struct


_COMMIT_REPORT = {
    "generated_summary": (
        "Refactored the request-signing path and tightened the body-signature "
        "verification on the push endpoint. Added a regression test and "
        "updated the auth-model docs."
    ),
    "open_questions": [
        "Should duplicate pushes also skip the wiretap publish?",
    ],
}

_WEEKLY_DIGEST = {
    "highlights": [
        "Shipped hardened push auth: body signatures are now required and duplicates short-circuit notifications.",
        "Added a localhost guard on the ghost UI covering DNS rebinding and CSRF.",
        "Rewrote the security docs so they match the code.",
    ],
    "commits": [
        "receive: require X-Vantage-Signature",
        "local_guard: Host + Origin middleware",
        "docs/SECURITY.md",
    ],
}

_STANDUP = {
    "yesterday": [
        "Finished the signed-push hardening and the dedup guard.",
        "Wrote the ghost-UI localhost guard and ten tests around it.",
    ],
    "today": [
        "Write the fresh-clone install smoke test.",
        "Start on the offline demo mode.",
    ],
    "blockers": [],
}

_PR_DRAFT = {
    "title": "Harden /v1/receive and the ghost UI",
    "summary": (
        "Three small security fixes: require the body signature on push, "
        "short-circuit duplicate pushes before notifications fire, and reject "
        "non-local Host and Origin headers on the ghost UI."
    ),
    "rationale": (
        "The earlier auth model treated the body signature as optional and the "
        "ghost UI trusted any Host header. Both were gaps the architecture "
        "report claimed were already closed."
    ),
}


class MockLLMProvider:
    """Returns canned JSON drafts shaped like the real agent prompts."""

    model_id = "mock"

    async def complete(self, messages: list[dict], *, max_tokens: int = 600) -> str:
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        blob = (system + "\n" + user).lower()

        if "commit report" in system.lower() or "commit report" in blob:
            return json.dumps(_COMMIT_REPORT)
        if "weekly digest" in blob or '"highlights"' in system:
            return json.dumps(_WEEKLY_DIGEST)
        if "standup" in blob or '"yesterday"' in system:
            return json.dumps(_STANDUP)
        if "pr draft" in blob or '"title"' in system and '"summary"' in system:
            return json.dumps(_PR_DRAFT)

        # Fallback: a short free-text answer. Used by the manager-agent chat
        # path when it isn't holding a JSON system prompt.
        return (
            "Mock provider: demo mode is active, so I'm returning a canned "
            "answer rather than calling a real model. In production this would "
            "be a real LLM response grounded in the artifacts in the store."
        )

    async def embed(self, text: str) -> list[float]:
        # Deterministic hash-based 768-dim vector. Semantic search becomes
        # effectively exact-match (same text → same vector) which is fine for
        # demo purposes and identical to how a real embedder is wired.
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).digest()
        # Expand to 768*4 = 3072 bytes, then unpack as 768 float32
        buf = (digest * (3072 // len(digest) + 1))[:3072]
        return list(struct.unpack("768f", buf))
