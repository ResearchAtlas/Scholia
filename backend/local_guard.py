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
- A request body must be JSON.
- CORS: the app's own pages are same-origin with the API and never need it. An
  explicitly configured development origin (a dev server on this machine) may call
  the API across origins: its preflight is checked (origin, method, and only the
  Content-Type and client headers) and answered, and its responses carry
  Access-Control-Allow-Origin for that origin alone, without credentials. Any
  other preflight is refused.
- Anything outside /api is static navigation: GET or HEAD only.

The client header is not authentication. This protects against web pages and
documents, not against other programs running as the same user.
"""

import json

CLIENT_HEADER = b"x-scholia-client"
CLIENT_VALUE = b"local"
LOOPBACK = {"127.0.0.1", "::1"}
_CHANGES = {"POST", "PUT", "PATCH", "DELETE"}
_CORS_METHODS = b"GET, POST, PUT, PATCH, DELETE"
_CORS_HEADERS = {b"content-type", CLIENT_HEADER}
_JSON = {b"application/json", b"application/json; charset=utf-8"}


class LocalRequestGuard:
    """origin: the app's own origin, e.g. "http://127.0.0.1:53111"; dev_origins: development
    servers allowed to call the API across origins."""

    def __init__(self, app, *, origin, dev_origins=()):
        self.app = app
        self.dev_origins = {o.encode() for o in dev_origins}
        self.origins = {origin.encode(), *self.dev_origins}
        self.hosts = {o.split(b"://", 1)[1] for o in self.origins}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await _refuse(send, scope, 403, "not_allowed", "Only HTTP requests are accepted")
        problem = self.problem(scope)
        if problem:
            status, code = problem
            return await _refuse(send, scope, status, code, "The request was refused")
        origin = dict(scope["headers"]).get(b"origin")
        if scope["method"] == "OPTIONS":  # a development origin's preflight, checked above
            return await _preflight(send, origin)
        if origin in self.dev_origins:  # the response is readable by that origin alone
            return await self.app(scope, receive, _with_cors(send, origin))
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
            requested = headers.get(b"access-control-request-method", [b""])[-1]
            asked = {h.strip().lower() for v in headers.get(b"access-control-request-headers", []) for h in v.split(b",")}
            asked.discard(b"")
            if (not origins or origins[0] not in self.dev_origins or requested not in _CORS_METHODS.split(b", ")
                    or not asked <= _CORS_HEADERS):
                return 403, "preflight_refused"
            return None
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


async def _preflight(send, origin):
    await send({"type": "http.response.start", "status": 204, "headers": [
        (b"access-control-allow-origin", origin), (b"access-control-allow-methods", _CORS_METHODS),
        (b"access-control-allow-headers", b"content-type, x-scholia-client"),
        (b"access-control-max-age", b"600"), (b"vary", b"origin"), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


def _with_cors(send, origin):
    async def send_with_cors(message):
        if message["type"] == "http.response.start":
            message = {**message, "headers": [*message.get("headers", []),
                                              (b"access-control-allow-origin", origin), (b"vary", b"origin")]}
        await send(message)
    return send_with_cors


async def _refuse(send, scope, status, code, message):
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"code": code, "message": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
