from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Business-event counters ─────────────────────────────────────────────────

artifacts_received = Counter(
    "vantage_artifacts_received_total",
    "Total artifacts received from dev daemons",
    ["developer_id", "channel"],
)

probe_requests = Counter(
    "vantage_probe_requests_total",
    "Total probe requests processed",
    ["developer_id", "decision"],
)

timeline_requests = Counter(
    "vantage_timeline_requests_total",
    "Total timeline GET requests",
    ["developer_id"],
)

dashboard_requests = Counter(
    "vantage_dashboard_requests_total",
    "Total dashboard GET requests",
)

ws_connections_active = Gauge(
    "vantage_ws_connections_active",
    "Number of currently active dev daemon WebSocket connections",
)

probe_latency = Histogram(
    "vantage_probe_latency_seconds",
    "Time to complete a live probe (excluding offline fallback)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0],
)

eval_run_total = Counter(
    "vantage_eval_run_total",
    "Eval scenario outcomes. Labels distinguish suite (scrubber / manager_agent) and outcome (pass / fail / error / skip).",
    ["suite", "outcome"],
)

# ── HTTP request-path metrics ───────────────────────────────────────────────
#
# ``route`` is the endpoint path *template* (e.g. ``/v1/timeline/{developer_id}``),
# NOT the raw request path. Low cardinality is table-stakes for Prometheus;
# raw paths with user ids would blow label-set size apart.

http_requests_total = Counter(
    "vantage_http_requests_total",
    "HTTP requests received, labeled by route template, method, and status code bucket.",
    ["route", "method", "status"],
)

http_request_duration_seconds = Histogram(
    "vantage_http_request_duration_seconds",
    "HTTP request latency including handler + any middleware below us.",
    ["route", "method"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── LLM call metrics ────────────────────────────────────────────────────────

llm_call_duration_seconds = Histogram(
    "vantage_llm_call_duration_seconds",
    "Latency of a single LLM call (``complete`` / ``messages.create``).",
    ["provider", "model", "operation"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0],
)

llm_tokens_total = Counter(
    "vantage_llm_tokens_total",
    "Tokens accounted to each LLM call. kind=input|output|cache_read|cache_create.",
    ["provider", "model", "kind"],
)

llm_calls_total = Counter(
    "vantage_llm_calls_total",
    "LLM calls observed, by outcome. outcome=ok|error.",
    ["provider", "model", "operation", "outcome"],
)

# ── Vector-search / storage metrics ────────────────────────────────────────

qdrant_op_duration_seconds = Histogram(
    "vantage_qdrant_op_duration_seconds",
    "Qdrant operation latency (op=search|upsert|scroll|get_collection).",
    ["op", "outcome"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

embedder_duration_seconds = Histogram(
    "vantage_embedder_duration_seconds",
    "Embedder.embed latency per call.",
    ["backend", "outcome"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ── Agent tool-call metrics ─────────────────────────────────────────────────

agent_tool_call_duration_seconds = Histogram(
    "vantage_agent_tool_call_duration_seconds",
    "Duration of a manager-agent tool call, excluding the LLM turn around it.",
    ["tool_name", "outcome"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

agent_tool_calls_total = Counter(
    "vantage_agent_tool_calls_total",
    "Agent tool invocations. outcome=ok|error.",
    ["tool_name", "outcome"],
)
