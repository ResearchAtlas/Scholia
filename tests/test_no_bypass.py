"""Nothing leaves the app except through the outbound gate, and nothing sends telemetry
(slice-1 spec section 10; ticket 18).

A static check over the backend's modules: only backend/outbound_gate.py makes an HTTP
client or transport; the provider modules use the gate's client they are given; no other
network library is imported. The exceptions are named, each with what it may do: the
frozen self-test drives the app in process and calls the helper it started on loopback,
the desktop entry listens on loopback, and the gate parses IPv4 forms. The interface asks
only its own origin, which its Content-Security-Policy enforces. No dependency is a
telemetry or analytics client.
"""

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

from backend.app import _CSP

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted((ROOT / "backend").rglob("*.py"))
NETWORK = {"httpx", "httpcore", "requests", "aiohttp", "urllib3", "http.client", "urllib.request", "socket", "ssl",
           "websockets", "websocket", "ftplib", "smtplib", "telnetlib", "xmlrpc.client", "grpc"}  # and see test_network_guard.py
# Module -> the network modules it may import, and why.
ALLOWED_IMPORTS = {
    "outbound_gate.py": {"httpx", "socket"},  # the gate itself; socket.inet_aton reads IPv4 forms
    "openrouter.py": {"httpx"},  # exception and timeout types; requests go through the gate's client
    "openrouter_client.py": {"httpx"},  # the same
    "self_test.py": {"httpx", "urllib.request"},  # in-process app, and the helper it starts on loopback
    "desktop.py": {"socket"},  # the loopback socket the app is served on
}
MAKES_REQUESTS = {"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport", "get", "post", "put", "patch",
                  "delete", "head", "options", "request", "stream"}
IN_PROCESS = {"ASGITransport", "MockTransport"}


def imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def network_imports(path):
    found = set()
    for name in imports(ast.parse(path.read_text())):
        found |= {module for module in NETWORK if name == module or name.startswith(module + ".")}
    return found


def attribute_calls(tree, module):
    """(attribute, call) for every module.attribute(...) call."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == module:
            yield node.func.attr, node


@pytest.mark.parametrize("path", MODULES, ids=lambda path: str(path.relative_to(ROOT)))
def test_only_named_modules_import_a_network_library(path):
    assert network_imports(path) <= ALLOWED_IMPORTS.get(path.name, set())


def test_only_the_gate_makes_an_http_client():
    for path in MODULES:
        tree = ast.parse(path.read_text())
        for attribute, call in attribute_calls(tree, "httpx"):
            if attribute not in MAKES_REQUESTS or path.name == "outbound_gate.py":
                continue
            # The frozen self-test's clients reach the app in process, never the network.
            transport = next((k.value for k in call.keywords if k.arg == "transport"), None)
            assert path.name == "self_test.py" and isinstance(transport, ast.Call) \
                and isinstance(transport.func, ast.Attribute) and transport.func.attr in IN_PROCESS, \
                f"{path.relative_to(ROOT)}:{call.lineno} makes an HTTP client outside the gate"


def test_the_exceptions_do_only_what_they_are_named_for():
    # The self-test's one urllib request goes to the helper it started, on loopback.
    self_test = ast.parse((ROOT / "backend/self_test.py").read_text())
    requests = [node for node in ast.walk(self_test) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "Request"]
    assert len(requests) == 1
    url = requests[0].args[0]
    assert isinstance(url, ast.JoinedStr) and url.values[0].value == "http://127.0.0.1:"
    desktop = ast.parse((ROOT / "backend/desktop.py").read_text())
    calls = {node.func.attr for node in ast.walk(desktop) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)}
    assert not calls & {"connect", "connect_ex", "create_connection", "sendto", "open_connection"}
    gate = ast.parse((ROOT / "backend/outbound_gate.py").read_text())
    assert {attribute for attribute, _ in attribute_calls(gate, "socket")} == {"inet_aton"}


def test_the_interface_asks_only_its_own_origin():
    assert re.search(r"default-src 'self'", _CSP) and "connect-src" not in _CSP
    for path in (ROOT / "frontend/src").rglob("*.js*"):
        text = path.read_text()
        assert not re.search(r"XMLHttpRequest|WebSocket|sendBeacon|EventSource", text), path
        for call in re.findall(r"fetch\(([^,)]*)", text):
            assert path.name == "api.js" and call.strip() == "path", (path, call)


TELEMETRY = re.compile(r"sentry|posthog|segment|mixpanel|amplitude|datadog|newrelic|bugsnag|rollbar|analytics|"
                       r"telemetry|opentelemetry|honeycomb|logrocket|hotjar|plausible|statsig", re.IGNORECASE)


def test_no_dependency_is_a_telemetry_client():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    python = project["dependencies"] + [d for extra in project.get("optional-dependencies", {}).values() for d in extra]
    package = json.loads((ROOT / "frontend/package.json").read_text())
    npm = list(package.get("dependencies", {})) + list(package.get("devDependencies", {}))
    assert [name for name in python + npm if TELEMETRY.search(name)] == []
