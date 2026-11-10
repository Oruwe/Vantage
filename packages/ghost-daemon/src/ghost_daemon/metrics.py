"""Prometheus metrics for the ghost-daemon.

Separate registry from the replica (different process, different port), but
the series names deliberately mirror the replica's where the semantics
match — so a single Grafana dashboard can slice by ``job`` and compare
ghost vs replica pushes/LLMs/embeddings side by side.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram

# ── Push path ───────────────────────────────────────────────────────────────

ghost_pushes_total = Counter(
    "vantage_ghost_pushes_total",
    "Ghost-daemon pushes to subscribers. outcome=ok|error|timeout.",
    ["subscriber", "outcome"],
)

ghost_push_duration_seconds = Histogram(
    "vantage_ghost_push_duration_seconds",
    "Latency of a single ghost→replica push (request send + response read).",
    ["subscriber", "outcome"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── LLM path (mirrors the replica series names) ─────────────────────────────

llm_call_duration_seconds = Histogram(
    "vantage_ghost_llm_call_duration_seconds",
    "Ghost-side LLM call latency (``complete``). Labels: provider/model/operation.",
    ["provider", "model", "operation"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0],
)

llm_tokens_total = Counter(
    "vantage_ghost_llm_tokens_total",
    "Tokens accounted to each ghost-side LLM call. kind=input|output.",
    ["provider", "model", "kind"],
)

llm_calls_total = Counter(
    "vantage_ghost_llm_calls_total",
    "Ghost-side LLM calls observed. outcome=ok|error.",
    ["provider", "model", "operation", "outcome"],
)

# ── Embedder path ───────────────────────────────────────────────────────────

embedder_duration_seconds = Histogram(
    "vantage_ghost_embedder_duration_seconds",
    "Ghost-side embedder.embed latency per call.",
    ["backend", "outcome"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ── Scrubber path ───────────────────────────────────────────────────────────

scrubber_duration_seconds = Histogram(
    "vantage_ghost_scrubber_duration_seconds",
    "Latency of a single scrub() call over one chunk of session content.",
    buckets=[0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
