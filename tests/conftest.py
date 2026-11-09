"""Test-suite fast-path.

The default dev-loop goal is <2 min for the full suite. Two things dominated
the slow run:

  1. QdrantStore / ReceivedStore / Embedder probe their backends (Qdrant,
     Ollama) on first use. Each probe eats a 5–10s timeout when the backend
     is down. Dozens of tests hit this.
  2. No parallelism — single-process pytest on Windows is slow per-test.

Fix (1) by stubbing the probes to fail fast. Fix (2) by running under
pytest-xdist (``-n auto`` in pyproject.toml).

Tests that *do* want Qdrant/Ollama should use the ``real_backends`` marker;
we skip the fast-path monkeypatches when a test opts in.
"""
from __future__ import annotations

import os
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register markers used by the suite."""
    config.addinivalue_line(
        "markers",
        "slow: integration tests that exercise real I/O or long waits — "
        "skipped by default, run with `pytest -m slow`.",
    )
    config.addinivalue_line(
        "markers",
        "real_backends: test wants real Qdrant/Ollama/remote endpoints. "
        "Skips the fast-path mocks.",
    )
    config.addinivalue_line(
        "markers",
        "llm_eval: test that calls a real LLM endpoint. Costs money per run. "
        "Skipped by default; opt in with `pytest -m llm_eval` AND set "
        "ANTHROPIC_API_KEY. Gate to main-branch CI only.",
    )

    # Disable the `anonymized_telemetry` opt-in that qdrant-client prints on
    # first use — pure noise in CI.
    os.environ.setdefault("QDRANT_CLIENT_FORCE_DISABLE_CHECK_VERSION", "1")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``slow`` unless explicitly selected with ``-m slow``.

    Without this, every ``pytest tests/`` run pays for the big integration
    tests. CI's main-branch job can override by passing ``-m "slow or not slow"``.
    """
    markexpr = config.getoption("markexpr", default="") or ""
    if "slow" not in markexpr:
        skip_slow = pytest.mark.skip(reason="slow test — run with `pytest -m slow`")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)
    if "llm_eval" not in markexpr:
        skip_llm = pytest.mark.skip(reason="llm_eval — run with `pytest -m llm_eval` (costs $)")
        for item in items:
            if "llm_eval" in item.keywords:
                item.add_marker(skip_llm)


# ── Fast-path fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_qdrant_and_embedder(request: pytest.FixtureRequest, monkeypatch) -> None:
    """Short-circuit the two backends whose probes dominate test time.

    Tests marked ``real_backends`` opt out and get the real client.
    """
    if request.node.get_closest_marker("real_backends") is not None:
        return

    # ── ghost-daemon QdrantStore ──────────────────────────────────────────
    try:
        from ghost_daemon.storage import qdrant_store as _ghost_q

        def _ghost_ensure_false(self) -> bool:
            # Mimic the "available once probed False" state so no retries happen.
            self._client = None
            return False

        monkeypatch.setattr(_ghost_q.QdrantStore, "_ensure_client", _ghost_ensure_false)
    except ImportError:
        pass

    # ── shadow-replica ReceivedStore ──────────────────────────────────────
    try:
        from shadow_replica.storage import received_store as _rep_s

        def _rep_ensure_false(self) -> bool:
            self._client = None
            return False

        monkeypatch.setattr(_rep_s.ReceivedStore, "_ensure_client", _rep_ensure_false)
    except ImportError:
        pass

    # ── shadow-replica Embedder (Ollama/OpenAI probe) ─────────────────────
    try:
        from shadow_replica.models import embedder as _rep_emb

        _orig_init = _rep_emb.Embedder.__init__

        def _fast_init(self, *args, **kwargs):
            _orig_init(self, *args, **kwargs)
            # Pre-mark unavailable so .embed() returns zeros without network.
            self._available = False

        monkeypatch.setattr(_rep_emb.Embedder, "__init__", _fast_init)
    except ImportError:
        pass
