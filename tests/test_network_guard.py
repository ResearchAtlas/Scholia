import os
import socket
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

from network_guard import NetworkBlocked, mock_http_server, register_server

PROVIDER_URL = "https://openrouter.ai/api/v1/chat/completions"


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_listener():
    """A real listener on a local port, standing in for a local proxy. Never registered."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield server.getsockname()[1]


def test_unregistered_local_port_is_refused(local_listener):
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("127.0.0.1", local_listener), timeout=1)
    with socket.socket() as sock, pytest.raises(NetworkBlocked):
        sock.connect_ex(("127.0.0.1", local_listener))
    with pytest.raises(NetworkBlocked):
        httpx.get(f"http://127.0.0.1:{local_listener}/")


def test_provider_call_fails():
    with pytest.raises(NetworkBlocked):
        httpx.post(PROVIDER_URL, json={"model": "any", "messages": []})


async def _async_provider_call():
    async with httpx.AsyncClient() as client:
        await client.post(PROVIDER_URL, json={})


@pytest.mark.asyncio
async def test_async_provider_call_fails():
    with pytest.raises(NetworkBlocked):
        await _async_provider_call()


def test_provider_call_through_local_proxy_fails(local_listener):
    with httpx.Client(proxy=f"http://127.0.0.1:{local_listener}") as client:
        with pytest.raises(NetworkBlocked):
            client.post(PROVIDER_URL, json={})


def test_direct_connect_by_name_is_refused():
    with socket.socket() as sock, pytest.raises(NetworkBlocked):
        sock.connect(("openrouter.ai", 443))


@pytest.mark.parametrize(
    "lookup",
    [
        lambda: socket.getaddrinfo("openrouter.ai", 443),
        lambda: socket.gethostbyname("openrouter.ai"),
        lambda: socket.gethostbyname_ex("openrouter.ai"),
        lambda: socket.gethostbyaddr("8.8.8.8"),
        lambda: socket.getnameinfo(("8.8.8.8", 53), 0),
    ],
)
def test_lookup_of_remote_name_is_refused(lookup):
    with pytest.raises(NetworkBlocked):
        lookup()


def test_uvloop_cannot_be_imported():
    # uvloop's native event loop opens sockets the guard cannot see.
    with pytest.raises(ImportError):
        import uvloop  # noqa: F401


def test_udp_send_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(NetworkBlocked):
            sock.sendto(b"x", ("127.0.0.1", 53))
        with pytest.raises(NetworkBlocked):
            sock.sendmsg([b"x"], [], 0, ("127.0.0.1", 53))


def test_unix_socket_connect_is_refused(tmp_path):
    with socket.socket(socket.AF_UNIX) as sock, pytest.raises(NetworkBlocked):
        sock.connect(str(tmp_path / "proxy.sock"))


def test_registered_mock_server_is_reachable():
    with mock_http_server(_Ok) as base_url:
        assert httpx.get(base_url + "/").text == "ok"


@pytest.mark.asyncio
async def test_registered_mock_server_is_reachable_async():
    with mock_http_server(_Ok) as base_url:
        async with httpx.AsyncClient() as client:
            assert (await client.get(base_url + "/")).text == "ok"


def test_registration_covers_tcp_only():
    with mock_http_server(_Ok) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            with pytest.raises(NetworkBlocked):
                udp.sendto(b"x", ("127.0.0.1", port))
            with pytest.raises(NetworkBlocked):
                udp.connect(("127.0.0.1", port))


def test_ipv4_registration_does_not_allow_ipv6_localhost():
    with mock_http_server(_Ok) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        with socket.socket(socket.AF_INET6) as sock, pytest.raises(NetworkBlocked):
            sock.connect(("localhost", port))


def test_registration_lapses_when_server_closes():
    with mock_http_server(_Ok) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_register_refuses_non_listening_socket():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        with pytest.raises(ValueError):
            register_server(sock)


def test_register_refuses_non_loopback_bind():
    with socket.create_server(("0.0.0.0", 0)) as server:
        with pytest.raises(ValueError):
            register_server(server)


def test_provider_keys_and_proxies_absent_from_environment():
    leaked = [
        name for name in os.environ
        if name.upper().endswith(("_API_KEY", "_API_TOKEN", "_ACCESS_TOKEN", "_SECRET_KEY"))
        or name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    ]
    assert leaked == []
