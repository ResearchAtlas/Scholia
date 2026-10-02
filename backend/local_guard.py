"""Accept requests only from this machine and from the app's own pages.

A pure ASGI middleware: it reads the connection scope and headers, decides, and
either passes the request on untouched or answers it. It never reads, wraps or
replaces `receive`, so it cannot get between a streaming response and the
client's disconnect, and it decides before any route parses a body.

- The peer must be a loopback address, and the Host header exactly one of the
  app's own host:port values (a page from another site that resolves its name to
  127.0.0.1, DNS rebinding, still sends its own Host).
- An API request that carries Origin must carry exactly the app's origin, or a
  configured development origin; `null` and anything else are refused.
- An API request that changes something and carries no Origin must carry the
  header `X-Scholia-Client: local`, which a page on another site cannot send
  without a preflight. A read with no Origin must be same-origin by Fetch
  Metadata or carry that header. Fetch Metadata saying cross-site is refused.
- A request body must be JSON. CORS preflights are refused and no CORS headers are
  sent: the app's pages are same-origin with the API, so no other origin is ever
  granted access. A development origin is one whose server forwards /api to this
  one (Vite's proxy), so the browser sees same-origin requests and the backend sees
  that origin in Origin; it is never a cross-origin caller either.
- Anything outside /api is static navigation: GET or HEAD only.

The client header is not authentication. This protects against web pages and
documents, not against other programs running as the same user.
"""

import json

CLIENT_HEADER = b"x-scholia-client"
CLIENT_VALUE = b"local"
LOOPBACK = {"127.0.0.1", "::1"}
_CHANGES = {"POST", "PUT", "PATCH", "DELETE"}
_JSON = {b"application/json", b"application/json; charset=utf-8"}


class LocalRequestGuard:
    """origins: the app's origin and any development origins, e.g. "http://127.0.0.1:53111"."""

    def __init__(self, app, *, origins):
        self.app = app
        self.origins = {origin.encode() for origin in origins}
        self.hosts = {origin.split("://", 1)[1].encode() for origin in origins}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await _refuse(send, scope, 403, "not_allowed", "Only HTTP requests are accepted")
        problem = self.problem(scope)
        if problem:
            status, code = problem
            return await _refuse(send, scope, status, code, "The request was refused")
        return await self.app(scope, receive, send)

    def problem(self, scope):
        """(status, code) for a refused request, or None."""
        client = scope.get("client")
        if not client or client[0] not in LOOPBACK:
            return 403, "not_local"
        headers = {}
        for name, value in scope["headers"]:
            headers.setdefault(name, []).append(value)
        hosts = headers.get(b"host", [])
        if len(hosts) != 1 or hosts[0] not in self.hosts:
            return 403, "host_refused"
        method, path = scope["method"], scope["path"]
        if not (path == "/api" or path.startswith("/api/")):
            return None if method in ("GET", "HEAD") else (405, "method_not_allowed")
        origins = headers.get(b"origin", [])
        fetch_site = headers.get(b"sec-fetch-site", [b""])[-1]
        marked = headers.get(CLIENT_HEADER, []) == [CLIENT_VALUE]
        if len(origins) > 1 or (origins and origins[0] not in self.origins):
            return 403, "origin_refused"
        if fetch_site == b"cross-site":
            return 403, "cross_site"
        if method == "OPTIONS":
            return 403, "preflight_refused"
        if not origins:
            if method in _CHANGES and not marked:
                return 403, "client_header_missing"
            if method not in _CHANGES and not (marked or fetch_site == b"same-origin"):
                return 403, "client_header_missing"
        has_body = headers.get(b"transfer-encoding") or headers.get(b"content-length", [b"0"])[-1] not in (b"0", b"")
        if has_body:
            content_types = headers.get(b"content-type", [])
            if len(content_types) != 1 or content_types[0].lower().replace(b" ", b"") not in {
                    t.replace(b" ", b"") for t in _JSON}:
                return 415, "json_required"
        return None


async def _refuse(send, scope, status, code, message):
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"code": code, "message": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
