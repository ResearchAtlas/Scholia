import _socket
import asyncio
import os
import socket
import sys
import types
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

import network_guard
from network_guard import NetworkBlocked, allow_subprocess, mock_http_server, register_server

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


def test_raw_c_socket_class_is_guarded(local_listener):
    # The C base class, below any Python-level patch, is checked by the audit hook.
    sock = _socket.socket()
    try:
        with pytest.raises(NetworkBlocked):
            sock.connect(("127.0.0.1", local_listener))
    finally:
        sock.close()


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


def test_connect_by_name_is_refused_before_any_lookup():
    # The C connect resolves names before its audit event. A name that cannot
    # resolve shows the order: NetworkBlocked, not a resolver error.
    with socket.socket() as sock, pytest.raises(NetworkBlocked):
        sock.connect(("scholia-test.invalid", 443))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock, pytest.raises(NetworkBlocked):
        sock.sendto(b"x", ("scholia-test.invalid", 53))
    with socket.SocketType() as sock, pytest.raises(NetworkBlocked):
        sock.connect(("scholia-test.invalid", 443))


def test_address_subclass_cannot_disguise_destination():
    class Disguised(str):
        def __str__(self):
            return "127.0.0.1"

    with mock_http_server(_Ok) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        with socket.socket() as sock, pytest.raises(NetworkBlocked):
            sock.connect((Disguised("10.255.255.1"), port))
        sock = socket.SocketType()
        try:
            with pytest.raises(NetworkBlocked):
                sock.connect((Disguised("10.255.255.1"), port))
        finally:
            sock.close()


def test_connection_opened_before_the_block_fails_the_session():
    with mock_http_server(_Ok) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            network_guard.clear_registrations()  # as if it predated the block
            with pytest.raises(RuntimeError, match="open before"):
                network_guard._refuse_open_connections()


def test_refused_audit_hook_fails_the_session(monkeypatch):
    monkeypatch.setattr(network_guard, "_installed", False)
    monkeypatch.setattr(sys, "addaudithook", lambda hook: None)  # refused silently
    monkeypatch.setattr(sys, "audit", lambda *args: None)
    with pytest.raises(RuntimeError, match="audit hook"):
        network_guard.install()


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


def test_lookup_name_subclass_cannot_pass_as_loopback():
    class Disguised(str):
        def __eq__(self, other):
            return True

        def __hash__(self):
            return hash("localhost")

    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo(Disguised("scholia-test.invalid"), 443)


def test_plugins_load_only_by_name(pytestconfig):
    # Auto-loaded plugins would import before the block installs.
    assert pytestconfig.getoption("disable_plugin_autoload") is True
    assert not pytestconfig.pluginmanager.has_plugin("anyio")


def test_uvloop_cannot_be_imported():
    # uvloop's native event loop opens sockets the guard cannot see.
    with pytest.raises(ImportError):
        import uvloop  # noqa: F401


def test_uvloop_loaded_before_the_guard_fails_the_session(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvloop", types.ModuleType("uvloop"))
    with pytest.raises(RuntimeError, match="uvloop"):
        network_guard.install()


def test_uvloop_policy_set_before_the_guard_fails_the_session(monkeypatch):
    policy = type("EventLoopPolicy", (asyncio.DefaultEventLoopPolicy,), {"__module__": "uvloop"})()
    monkeypatch.setattr(asyncio, "get_event_loop_policy", lambda: policy)
    with pytest.raises(RuntimeError, match="uvloop"):
        network_guard.install()


def test_udp_send_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(NetworkBlocked):
            sock.sendto(b"x", ("127.0.0.1", 53))
        with pytest.raises(NetworkBlocked):
            sock.sendmsg([b"x"], [], 0, ("127.0.0.1", 53))


def test_unix_socket_connect_is_refused():
    # A short relative path: macOS limits AF_UNIX paths to 104 bytes.
    with socket.socket(socket.AF_UNIX) as sock, pytest.raises(NetworkBlocked):
        sock.connect("proxy.sock")


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


def test_project_code_avoids_networking_outside_the_block():
    # Native and private networking APIs bypass Python sockets, so the block's
    # stated limits are enforced on the repository's own code.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    exempt = {"tests/network_guard.py", "tests/test_network_guard.py"}
    pattern = re.compile(r"\b(uvloop|_socket|NSURL\w*|\w*ContentsOfURL\w*|CFNetwork|CFStream|pycurl)\b")
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for folder in ("backend", "tests")
        for path in (root / folder).rglob("*.py")
        if str(path.relative_to(root)) not in exempt
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_child_process_needs_an_explicit_allowance():
    import subprocess

    with pytest.raises(NetworkBlocked):
        subprocess.run(["/usr/bin/true"], check=True)
    with allow_subprocess("/usr/bin/true"):
        subprocess.run(["/usr/bin/true"], check=True)
        with pytest.raises(NetworkBlocked):  # only the named program
            subprocess.run(["/bin/echo"], check=True)
        with pytest.raises(NetworkBlocked):  # a shell could run anything
            os.system("/usr/bin/true")
    with pytest.raises(NetworkBlocked):
        subprocess.run(["/usr/bin/true"], check=True)
    with pytest.raises(ValueError):
        with allow_subprocess("true"):
            pass
