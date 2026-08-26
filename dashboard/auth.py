"""HTTP Basic Auth guarding every dashboard route.

The dashboard can switch a running engine to `live` and arm real market
orders (see `dashboard/server.py`'s module docstring) -- if
`DASHBOARD_HOST` is set to anything other than `127.0.0.1`, that control
surface is reachable by anyone on the internet unless something gates it.
This is plain ASGI middleware (not `fastapi.security.HTTPBasic`, which only
wires into HTTP dependency injection) so it also covers the `/ws`
websocket handshake and the static frontend files, not just the JSON API.
"""

from __future__ import annotations

import base64
import hmac

from starlette.types import ASGIApp, Receive, Scope, Send


class BasicAuthMiddleware:
    """Rejects any HTTP or websocket request without a matching Basic Auth header."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
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
