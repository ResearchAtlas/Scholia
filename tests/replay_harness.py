"""Offline replay of recorded model-provider exchanges.

`replay(name)` serves the exchanges recorded in tests/fixtures/replay/<name>.json
from an in-process HTTP server registered with the test network block, and
yields its base URL. Point a provider client at that URL instead of the real
provider. Each recorded exchange is served once, in order. A request is matched
on method, path and JSON body (key order ignored); headers are not matched, so
no key is ever needed. A request with no recorded exchange left gets a 501
response, and fails the test when the block exits, even if the client swallowed
the error.

Fixture format: a JSON list of
    {"request": {"method", "path", "body"}, "response": {"status", "body"}}
where both bodies are JSON values.
"""

import json
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from network_guard import mock_http_server

FIXTURES = Path(__file__).parent / "fixtures" / "replay"


class UnrecordedRequest(AssertionError):
    """A request reached the replay server with no recorded exchange left."""


def _key(method: str, path: str, body) -> tuple:
    return method, path, json.dumps(body, sort_keys=True)


@contextmanager
def replay(name: str):
    exchanges = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    recorded = defaultdict(deque)
    for exchange in exchanges:
        request = exchange["request"]
        recorded[_key(request["method"], request["path"], request.get("body"))].append(
            exchange["response"]
        )
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
                queue = recorded.get(_key(self.command, self.path, body))
                response = queue.popleft() if queue else None
                if response is None:
                    unrecorded.append(f"{self.command} {self.path}")
            if response is None:
                response = {"status": 501, "body": {"error": "no recorded response for this request"}}
            data = json.dumps(response["body"]).encode()
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        def log_message(self, *args):
            pass

    with mock_http_server(Handler) as base_url:
        yield base_url
    if unrecorded:
        raise UnrecordedRequest(f"requests with no recorded response: {unrecorded}")
