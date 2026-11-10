from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING

import httpx

from ghost_daemon.metrics import ghost_push_duration_seconds, ghost_pushes_total
from ghost_daemon.observability import current_trace_id, new_trace_id, use_trace_id
from vantage.schemas import ApprovedPayload, SubscriberIdentity

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _body_signature(body: bytes, token_value: str) -> str:
    """HMAC-SHA256 of body keyed with the token value."""
    key = token_value.encode()
    sig = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


async def push(
    approved: ApprovedPayload,
    subscriber: SubscriberIdentity,
    developer_id: str,
) -> bool:
    """POST an approved artifact to a subscriber's receive endpoint.

    Returns True on success (2xx), False on any error.
    Logs failures but does not raise — callers should not be blocked by push failures.
    """
    body = approved.model_dump_json().encode()
    sig = _body_signature(body, subscriber.token_value)

    # Reuse an inherited trace_id (e.g. from an inbound UI request) when present;
    # otherwise mint a fresh one so the push still appears correlated in logs.
    inherited = current_trace_id()
    trace_id = inherited if inherited and inherited != "-" else new_trace_id()

    headers = {
        "Authorization": f"Bearer {subscriber.token_value}",
        "X-Vantage-Developer-Id": developer_id,
        "X-Vantage-Signature": sig,
        "X-Request-Id": trace_id,
        "Content-Type": "application/json",
    }

    receive_url = subscriber.receive_url.rstrip("/") + "/v1/receive"

    sub_label = subscriber.subscriber_id
    with use_trace_id(trace_id):
        t0 = time.monotonic()
        outcome = "error"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(receive_url, content=body, headers=headers)
            if resp.is_success:
                outcome = "ok"
                logger.info(
                    "Pushed %s to subscriber %s (%s) — %d",
                    approved.approval_id,
                    subscriber.name,
                    subscriber.subscriber_id,
                    resp.status_code,
                )
                return True
            outcome = f"{resp.status_code // 100}xx"
            logger.warning(
                "Push to subscriber %s failed: %d %s",
                subscriber.subscriber_id,
                resp.status_code,
                resp.text[:200],
            )
            return False
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            logger.warning(
                "Push to subscriber %s timed out: %s",
                subscriber.subscriber_id, exc,
            )
            return False
        except Exception as exc:
            outcome = "error"
            logger.warning(
                "Push to subscriber %s error: %s",
                subscriber.subscriber_id, exc,
            )
            return False
        finally:
            elapsed = time.monotonic() - t0
            ghost_push_duration_seconds.labels(
                subscriber=sub_label, outcome=outcome,
            ).observe(elapsed)
            ghost_pushes_total.labels(subscriber=sub_label, outcome=outcome).inc()
