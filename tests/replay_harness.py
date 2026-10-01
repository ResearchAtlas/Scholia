"""Offline replay of recorded model-provider exchanges.

`replay(name)` serves the exchanges recorded in tests/fixtures/replay/<name>.json
from an in-process HTTP server registered with the test network block, and
yields its base URL. Point a provider client at that URL instead of the real
provider. The exchanges are served once each, in the recorded order: a request
of any HTTP method must match the next recorded exchange on method, path and
JSON body (key order ignored). Headers are not matched, so no key is ever
needed. A request that does not match the next exchange, out of order or
unrecorded, gets a 501 response and fails the test when the block exits, even
if the client swallowed the error.

Fixture format: a JSON list of
    {"request": {"method", "path", "body"}, "response": {"status", "body"}}
where both bodies are JSON values.
"""

import json
import threading
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from network_guard import mock_http_server

FIXTURES = Path(__file__).parent / "fixtures" / "replay"


class UnrecordedRequest(AssertionError):
    """A request reached the replay server that was not the next recorded exchange."""


def _key(method: str, path: str, body) -> tuple:
    return method, path, json.dumps(body, sort_keys=True)


@contextmanager
def replay(name: str):
    exchanges = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    pending = deque(exchanges)  # one queue: the next request must match its head
    unrecorded = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw.decode("utf-8", "replace")
            with lock:
                response = None
                if pending:
                    request = pending[0]["request"]
                    expected = _key(request["method"], request["path"], request.get("body"))
                    if _key(self.command, self.path, body) == expected:
                        response = pending.popleft()["response"]
                if response is None:
                    unrecorded.append(f"{self.command} {self.path}")
            if response is None:
                response = {"status": 501, "body": {"error": "not the next recorded request"}}
            data = json.dumps(response["body"]).encode()
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def __getattr__(self, name):
            # Every method (GET, HEAD, OPTIONS or any other) goes through the same match, so
            # none gets the base class's own 501 without being recorded as a failure.
            if name.startswith("do_"):
                return self._serve
            raise AttributeError(name)

        def log_message(self, *args):
            pass

    with mock_http_server(Handler) as base_url:
        yield base_url
    if unrecorded:
        raise UnrecordedRequest(f"requests that were not the next recorded exchange: {unrecorded}")
