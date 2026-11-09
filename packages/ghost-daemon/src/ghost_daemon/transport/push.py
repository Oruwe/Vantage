from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import TYPE_CHECKING

import httpx

from vantage.schemas import ApprovedPayload, SubscriberIdentity

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _new_trace_id() -> str:
    """12 hex chars. Matches the replica's mint format so log scraping is
    symmetric across the push → receive boundary. Called ``trace_id`` here
    to stay semantically distinct from the probe-protocol ``request_id`` on
    the replica side (different layer, different lifecycle)."""
    return secrets.token_hex(6)


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
    trace_id = _new_trace_id()

    headers = {
        "Authorization": f"Bearer {subscriber.token_value}",
        "X-Vantage-Developer-Id": developer_id,
        "X-Vantage-Signature": sig,
        "X-Request-Id": trace_id,
        "Content-Type": "application/json",
    }

    receive_url = subscriber.receive_url.rstrip("/") + "/v1/receive"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(receive_url, content=body, headers=headers)
        if resp.is_success:
            logger.info(
                "Pushed %s to subscriber %s (%s) — %d [trace=%s]",
                approved.approval_id,
                subscriber.name,
                subscriber.subscriber_id,
                resp.status_code,
                trace_id,
            )
            return True
        logger.warning(
            "Push to subscriber %s failed: %d %s [trace=%s]",
            subscriber.subscriber_id,
            resp.status_code,
            resp.text[:200],
            trace_id,
        )
        return False
    except Exception as exc:
        logger.warning(
            "Push to subscriber %s error: %s [trace=%s]",
            subscriber.subscriber_id, exc, trace_id,
        )
        return False
