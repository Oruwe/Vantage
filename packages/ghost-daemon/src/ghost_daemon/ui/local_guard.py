"""Keep the local approval UI reachable only by this machine.

The approval gate is Vantage's core promise, so the page that drives it must
not answer other websites. Two checks:

* **Host** (every request) — a DNS-rebinding page resolves its own hostname to
  127.0.0.1, but the browser still sends that hostname in ``Host``. Anything
  that is not a loopback name (or a host the developer explicitly configured)
  gets 400, so the page can neither read drafts nor approve them.
* **Origin** (state-changing requests) — a classic cross-site form POST carries
  ``Host: 127.0.0.1`` but ``Origin: https://other.site``. A present Origin whose
  host is not allowed, or the opaque ``null`` origin, gets 403. A missing Origin
  is allowed: browsers always send one on cross-site POSTs, so its absence
  means curl, a script, or the CLI.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# Starlette's TestClient sends ``Host: testserver``. It is a single-label name,
# so a public attacker cannot own it or rebind it; allowing it keeps the test
# suite working without weakening the guard.
_TEST_CLIENT_HOST = "testserver"
_WILDCARD_BINDS = frozenset({"", "0.0.0.0", "::"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def allowed_hosts(bind_address: str, extra: Iterable[str] = ()) -> frozenset[str]:
    hosts = set(_LOOPBACK) | {_TEST_CLIENT_HOST}
    if bind_address.strip().lower() not in _WILDCARD_BINDS:
        hosts.add(_normalise(bind_address))
    hosts.update(_normalise(h) for h in extra if h.strip())
    return frozenset(hosts)


def host_from_header(value: str) -> str:
    """Hostname from a Host header: strip the port and IPv6 brackets."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _normalise(host: str) -> str:
    return host.strip().lower().strip("[]")


def install_local_guard(app: FastAPI, bind_address: str, extra_hosts: Iterable[str] = ()) -> None:
    hosts = allowed_hosts(bind_address, extra_hosts)

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        host = host_from_header(request.headers.get("host", ""))
        if host not in hosts:
            logger.warning("Rejected request for foreign host %r on %s", host, request.url.path)
            return PlainTextResponse("Invalid host", status_code=400)

        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                origin_host = urlsplit(origin).hostname if origin != "null" else None
                if origin_host is None or origin_host.lower() not in hosts:
                    logger.warning("Rejected cross-origin %s from %r", request.method, origin)
                    return PlainTextResponse("Cross-origin request refused", status_code=403)

        return await call_next(request)
