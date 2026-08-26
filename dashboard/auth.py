"""HTTP Basic Auth guarding every dashboard route.

The dashboard can switch a running engine to `live` and arm real market
orders (see `dashboard/server.py`'s module docstring) -- if
`DASHBOARD_HOST` is set to anything other than `127.0.0.1`, that control
surface is reachable by anyone on the internet unless something gates it.
This is plain ASGI middleware (not `fastapi.security.HTTPBasic`, which only
wires into HTTP dependency injection) so it also covers the `/ws`
websocket handshake and the static frontend files, not just the JSON API.

The websocket handshake needs a second path in (`WsTicketStore`, below) --
see that class's docstring for why the Authorization header alone isn't
reliable there.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

_TICKET_TTL_SEC = 30.0


class WsTicketStore:
    """Short-lived, single-use tickets that let `/ws` connect without
    depending on the browser resending cached HTTP Basic Auth credentials
    on a raw WebSocket handshake.

    `fetch()` reliably reattaches cached Basic Auth credentials to
    same-origin requests, which is why every `/api/*` call works once
    logged in -- but the native `WebSocket` constructor doing the same is
    inconsistent across browsers (observed failing on iPad Safari: the
    page loads and every REST call succeeds, but `/ws` gets closed by
    this middleware on every attempt because no Authorization header ever
    arrives with the handshake). The fix used here is a standard one:
    the page fetches a ticket over a normal, reliably-authenticated HTTP
    request first (`GET /api/ws_ticket`, itself behind Basic Auth), then
    passes that ticket as a `/ws?ticket=...` query param instead of
    relying on the browser to carry the Authorization header over.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, float] = {}

    def issue(self) -> str:
        self._expire()
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = time.time() + _TICKET_TTL_SEC
        return ticket

    def consume(self, ticket: str | None) -> bool:
        """Check and invalidate `ticket` in one step (single-use)."""
        self._expire()
        if not ticket:
            return False
        return self._tickets.pop(ticket, None) is not None

    def _expire(self) -> None:
        now = time.time()
        for expired in [t for t, exp in self._tickets.items() if exp < now]:
            del self._tickets[expired]


class BasicAuthMiddleware:
    """Rejects any HTTP or websocket request without a matching Basic Auth
    header -- except a websocket handshake carrying a valid ticket from
    `ticket_store` (see `WsTicketStore` above), which is accepted instead."""

    def __init__(
        self,
        app: ASGIApp,
        username: str,
        password: str,
        ticket_store: WsTicketStore | None = None,
    ) -> None:
        self.app = app
        self.username = username
        self.password = password
        self.ticket_store = ticket_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket" and self.ticket_store is not None:
            query = parse_qs(scope.get("query_string", b"").decode())
            ticket = (query.get("ticket") or [None])[0]
            if self.ticket_store.consume(ticket):
                await self.app(scope, receive, send)
                return

        headers = dict(scope["headers"])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        if self._is_authorized(auth_header):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="crypto-arb-engine dashboard"'),
                    (b"content-type", b"text/plain"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"Authentication required."})

    def _is_authorized(self, auth_header: str) -> bool:
        if not auth_header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, _, password = decoded.partition(":")
        return hmac.compare_digest(username, self.username) and hmac.compare_digest(password, self.password)
