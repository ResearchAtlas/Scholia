"""Loopback is only transport: a local provider counts for Private and Local only after the researcher's declaration.

These tests send real requests to mock servers this process starts and
registers with the test network block, and check what each server received.
"""

import asyncio
import json
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

from backend.db import Database, new_id
from backend.outbound_gate import GateInputs, OutboundDenied, OutboundGate, listener_is_ours
from network_guard import allow_subprocess, mock_http_server


def server(stack, redirect_to=None):
    """Start a registered mock server; returns (base URL, the list of paths it received)."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received.append((self.command, self.path))
            if redirect_to:
                self.send_response(307)
                self.send_header("Location", f"{redirect_to}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

        def log_message(self, *args):
            pass

    return stack.enter_context(mock_http_server(Handler)), received


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data")
    yield database
    database.close()


@pytest.fixture
def stack():
    with ExitStack() as stack:
        yield stack


def project(db, sensitivity):
    project_id = new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO projects (id, name, kind, sensitivity) VALUES (?, 'P', 'research', ?)",
        (project_id, sensitivity)))
    return project_id


def declare(db, base_url):
    db.write(lambda conn: conn.execute(
        "INSERT INTO local_declarations (id, provider, base_url, statement) VALUES (?, 'ollama', ?, 'statement')",
        (new_id(), base_url)))


def reasons(db):
    return db.read(lambda conn: [
        json.loads(data)["reason"] for (data,) in
        conn.execute("SELECT data FROM audit_log WHERE event = 'outbound' ORDER BY seq")])


def ours(host, port):
    """The gate's real check that this account listens there; these servers run in this process."""
    with allow_subprocess("/usr/sbin/lsof"):
        return listener_is_ours(host, port)


def gate_for(db, provider_urls=(), helper_url=None):
    return OutboundGate(db, lambda: GateInputs(provider_urls=provider_urls, helper_url=helper_url),
                        local_listener=ours)


def test_loopback_provider_is_refused_for_local_only_until_declared(db, stack):
    base, received = server(stack)
    gate = gate_for(db, provider_urls=[f"{base}/v1"])
    project_id = project(db, "local_only")
    with gate.client(project_id) as client:
        with pytest.raises(OutboundDenied, match="not_declared"):
            client.post(f"{base}/v1/chat/completions", json={"messages": []})
        assert received == []
        declare(db, f"{base}/v1")
        assert client.post(f"{base}/v1/chat/completions", json={"messages": []}).json() == {"ok": True}
    assert received == [("POST", "/v1/chat/completions")]
    assert reasons(db) == ["not_declared", None]


def test_a_declaration_covers_only_its_exact_origin(db, stack):
    base, received = server(stack)
    port = base.rsplit(":", 1)[1]
    gate = gate_for(db, provider_urls=[f"{base}/v1"])
    project_id = project(db, "local_only")
    for declared in (
        f"http://localhost:{port}/v1",      # the same server under another name
        f"http://127.0.0.1:{int(port) + 1}/v1",
        f"https://127.0.0.1:{port}/v1",
        "https://api.other-provider.example/v1",
        "not a url",
    ):
        declare(db, declared)
        with gate.client(project_id) as client, pytest.raises(OutboundDenied, match="not_declared"):
            client.get(f"{base}/v1/models")
    assert received == []


def test_a_declaration_does_not_make_an_unconfigured_port_a_provider(db, stack):
    base, received = server(stack)
    declare(db, f"{base}/v1")
    with gate_for(db).client(project(db, "local_only")) as client, \
            pytest.raises(OutboundDenied, match="unknown_destination"):
        client.get(f"{base}/v1/models")
    assert received == []


def test_a_declaration_does_not_open_remote_providers_for_local_only(db):
    declare(db, "https://api.other-provider.example/v1")
    gate = gate_for(db, provider_urls=["https://api.other-provider.example/v1"])
    with gate.client(project(db, "local_only")) as client, \
            pytest.raises(OutboundDenied, match="not_allowed_at_level"):
        client.get("https://api.other-provider.example/v1/models")


def test_normal_ignores_declarations(db, stack):
    base, received = server(stack)
    gate = gate_for(db, provider_urls=[f"{base}/v1"])
    with gate.client(project(db, "normal")) as client:  # any model route, declared or not
        client.get(f"{base}/v1/models")
        declare(db, f"{base}/v1")
        client.get(f"{base}/v1/models")
    assert len(received) == 2


def test_private_uses_a_declared_server_without_openrouters_flags_or_a_confirmed_key(db, stack):
    # Ticket 64: the same declaration, audit record and dispatch checks as Local only; the
    # Private allowlist and the key confirmation are OpenRouter's alone.
    base, received = server(stack)
    gate = OutboundGate(db, lambda: GateInputs(provider_urls=[f"{base}/v1"], private_route=lambda *args: None,
                                               key_attested=lambda *args: False), local_listener=ours)
    with gate.client(project(db, "private")) as client:
        with pytest.raises(OutboundDenied, match="not_declared"):  # loopback alone is only transport
            client.post(f"{base}/v1/chat/completions", json={"model": "local", "messages": []})
        assert received == []
        declare(db, f"{base}/v1")
        client.post(f"{base}/v1/chat/completions", json={"model": "local", "messages": []},
                    headers={"Authorization": "Bearer local"})
    assert received == [("POST", "/v1/chat/completions")]
    assert reasons(db) == ["not_declared", None]


@pytest.mark.parametrize("follow", [True, False])
def test_a_declared_server_cannot_redirect_elsewhere_from_a_private_project(db, stack, follow):
    target, target_received = server(stack)
    base, received = server(stack, redirect_to=target)
    declare(db, base)
    declare(db, target)
    gate = gate_for(db, provider_urls=[f"{base}/v1", f"{target}/v1"])
    with gate.client(project(db, "private"), follow_redirects=follow) as client, \
            pytest.raises(OutboundDenied, match="cross_origin_redirect"):
        client.post(f"{base}/v1/chat/completions", json={"messages": []})
    assert target_received == []


@pytest.mark.parametrize("level", ["normal", "private", "local_only"])
def test_the_local_helper_is_allowed_at_every_level(db, stack, level):
    helper, received = server(stack)
    with gate_for(db, helper_url=helper).client(project(db, level)) as client:
        client.get(f"{helper}/health")
    assert received == [("GET", "/health")]


@pytest.mark.parametrize("level", ["normal", "private", "local_only"])
def test_the_gate_refuses_another_local_server_before_it_is_reached(db, stack, level):
    # The other server is registered, so only the gate stands between them.
    helper, _ = server(stack)
    other, received = server(stack)
    declare(db, other)
    with gate_for(db, helper_url=helper).client(project(db, level)) as client, \
            pytest.raises(OutboundDenied, match="unknown_destination"):
        client.get(f"{other}/anything")
    assert received == []


@pytest.mark.parametrize("follow", [True, False])
def test_a_declared_server_cannot_redirect_elsewhere(db, stack, follow):
    target, target_received = server(stack)
    base, received = server(stack, redirect_to=target)
    declare(db, base)
    declare(db, target)
    gate = gate_for(db, provider_urls=[f"{base}/v1", f"{target}/v1"])
    with gate.client(project(db, "local_only"), follow_redirects=follow) as client, \
            pytest.raises(OutboundDenied, match="cross_origin_redirect"):
        client.post(f"{base}/v1/chat/completions", json={"messages": []})
    assert received == [("POST", "/v1/chat/completions")]
    assert target_received == []
    assert reasons(db) == [None, "cross_origin_redirect"]


def test_proxy_settings_in_the_environment_are_ignored(db, stack, monkeypatch):
    helper, received = server(stack)
    proxy, proxy_received = server(stack)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    with httpx.Client() as plain:  # the premise: a default client would go through the proxy
        plain.get(f"{helper}/health")
    assert proxy_received == [("GET", f"{helper}/health")] and received == []
    proxy_received.clear()
    with gate_for(db, helper_url=helper).client(project(db, "normal")) as client:
        client.get(f"{helper}/health")
    assert received == [("GET", "/health")]
    assert proxy_received == []


def test_certificate_settings_in_the_environment_are_ignored(db, stack, monkeypatch):
    helper, received = server(stack)
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/scholia-test-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent/scholia-test-certs")
    with pytest.raises(OSError):  # the premise: httpx's default transport reads them
        httpx.HTTPTransport()
    gate = gate_for(db, helper_url=helper)
    project_id = project(db, "normal")
    with gate.client(project_id) as client:
        client.get(f"{helper}/health")

    async def fetch():
        async with gate.async_client(project_id) as client:
            await client.get(f"{helper}/health")

    asyncio.run(fetch())
    assert received == [("GET", "/health"), ("GET", "/health")]


@pytest.mark.asyncio
async def test_async_client_over_real_sockets(db, stack):
    base, received = server(stack)
    project_id = await asyncio.to_thread(project, db, "local_only")
    gate = gate_for(db, provider_urls=[f"{base}/v1"])
    async with gate.async_client(project_id) as client:
        with pytest.raises(OutboundDenied, match="not_declared"):
            await client.post(f"{base}/v1/chat/completions", json={"messages": []})
        await asyncio.to_thread(declare, db, f"{base}/v1")
        assert (await client.post(f"{base}/v1/chat/completions", json={"messages": []})).status_code == 200
    assert received == [("POST", "/v1/chat/completions")]


# Declaring through the API (slice-1 spec section 6.4; ticket 64)


@pytest.mark.asyncio
async def test_a_declaration_is_one_per_exact_origin_audited_and_can_be_withdrawn(tmp_path):
    from scholia_app import started

    async with started(tmp_path / "data") as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.ollama.kind": "openai-compatible", "providers.ollama.base_url": "http://127.0.0.1:11434/v1",
            "providers.again.kind": "openai-compatible", "providers.again.base_url": "http://127.0.0.1:11434/other",
            "providers.cloud.kind": "openai-compatible", "providers.cloud.base_url": "https://api.example.com/v1"}})
        for name in ("ollama", "again"):  # two names for one server: one declaration, the newer
            response = await client.post("/api/local-declarations",
                                         json={"provider": name, "origin": "http://127.0.0.1:11434"})
            assert response.json() == {"ok": True}
        db = client.state["db"]
        stored = await asyncio.to_thread(db.read, lambda conn: conn.execute(
            "SELECT provider, base_url, statement FROM local_declarations").fetchall())
        assert stored == [("again", "http://127.0.0.1:11434/other", "2026-10-03")]
        listed = {p["name"]: (p["local"], p["declared_at"] is not None)
                  for p in (await client.get("/api/providers")).json()["providers"]}
        assert listed == {"openrouter": (False, False), "ollama": (True, True), "again": (True, True),
                          "cloud": (False, False)}
        response = await client.post("/api/local-declarations",
                                     json={"provider": "cloud", "origin": "https://api.example.com:443"})
        assert (response.status_code, response.json()["code"]) == (400, "not_local")
        response = await client.post("/api/local-declarations",
                                     json={"provider": "nobody", "origin": "http://127.0.0.1:11434"})
        assert response.status_code == 404

        assert (await client.delete("/api/local-declarations/ollama")).json() == {"ok": True}  # its origin's
        assert (await client.delete("/api/local-declarations/again")).status_code == 404
        audit = await asyncio.to_thread(db.read, lambda conn: conn.execute(
            "SELECT event, data FROM audit_log WHERE event LIKE 'local_%' ORDER BY seq").fetchall())
        assert [(event, json.loads(data)) for event, data in audit] == [
            ("local_declared", {"provider": "ollama", "origin": "http://127.0.0.1:11434", "statement": "2026-10-03"}),
            ("local_declared", {"provider": "again", "origin": "http://127.0.0.1:11434", "statement": "2026-10-03"}),
            ("local_declaration_withdrawn", {"provider": "ollama", "origin": "http://127.0.0.1:11434"})]


@pytest.mark.asyncio
async def test_a_declaration_names_the_origin_the_researcher_saw(tmp_path):
    from scholia_app import started

    async with started(tmp_path / "data") as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.ollama.kind": "openai-compatible", "providers.ollama.base_url": "http://127.0.0.1:11434/v1"}})
        [shown] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == "ollama"]
        assert shown["origin"] == "http://127.0.0.1:11434"
        current = (await client.get("/api/settings")).json()  # its address changes after the card was shown
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.ollama.base_url": "http://127.0.0.1:1234/v1"}})
        stale = await client.post("/api/local-declarations", json={"provider": "ollama", "origin": shown["origin"]})
        assert (stale.status_code, stale.json()["code"]) == (409, "target_changed")
        assert (await client.post("/api/local-declarations", json={"provider": "ollama"})).status_code == 400
        db = client.state["db"]
        assert await asyncio.to_thread(db.read, lambda conn: conn.execute(
            "SELECT count(*) FROM local_declarations").fetchone()) == (0,)
        fresh = await client.post("/api/local-declarations",
                                  json={"provider": "ollama", "origin": "http://127.0.0.1:1234"})
        assert fresh.status_code == 200
