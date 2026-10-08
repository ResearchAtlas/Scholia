"""Requests are accepted only from this machine and the app's own pages, before any body is read.

Includes one real loopback TCP stream: closing the connection mid-turn cancels
the turn and its model call, with the guard installed.
"""

import asyncio
import json
import socket
import threading

import httpx
import pytest
import uvicorn

from network_guard import register_server
from scholia_app import KEY, ORIGIN, FakeKeyring, MockProvider, app_for, started

pytestmark = pytest.mark.asyncio
HOST = ORIGIN.split("://")[1]


async def raw(app, method, path, headers=(), body=b"", client=("127.0.0.1", 50000)):
    """Call the ASGI app with exactly these headers. Returns (status, json body or None)."""
    sent = []
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": [(k.lower(), v) for k, v in headers],
             "client": client, "server": ("127.0.0.1", 8765), "scheme": "http"}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return start["status"], json.loads(payload)
    except ValueError:
        return start["status"], None


def headers(*extra, host=HOST):
    return [(b"host", host.encode()), *[(k, v) for k, v in extra]]


JSON = (b"content-type", b"application/json")
MARK = (b"x-scholia-client", b"local")
SAME = (b"origin", ORIGIN.encode())


@pytest.mark.parametrize("method, path, extra, body, expected", [
    # Allowed
    ("GET", "/api/projects", [(b"sec-fetch-site", b"same-origin")], b"", 200),
    ("GET", "/api/projects", [MARK], b"", 200),
    ("POST", "/api/projects", [SAME, JSON], b'{"name": "A"}', 201),
    ("POST", "/api/projects", [MARK, JSON], b'{"name": "A"}', 201),
    ("POST", "/api/projects", [SAME, (b"content-type", b"application/json; charset=utf-8")], b'{"name": "A"}', 201),
    ("GET", "/", [], b"", 404),  # static navigation needs no header (no build here, so 404)
    # Refused
    ("GET", "/api/projects", [], b"", 403),  # no Origin, no marker, no fetch metadata
    ("GET", "/api/projects", [(b"sec-fetch-site", b"same-site")], b"", 403),
    ("GET", "/api/projects", [MARK, (b"sec-fetch-site", b"cross-site")], b"", 403),
    ("POST", "/api/projects", [JSON], b'{"name": "A"}', 403),  # a typeless or form POST from another page
    ("POST", "/api/projects", [(b"origin", b"null"), JSON], b'{"name": "A"}', 403),
    ("POST", "/api/projects", [(b"origin", b"http://127.0.0.1:9999"), JSON], b'{"name": "A"}', 403),
    ("POST", "/api/projects", [(b"origin", b"http://localhost.attacker.example:8765"), JSON], b'{"name": "A"}', 403),
    ("POST", "/api/projects", [SAME, (b"origin", ORIGIN.encode()), JSON], b'{"name": "A"}', 403),  # two Origins
    ("POST", "/api/projects", [SAME, (b"content-type", b"text/plain")], b'{"name": "A"}', 415),
    ("POST", "/api/projects", [SAME, (b"content-type", b"application/x-www-form-urlencoded")], b"name=A", 415),
    ("POST", "/api/projects", [SAME, (b"content-type", b"multipart/form-data; boundary=x")], b"--x--", 415),
    ("POST", "/api/projects", [SAME], b'{"name": "A"}', 415),  # a body with no content type
    ("OPTIONS", "/api/projects", [SAME, (b"access-control-request-method", b"POST")], b"", 403),
    ("POST", "/", [SAME, JSON], b"{}", 405),
    ("DELETE", "/index.html", [MARK], b"", 405),
])
async def test_the_request_matrix(tmp_path, method, path, extra, body, expected):
    async with started(tmp_path / "data") as client:
        hdrs = headers(*extra)
        if body:
            hdrs.append((b"content-length", str(len(body)).encode()))
        status, _ = await raw(client.app, method, path, hdrs, body)
        assert status == expected
        if expected >= 400 and path == "/api/projects" and method == "POST":
            assert len((await client.get("/api/projects")).json()["projects"]) == 1  # General only: nothing created


@pytest.mark.parametrize("host_headers", [
    [(b"host", b"attacker.example")],
    [(b"host", b"127.0.0.1:9999")],
    [(b"host", b"localhost.attacker.example:8765")],
    [(b"host", HOST.encode()), (b"host", b"attacker.example")],
    [],
])
async def test_a_host_other_than_the_apps_own_is_refused(tmp_path, host_headers):
    async with started(tmp_path / "data") as client:
        status, body = await raw(client.app, "GET", "/api/projects", [*host_headers, MARK])
        assert (status, body["code"]) == (403, "host_refused")


async def test_a_peer_off_this_machine_is_refused(tmp_path):
    async with started(tmp_path / "data") as client:
        status, body = await raw(client.app, "GET", "/api/projects", headers(MARK), client=("192.168.1.20", 50000))
        assert (status, body["code"]) == (403, "not_local")


async def test_a_refused_request_never_reaches_a_route(tmp_path):
    async with started(tmp_path / "data") as client:
        calls = []
        original = client.app.app

        async def recording(scope, receive, send):
            calls.append(scope["path"])
            return await original(scope, receive, send)

        client.app.app = recording
        await raw(client.app, "POST", "/api/projects", headers(JSON), b'{"name": "A"}')
        assert calls == []
        await raw(client.app, "GET", "/api/projects", headers(MARK))
        assert calls == ["/api/projects"]
        client.app.app = original


async def test_a_body_over_the_limit_or_of_unknown_length_is_refused_before_anything_reads_it(tmp_path):
    from backend import local_guard
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        path, reached, original = f"/api/projects/{project}/materials", [], client.app.app

        async def recording(scope, receive, send):
            reached.append(scope["path"])
            return await original(scope, receive, send)

        client.app.app = recording
        over = str(local_guard.MAX_BODY + 1).encode()  # said by its Content-Length, before any of it is read
        status, body = await raw(client.app, "POST", path, headers(SAME, JSON, (b"content-length", over)), b"")
        assert (status, body["code"]) == (413, "request_too_large")
        status, body = await raw(client.app, "POST", path, headers(SAME, JSON, (b"transfer-encoding", b"chunked")), b"{}")
        assert (status, body["code"]) == (411, "length_required")  # a length not known before it is read
        assert reached == []  # no route parsed either
        client.app.app = original
        assert (await client.get(f"/api/projects/{project}/materials")).json()["materials"] == []


def test_the_body_limit_admits_one_file_of_the_largest_size_and_no_more():
    from backend import extraction, local_guard
    encoded = (extraction.MAX_FILE_BYTES + 2) // 3 * 4  # its bytes in base64
    around = json.dumps({"files": [{"name": "\u0001" * 255, "data": ""}], "conversation_id": "x" * 100,
                         "material_id": "x" * 100})  # its name escaped character by character, the longest ids
    assert encoded + len(around) <= local_guard.MAX_BODY < 2 * encoded


async def test_a_closed_connection_mid_turn_cancels_it_over_real_loopback_tcp(tmp_path):
    """A real server and a real socket: the client disconnects while the model call waits."""
    provider = MockProvider()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("never delivered")

    provider.replies.append(held)
    keyring = FakeKeyring()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    register_server(sock)
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    from backend.app import create_app
    app = create_app(tmp_path / "data", origin=origin, keyring_backend=keyring,
                     transport=httpx.MockTransport(provider))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", loop="asyncio", http="h11", ws="none",
                                           log_config=None, access_log=False))
    loop_box = {}

    def serve():
        async def main():
            loop_box["loop"] = asyncio.get_running_loop()
            await server.serve(sockets=[sock])
        asyncio.run(main())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        hdrs = {"X-Scholia-Client": "local", "Origin": origin}
        async with httpx.AsyncClient(base_url=origin, headers=hdrs, timeout=10) as http:
            assert (await http.post("/api/setup", json={"openrouter_key": KEY})).status_code == 200
            conversation = (await http.post("/api/conversations", json={})).json()["id"]
            async with http.stream("POST", f"/api/conversations/{conversation}/message/stream",
                                   json={"content": "hi"}) as stream:
                lines = stream.aiter_lines()
                first = await anext(lines)
                run_id = json.loads(first[len("data: "):])["run_id"]
                for _ in range(200):
                    if provider.answers:
                        break
                    await asyncio.sleep(0.02)
            # The stream is closed here, mid-call.
            for _ in range(200):
                turn = (await http.get(f"/api/conversations/{conversation}")).json()["turns"][0]
                if turn["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert (turn["status"], turn["cancel_reason"]) == ("cancelled", "researcher")
            assert run_id == turn["run_id"]
            harness = app.app.state.scholia["harness"]
            assert not harness.registry.is_active(run_id)
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()


DEV = "http://127.0.0.1:5173"


async def test_a_configured_development_origin_gets_its_preflight_and_cors_headers(tmp_path):
    async with started(tmp_path / "data", dev_origins=[DEV]) as client:
        preflight = await client.options("/api/projects", headers={
            "Origin": DEV, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-scholia-client"})
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == DEV
        assert "allow-credentials" not in " ".join(preflight.headers.keys())
        response = await client.post("/api/projects", json={"name": "From dev"}, headers={"Origin": DEV})
        assert response.status_code == 201 and response.headers["access-control-allow-origin"] == DEV
        own = await client.post("/api/projects", json={"name": "Same origin"}, headers={"Origin": ORIGIN})
        assert own.status_code == 201 and "access-control-allow-origin" not in own.headers


@pytest.mark.parametrize("origin, method, asked", [
    ("http://127.0.0.1:5174", "POST", "content-type"),  # not configured
    (ORIGIN, "POST", "content-type"),  # the app's own pages never need one
    (DEV, "TRACE", "content-type"),
    (DEV, "POST", "content-type, authorization"),
    ("null", "POST", "content-type"),
])
async def test_other_preflights_are_refused(tmp_path, origin, method, asked):
    async with started(tmp_path / "data", dev_origins=[DEV]) as client:
        response = await client.options("/api/projects", headers={
            "Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": asked})
        assert response.status_code == 403 and "access-control-allow-origin" not in response.headers


SESSION = "s" * 43
OWN = (b"x-scholia-session", SESSION.encode())


async def test_with_a_session_every_api_request_needs_this_launchs_secret(tmp_path):
    async with started(tmp_path / "data", setup=False, session=SESSION) as client:
        app = client.app
        assert (await raw(app, "GET", "/api/projects", headers(MARK, OWN)))[0] == 200
        for extra in ([MARK],  # what another account on this machine can send
                      [MARK, (b"x-scholia-session", b"wrong")],
                      [MARK, OWN, OWN]):  # exactly one
            status, body = await raw(app, "GET", "/api/projects", headers(*extra))
            assert (status, body["code"]) == (401, "session_required")
        status, _ = await raw(app, "POST", "/api/projects", headers(MARK, JSON), body=b'{"name": "A"}')
        assert status == 401
        assert (await raw(app, "GET", "/", headers()))[0] != 401  # the static pages hold no data
        assert (await raw(app, "GET", "/api/projects", headers(MARK, OWN), client=("10.0.0.2", 1)))[1]["code"] == \
            "not_local"  # the earlier checks still come first
        assert (await raw(app, "GET", "/api/projects", headers(MARK, OWN, host="evil.test:80")))[1]["code"] == \
            "host_refused"


async def test_a_session_needs_a_long_secret_and_no_development_origins():
    from backend.local_guard import LocalRequestGuard
    with pytest.raises(ValueError):
        LocalRequestGuard(None, origin=ORIGIN, session="short")
    with pytest.raises(ValueError):
        LocalRequestGuard(None, origin=ORIGIN, dev_origins=("http://localhost:5173",), session=SESSION)
