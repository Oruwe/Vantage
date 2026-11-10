"""Localhost guard middleware for the ghost UI.

The ghost-daemon binds to 127.0.0.1 by default, which stops a remote attacker
from connecting directly. It does NOT stop:

1. DNS rebinding — the browser visits evil.example, which returns JS, then
   flips its DNS record to 127.0.0.1. The browser's same-origin policy still
   considers the destination "evil.example" so the attacker's JS can now talk
   to the local daemon. The `Host:` header the browser sends is `evil.example`
   (that's the name it "thinks" it's contacting). Defense: reject any request
   whose Host header isn't one of our known local names.

2. CSRF — a tab the user already has open on evil.example runs
   `fetch("http://127.0.0.1:7474/draft/x/approve", {method:"POST", ...})`.
   Modern browsers send an `Origin` header on cross-origin writes. Defense:
   reject state-changing methods whose Origin (when present) isn't local.
   Missing Origin is allowed so the CLI, curl, and server-to-server use cases
   keep working — browsers always set Origin on cross-origin writes.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger(__name__)


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# "testserver" is Starlette TestClient's default Host. We allow it so unit
# tests that hit the app in-process don't have to spoof a Host header. A
# public attacker cannot cause a browser to rebind a bare single-label name
# like "testserver" to 127.0.0.1 — browsers only resolve names via the OS
# resolver, and the attacker has no record to serve for "testserver".
_ALWAYS_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})

_WILDCARD_BIND = frozenset({"0.0.0.0", "::", ""})


def _host_only(raw: str) -> str:
    """Strip port + IPv6 brackets from a Host or Origin hostname."""
    if not raw:
        return ""
    s = raw.strip().lower()
    # Origin header includes scheme, e.g. "http://127.0.0.1:7474"
    if "://" in s:
        s = urlparse(s).hostname or ""
    # IPv6 literal: "[::1]:7474"
    if s.startswith("["):
        end = s.find("]")
        if end != -1:
            return s[1:end]
    # IPv4 or hostname with port: "127.0.0.1:7474"
    if ":" in s and s.count(":") == 1:
        s = s.split(":", 1)[0]
    return s


def _allowed_hosts(bind_address: str) -> frozenset[str]:
    """Hosts we accept Host/Origin headers for.

    Always includes the loopback names. Also includes the configured bind
    address unless it's a wildcard (0.0.0.0, ::) — a wildcard bind doesn't
    identify a specific name the daemon owns.
    """
    extra = set()
    b = (bind_address or "").strip().lower()
    if b and b not in _WILDCARD_BIND:
        extra.add(b)
    return _ALWAYS_ALLOWED_HOSTS | extra


def install_local_guard(app: FastAPI, bind_address: str) -> None:
    allowed = _allowed_hosts(bind_address)

    @app.middleware("http")
    async def _local_guard(request: Request, call_next):
        host_header = request.headers.get("host", "")
        host = _host_only(host_header)
        if host not in allowed:
            logger.warning("Rejected request with Host=%r (allowed=%s)", host_header, sorted(allowed))
            return JSONResponse(
                {"error": "invalid Host header"},
                status_code=400,
            )

        if request.method in _WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                # "null" is the literal origin browsers send for sandboxed or
                # file:// contexts — treat it as cross-origin and refuse.
                if origin.strip().lower() == "null":
                    logger.warning("Rejected %s with Origin=null", request.method)
                    return JSONResponse(
                        {"error": "cross-origin write refused"},
                        status_code=403,
                    )
                origin_host = _host_only(origin)
                if origin_host not in allowed:
                    logger.warning(
                        "Rejected %s with Origin=%r (host=%r)", request.method, origin, origin_host,
                    )
                    return JSONResponse(
                        {"error": "cross-origin write refused"},
                        status_code=403,
                    )

        return await call_next(request)
