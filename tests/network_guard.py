"""Outbound network block for the test suite.

Installed for every test session by tests/conftest.py. Every outbound connection
is refused, including to other ports on this machine, because a local proxy could
forward a request to a paid provider. The only reachable endpoints are TCP mock
servers a test starts in this process and registers with `register_server`, for
as long as they stay open. Name lookups resolve loopback names only. Provider
keys and proxy settings are removed from the environment.

The checks run in a CPython audit hook, which the interpreter calls from inside
its C socket code for every connect, connect_ex, sendto, sendmsg and name lookup,
whichever Python class made the socket.

Limits: uvloop, whose native event loop opens sockets without these audit events,
is refused (the session fails if it was loaded first). Other C extensions with
their own networking, and subprocesses a test starts, are not covered; tests must
not use them.
"""

import asyncio
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
_SEND_EVENTS = {"socket.connect", "socket.sendto", "socket.sendmsg"}
_LOOKUP_EVENTS = {
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
}
# endpoint -> the registered listening socket; allowed only while it stays open,
# and only for client sockets of the same family and type
_allowed: dict[tuple[str, int], weakref.ref] = {}
_listening: weakref.WeakSet = weakref.WeakSet()  # sockets that listen() in this process
_lock = threading.Lock()
_installed = False
_real_listen = socket.socket.listen

_SECRET_ENV_SUFFIXES = ("_API_KEY", "_API_TOKEN", "_ACCESS_TOKEN", "_SECRET_KEY")
_PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def _is_allowed(sock, address) -> bool:
    if sock.family not in _INET or sock.type != socket.SOCK_STREAM:
        return False
    if not (isinstance(address, tuple) and len(address) >= 2):
        return False
    with _lock:
        ref = _allowed.get((str(address[0]), int(address[1])))
    server = ref() if ref else None
    return server is not None and server.fileno() != -1 and server.family == sock.family


def _require_loopback(name) -> None:
    text = name.decode() if isinstance(name, bytes) else name
    if text is not None and text not in _LOOPBACK_NAMES:
        raise NetworkBlocked(f"test network block: lookup of {text!r} refused")


def _audit(event: str, args: tuple) -> None:
    if event in _SEND_EVENTS:
        sock, address = args
        # sendmsg without an address sends on an already connected socket
        if address is not None and not _is_allowed(sock, address):
            raise NetworkBlocked(f"test network block: connection to {address!r} refused")
    elif event in _LOOKUP_EVENTS:
        _require_loopback(args[0][0] if event == "socket.getnameinfo" else args[0])


def _recording_listen(self, *args):
    result = _real_listen(self, *args)
    with _lock:
        _listening.add(self)
    return result


def _refuse_uvloop() -> None:
    policy = asyncio.get_event_loop_policy()
    if sys.modules.get("uvloop") is not None or type(policy).__module__.startswith("uvloop"):
        raise RuntimeError(
            "uvloop was loaded before the test network block; its native sockets bypass it"
        )
    sys.modules["uvloop"] = None  # makes `import uvloop` raise ImportError


def install() -> None:
    global _installed
    _refuse_uvloop()
    if not _installed:  # audit hooks cannot be removed, so add it once
        sys.addaudithook(_audit)
        socket.socket.listen = _recording_listen
        _installed = True


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
