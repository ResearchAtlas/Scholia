"""Outbound network block for the test suite.

Installed for every test session by tests/conftest.py. Every outbound
connection is refused, including to other ports on this machine, because a local
proxy could forward a request to a paid provider. The only reachable endpoints are
mock servers a test starts in this process and registers with `register_server`.
Provider keys and proxy settings are removed from the environment.

Limit: this guards Python sockets in the test process. A subprocess a test starts
is not covered and must not be given network access.
"""

import os
import socket
import threading
import weakref
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class NetworkBlocked(RuntimeError):
    """Raised for any connection a test has not registered.

    Not an OSError, so client libraries cannot treat it as a retryable network
    failure and quietly try another route.
    """


_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
_allowed: set[tuple[str, int]] = set()
_listening: weakref.WeakSet = weakref.WeakSet()  # sockets that listen() in this process
_lock = threading.Lock()

_real_listen = socket.socket.listen
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_sendmsg = socket.socket.sendmsg
_real_getaddrinfo = socket.getaddrinfo

_SECRET_ENV_SUFFIXES = ("_API_KEY", "_API_TOKEN", "_ACCESS_TOKEN", "_SECRET_KEY")
_PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def _endpoint(address) -> tuple[str, int] | None:
    if isinstance(address, tuple) and len(address) >= 2:
        return str(address[0]), int(address[1])
    return None


def _check(sock: socket.socket, address) -> None:
    endpoint = _endpoint(address)
    if sock.family in (socket.AF_INET, socket.AF_INET6) and endpoint is not None:
        with _lock:
            if endpoint in _allowed:
                return
    raise NetworkBlocked(f"test network block: connection to {address!r} refused")


def _recording_listen(self, *args):
    result = _real_listen(self, *args)
    with _lock:
        _listening.add(self)
    return result


def _guarded_connect(self, address):
    _check(self, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check(self, address)
    return _real_connect_ex(self, address)


def _guarded_sendto(self, data, *args):
    # sendto(data, address) or sendto(data, flags, address)
    _check(self, args[-1])
    return _real_sendto(self, data, *args)


def _guarded_sendmsg(self, buffers, ancdata=(), flags=0, address=None):
    if address is None:
        return _real_sendmsg(self, buffers, ancdata, flags)
    _check(self, address)
    return _real_sendmsg(self, buffers, ancdata, flags, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else host
    if name is not None and name not in _LOOPBACK_NAMES:
        raise NetworkBlocked(f"test network block: lookup of {name!r} refused")
    return _real_getaddrinfo(host, *args, **kwargs)


def install() -> None:
    socket.socket.listen = _recording_listen
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.sendto = _guarded_sendto
    socket.socket.sendmsg = _guarded_sendmsg
    socket.getaddrinfo = _guarded_getaddrinfo


def register_server(sock: socket.socket) -> tuple[str, int]:
    """Allow connections to a listening socket this test process owns.

    Taking the socket object, not a port number, means only a server started in
    this process can be registered, never a proxy that happens to run locally.
    """
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        raise ValueError("only TCP servers on loopback can be registered")
    host, port = sock.getsockname()[:2]
    if host not in ("127.0.0.1", "::1"):
        raise ValueError(f"mock servers must bind to loopback, not {host!r}")
    with _lock:
        if sock not in _listening or sock.fileno() == -1:
            raise ValueError("socket is not a server listening in this process")
    endpoints = {(host, port)}
    if host == "127.0.0.1":
        endpoints.add(("localhost", port))
    with _lock:
        _allowed.update(endpoints)
    return host, port


def clear_registrations() -> None:
    with _lock:
        _allowed.clear()


@contextmanager
def mock_http_server(handler: type[BaseHTTPRequestHandler]):
    """Start an HTTP server on 127.0.0.1, register it, and yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = register_server(server.socket)
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _scrub_environment() -> None:
    for name in list(os.environ):
        if name in _PROXY_ENV or name.upper().endswith(_SECRET_ENV_SUFFIXES):
            del os.environ[name]


def pytest_configure(config):
    _scrub_environment()
    install()


@pytest.fixture(autouse=True)
def _reset_network_registrations():
    yield
    clear_registrations()
