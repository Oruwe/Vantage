from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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
