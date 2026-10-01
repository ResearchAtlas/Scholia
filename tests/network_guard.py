"""Outbound network block for the test suite.

Installed for every test session by tests/conftest.py. Every outbound connection
is refused, including to other ports on this machine, because a local proxy could
forward a request to a paid provider. The only reachable endpoints are TCP mock
servers a test starts in this process and registers with `register_server`, for
as long as they stay open. Name lookups resolve loopback names only. Provider
keys and proxy settings are removed from the environment.

Limits: this guards the Python socket API in the test process. uvloop, whose
native event loop opens its own sockets, cannot be imported. Code that calls the
private C base class `_socket.socket` directly, other C extensions with their own
networking, and subprocesses a test starts are not covered; tests must not use them.
"""

import os
import socket
import sys
import threading
import weakref
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class NetworkBlocked(RuntimeError):
    """Raised for any connection or lookup a test has not registered.

    Not an OSError, so client libraries cannot treat it as a retryable network
    failure and quietly try another route.
    """


_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
_INET = (socket.AF_INET, socket.AF_INET6)
# endpoint -> the registered listening socket; allowed only while it stays open,
# and only for client sockets of the same family and type
_allowed: dict[tuple[str, int], weakref.ref] = {}
_listening: weakref.WeakSet = weakref.WeakSet()  # sockets that listen() in this process
_lock = threading.Lock()

_real = {
    name: getattr(socket.socket, name)
    for name in ("listen", "connect", "connect_ex", "sendto", "sendmsg")
}
_real_lookups = {
    name: getattr(socket, name)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo")
}

_SECRET_ENV_SUFFIXES = ("_API_KEY", "_API_TOKEN", "_ACCESS_TOKEN", "_SECRET_KEY")
_PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def _is_allowed(sock: socket.socket, address) -> bool:
    if sock.family not in _INET or sock.type != socket.SOCK_STREAM:
        return False
    if not (isinstance(address, tuple) and len(address) >= 2):
        return False
    with _lock:
        ref = _allowed.get((str(address[0]), int(address[1])))
    server = ref() if ref else None
    return server is not None and server.fileno() != -1 and server.family == sock.family


def _check(sock: socket.socket, address) -> None:
    if not _is_allowed(sock, address):
        raise NetworkBlocked(f"test network block: connection to {address!r} refused")


def _recording_listen(self, *args):
    result = _real["listen"](self, *args)
    with _lock:
        _listening.add(self)
    return result


def _guarded_connect(self, address):
    _check(self, address)
    return _real["connect"](self, address)


def _guarded_connect_ex(self, address):
    _check(self, address)
    return _real["connect_ex"](self, address)


def _guarded_sendto(self, data, *args):
    # sendto(data, address) or sendto(data, flags, address)
    _check(self, args[-1])
    return _real["sendto"](self, data, *args)


def _guarded_sendmsg(self, buffers, ancdata=(), flags=0, address=None):
    if address is None:
        return _real["sendmsg"](self, buffers, ancdata, flags)
    _check(self, address)
    return _real["sendmsg"](self, buffers, ancdata, flags, address)


def _require_loopback(name) -> None:
    text = name.decode() if isinstance(name, bytes) else name
    if text is not None and text not in _LOOPBACK_NAMES:
        raise NetworkBlocked(f"test network block: lookup of {text!r} refused")


def _guarded_lookup(name):
    real = _real_lookups[name]
    if name == "getnameinfo":
        def lookup(sockaddr, flags):
            _require_loopback(sockaddr[0])
            return real(sockaddr, flags)
    else:
        def lookup(host, *args, **kwargs):
            _require_loopback(host)
            return real(host, *args, **kwargs)
    return lookup


def install() -> None:
    socket.socket.listen = _recording_listen
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.sendto = _guarded_sendto
    socket.socket.sendmsg = _guarded_sendmsg
    for name in _real_lookups:
        setattr(socket, name, _guarded_lookup(name))
    sys.modules["uvloop"] = None  # makes `import uvloop` raise ImportError


def register_server(sock: socket.socket) -> tuple[str, int]:
    """Allow TCP connections to a listening socket this test process owns.

    Taking the socket object, not a port number, means only a server started in
    this process can be registered, never a proxy that happens to run locally.
    The registration lapses when the socket closes.
    """
    if sock.family not in _INET or sock.type != socket.SOCK_STREAM:
        raise ValueError("only TCP servers on loopback can be registered")
    host, port = sock.getsockname()[:2]
    if host not in ("127.0.0.1", "::1"):
        raise ValueError(f"mock servers must bind to loopback, not {host!r}")
    endpoints = [(host, port)] + ([("localhost", port)] if host == "127.0.0.1" else [])
    with _lock:
        if sock not in _listening or sock.fileno() == -1:
            raise ValueError("socket is not a server listening in this process")
        for endpoint in endpoints:
            _allowed[endpoint] = weakref.ref(sock)
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
