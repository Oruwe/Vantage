"""Tests for the A2A schema + WiretapBus (shadow-replica side)."""
from __future__ import annotations

import asyncio
import json

import pytest

from shadow_replica.wiretap.bus import WiretapBus
from vantage.a2a import A2AAgentRef, A2AEnvelope, A2AIntent


def _env(intent=A2AIntent.PROBE_REQUEST, summary="t"):
    return A2AEnvelope(
        intent=intent,
        from_agent=A2AAgentRef(kind="shadow-replica", label="replica"),
        to_agent=A2AAgentRef(kind="ghost-daemon", developer_id="dev1", label="dev1"),
        summary=summary,
    )


def test_envelope_defaults_json_roundtrip():
    env = _env()
    data = env.model_dump(mode="json")
    assert data["intent"] == "probe_request"
    assert data["turn_id"]
    assert data["status"] == "ok"
    # survives JSON round-trip
    revived = A2AEnvelope(**json.loads(env.model_dump_json()))
    assert revived.turn_id == env.turn_id
    assert revived.intent == env.intent


def test_bus_recent_returns_latest():
    bus = WiretapBus(ring_cap=5)
    envs = [_env(summary=f"n{i}") for i in range(7)]
    for e in envs:
        bus.publish(e)
    recent = bus.recent(limit=3)
    assert [r.summary for r in recent] == ["n4", "n5", "n6"]
    assert len(bus.recent(limit=100)) == 5  # capped by ring


def test_bus_recent_empty_when_limit_zero():
    bus = WiretapBus()
    bus.publish(_env())
    assert bus.recent(limit=0) == []


@pytest.mark.anyio
async def test_bus_subscribe_receives_published():
    bus = WiretapBus()
    sub = bus.subscribe()
    received: list[A2AEnvelope] = []

    async def pump():
        async for env in sub:
            received.append(env)
            if len(received) >= 2:
                break

    task = asyncio.create_task(pump())
    await asyncio.sleep(0.01)  # let subscriber register
    bus.publish(_env(summary="first"))
    bus.publish(_env(summary="second"))
    await asyncio.wait_for(task, timeout=1.0)
    assert [e.summary for e in received] == ["first", "second"]


@pytest.mark.anyio
async def test_bus_subscriber_set_cleaned_up_on_aclose():
    """Closing the async-gen fires the finally that removes the subscriber."""
    bus = WiretapBus()
    sub = bus.subscribe()

    async def consume():
        async for _ in sub:
            return  # exit on first message

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    assert len(bus._subscribers) == 1
    bus.publish(_env())
    await asyncio.wait_for(task, timeout=1.0)
    await sub.aclose()  # emulate SSE connection drop
    await asyncio.sleep(0.01)
    assert len(bus._subscribers) == 0


@pytest.mark.anyio
async def test_bus_slow_consumer_drops_rather_than_blocks():
    """A subscriber that never reads must not block the publisher."""
    bus = WiretapBus()
    _ = bus.subscribe()  # register but never iterate
    await asyncio.sleep(0.01)
    for i in range(1000):  # way beyond queue cap
        bus.publish(_env(summary=f"n{i}"))
    # Publisher is sync — if it blocked we'd never get here. Ring still bounded.
    assert len(bus.recent(limit=10000)) <= 500
