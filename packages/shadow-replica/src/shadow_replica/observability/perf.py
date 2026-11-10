"""At-a-glance performance summary derived from the Prometheus registry.

Reads the current counter/histogram state and returns a small dict the
dashboard can render in one card. Not a replacement for Prometheus +
Grafana — this is the "is anything on fire right now" view a manager
can glance at without leaving the Vantage UI.

Percentile estimation uses linear interpolation inside cumulative buckets.
Precision is bounded by the histogram's bucket boundaries (chosen in
``metrics.py``); good enough for ops smoke-checks, not for SLO math.
"""
from __future__ import annotations

from typing import Any

from shadow_replica import metrics as m


def _buckets_from_histogram(hist) -> tuple[list[tuple[float, float]], float, float]:
    """Flatten a Histogram collector into (sorted_buckets, sum, count).

    ``sorted_buckets`` is ``[(le, cumulative_count), ...]`` across all
    label sets — we aggregate when we want an all-up view. The caller
    can then compute percentiles via linear interpolation.
    """
    cumulative: dict[float, float] = {}
    total_sum = 0.0
    total_count = 0.0
    for metric in hist.collect():
        for s in metric.samples:
            if s.name.endswith("_bucket"):
                le_raw = s.labels.get("le", "")
                try:
                    le = float("inf") if le_raw == "+Inf" else float(le_raw)
                except ValueError:
                    continue
                cumulative[le] = cumulative.get(le, 0.0) + float(s.value)
            elif s.name.endswith("_sum"):
                total_sum += float(s.value)
            elif s.name.endswith("_count"):
                total_count += float(s.value)
    pairs = sorted(cumulative.items(), key=lambda p: p[0])
    return pairs, total_sum, total_count


def _percentile(buckets: list[tuple[float, float]], total_count: float, p: float) -> float | None:
    """Estimate the p-percentile of a histogram via linear bucket interpolation.

    Returns ``None`` when there's no data yet (operators get a tidy '—' instead
    of a bogus zero). Uses the last finite bucket as an upper bound for the
    overflow tail — not perfect, but a sane floor for a first-glance summary.
    """
    if total_count <= 0 or not buckets:
        return None
    target = p * total_count
    prev_le = 0.0
    prev_cum = 0.0
    last_finite_le = None
    for le, cum in buckets:
        if le != float("inf"):
            last_finite_le = le
        if cum >= target:
            if le == float("inf"):
                return last_finite_le
            width = le - prev_le
            denom = cum - prev_cum
            if denom <= 0:
                return le
            return prev_le + width * (target - prev_cum) / denom
        prev_le = le if le != float("inf") else prev_le
        prev_cum = cum
    return last_finite_le


def _summarize(hist, *, as_ms: bool = True) -> dict[str, Any]:
    buckets, total_sum, total_count = _buckets_from_histogram(hist)
    scale = 1000.0 if as_ms else 1.0
    unit = "ms" if as_ms else "s"
    p50 = _percentile(buckets, total_count, 0.5)
    p95 = _percentile(buckets, total_count, 0.95)
    p99 = _percentile(buckets, total_count, 0.99)
    return {
        "count": int(total_count),
        "unit": unit,
        "p50": round(p50 * scale, 2) if p50 is not None else None,
        "p95": round(p95 * scale, 2) if p95 is not None else None,
        "p99": round(p99 * scale, 2) if p99 is not None else None,
        "avg": round(total_sum / total_count * scale, 2) if total_count else None,
    }


def _counter_sum(counter) -> int:
    total = 0.0
    for metric in counter.collect():
        for s in metric.samples:
            if s.name.endswith("_total"):
                total += float(s.value)
    return int(total)


def build_perf_summary() -> dict[str, Any]:
    """Return the all-up performance snapshot for the dashboard card."""
    return {
        "http": _summarize(m.http_request_duration_seconds),
        "probe": _summarize(m.probe_latency),
        "llm": _summarize(m.llm_call_duration_seconds),
        "qdrant": _summarize(m.qdrant_op_duration_seconds),
        "embedder": _summarize(m.embedder_duration_seconds),
        "tool_call": _summarize(m.agent_tool_call_duration_seconds),
        "counters": {
            "http_requests": _counter_sum(m.http_requests_total),
            "artifacts_received": _counter_sum(m.artifacts_received),
            "probe_requests": _counter_sum(m.probe_requests),
            "timeline_requests": _counter_sum(m.timeline_requests),
            "dashboard_requests": _counter_sum(m.dashboard_requests),
            "llm_calls": _counter_sum(m.llm_calls_total),
            "agent_tool_calls": _counter_sum(m.agent_tool_calls_total),
        },
    }
