import hashlib
from http.server import BaseHTTPRequestHandler

import pytest

from network_guard import mock_http_server
from tools import fetch

BODY = b"release archive"
PIN = {"file": "a.tar.gz", "size": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest()}


class Server(BaseHTTPRequestHandler):
    requests = 0
    body = BODY

    def do_GET(self):
        type(self).requests += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Server.requests, Server.body = 0, BODY
    with mock_http_server(Server) as url:
        yield f"{url}/a.tar.gz"


def test_download_is_verified_and_kept(tmp_path, server):
    path = fetch.fetch(PIN, tmp_path, server)
    assert path == tmp_path / "a.tar.gz" and path.read_bytes() == BODY
    assert fetch.fetch(PIN, tmp_path, server) == path  # a verified copy is reused
    assert Server.requests == 1


def test_a_download_that_differs_from_the_pin_is_refused_and_deleted(tmp_path, server):
    Server.body = b"tampered archive"
    with pytest.raises(RuntimeError, match="16 bytes, expected 15"):
        fetch.fetch(PIN, tmp_path, server)
    Server.body = b"Release archive"  # same size, other bytes
    with pytest.raises(RuntimeError, match="SHA-256"):
        fetch.fetch(PIN, tmp_path, server)
    assert list(tmp_path.iterdir()) == []


def test_a_cached_file_that_differs_is_downloaded_again(tmp_path, server):
    (tmp_path / "a.tar.gz").write_bytes(b"Release archive")
    assert fetch.fetch(PIN, tmp_path, server).read_bytes() == BODY
    assert Server.requests == 1


def test_pins_are_complete():
    for pin in fetch.PINS.values():
        assert {"file", "size", "sha256", "url"} <= set(pin)
        assert pin["url"].startswith("https://") and pin["url"].endswith("/" + pin["file"])
        assert len(pin["sha256"]) == 64
        for source in pin.get("sources", {}).values():  # a model's mirrors serve the same pinned file
            assert source["url"].startswith(source["repository"] + "/resolve/")
            assert source["url"].endswith("/" + pin["file"])
    assert fetch.PINS["embedding-model"]["url"] == fetch.PINS["embedding-model"]["sources"]["huggingface"]["url"]
    assert set(fetch.PINS["reranker-model"]["sources"]) == {"huggingface"}  # ggml-org's repository only
