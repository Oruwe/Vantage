from __future__ import annotations

import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from shadow_replica.config import TrustedDev


def verify_request(request: Request, body: bytes, trusted_devs: list["TrustedDev"]) -> "TrustedDev":
    """Verify the Authorization bearer token and body HMAC signature.

    Returns the matched TrustedDev on success.
    Raises HTTPException(401) if auth fails.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    provided_token = auth_header.removeprefix("Bearer ").strip()

    matched: TrustedDev | None = None
    for dev in trusted_devs:
        if hmac.compare_digest(dev.token_value, provided_token):
            matched = dev
            break

    if matched is None:
        raise HTTPException(status_code=401, detail="Unknown or revoked token")

    sig_header = request.headers.get("X-Vantage-Signature", "")
    if sig_header:
        expected_sig = "sha256=" + hmac.new(
            matched.token_value.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected_sig):
            raise HTTPException(status_code=401, detail="Body signature mismatch")

    return matched
