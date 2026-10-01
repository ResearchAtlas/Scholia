"""The outbound gate: classification, policy by sensitivity level, audit records and bypass attempts.

Remote hosts are an in-process httpx.MockTransport that records what reaches it;
loopback cases with real sockets are in test_local_declaration.py.
"""

import asyncio
import dataclasses
import json
import sqlite3
import threading

import httpx
import pytest

from backend.db import Database, new_id
from backend.outbound_gate import GateInputs, OutboundDenied, OutboundGate

OPENROUTER_API = "https://openrouter.ai/api/v1"
CHAT = f"{OPENROUTER_API}/chat/completions"
OTHER_PROVIDER = "https://api.other-provider.example/v1"
HELPER = "http://127.0.0.1:8765"
LOCAL_SERVER = "http://127.0.0.1:11434/v1"
OA_LINK = "https://repository.example.org/files/paper.pdf"
ROUTES = {"example/model-a", "example/model-b"}
KEY = "sk-or-v1-confirmed-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
ZDR_ONLY = {"provider": {"zdr": True}}


def chat(**fields):
    return {"model": "example/model-a", "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
            "provider": {"zdr": True}, **fields}


class Remote:
    """Stands in for every remote host and records the requests that reach it."""

    def __init__(self):
        self.received = []
        self.redirects = {}  # url -> (status, Location)

    def __call__(self, request):
        self.received.append(request)
        if str(request.url) in self.redirects:
            status, location = self.redirects[str(request.url)]
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data")
    yield database
    database.close()


@pytest.fixture
def remote():
    return Remote()


@pytest.fixture
def setup(db, remote):
    """A gate over db and remote, with full inputs that a test can change."""
    state = {"inputs": GateInputs(
        provider_urls=(OPENROUTER_API, OTHER_PROVIDER, LOCAL_SERVER),
        helper_url=HELPER,
        private_route=lambda model: ZDR_ONLY if model in ROUTES else None,
        key_attested=lambda key: key == KEY,
    )}

    class Setup:
        gate = OutboundGate(db, lambda: state["inputs"], transport=httpx.MockTransport(remote))

        @staticmethod
        def change(**fields):
            state["inputs"] = dataclasses.replace(state["inputs"], **fields)

    return Setup


def project(db, sensitivity="normal"):
    project_id = new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO projects (id, name, kind, sensitivity) VALUES (?, 'P', 'research', ?)",
        (project_id, sensitivity)))
    return project_id


def candidate(db, project_id, oa_url=OA_LINK):
    run_id, candidate_id = new_id(), new_id()

    def add(conn):
        conn.execute("INSERT INTO runs (id, project_id, kind) VALUES (?, ?, 'background')", (run_id, project_id))
        conn.execute(
            "INSERT INTO candidates (id, run_id, project_id, source, oa_url) VALUES (?, ?, ?, 'openalex', ?)",
            (candidate_id, run_id, project_id, oa_url))

    db.write(add)
    return candidate_id


def declare(db, base_url):
    db.write(lambda conn: conn.execute(
        "INSERT INTO local_declarations (id, provider, base_url, statement) VALUES (?, 'local', ?, 'statement')",
        (new_id(), base_url)))


def audit(db):
    return db.read(lambda conn: [
        (project_id, json.loads(data)) for project_id, data in
        conn.execute("SELECT project_id, data FROM audit_log WHERE event = 'outbound' ORDER BY seq")])


def decisions(db):
    return [(row["decision"], row["reason"]) for _, row in audit(db)]


def refused(client, method, url, reason, **kwargs):
    with pytest.raises(OutboundDenied) as caught:
        client.request(method, url, **kwargs)
    assert caught.value.reason == reason
    return caught.value


# One well-formed request per destination kind.
KINDS = {
    "model_provider": ("POST", CHAT, {"json": chat(), "headers": AUTH}),
    "scholarly_api": ("GET", "https://api.openalex.org/works?search=SECRET-QUERY", {}),
    "open_access": ("GET", OA_LINK, {}),
    "local_helper": ("GET", f"{HELPER}/health", {}),
    "local_provider": ("POST", f"{LOCAL_SERVER}/chat/completions", {"json": chat()}),
    "model_download": ("GET", "https://huggingface.co/example/embedding/resolve/main/model.gguf", {}),
}
ALLOWED = {
    "normal": set(KINDS),
    "private": {"model_provider", "scholarly_api", "open_access", "local_helper", "model_download"},
    "local_only": {"scholarly_api", "open_access", "local_helper", "local_provider"},
}


@pytest.mark.parametrize("level", ["normal", "private", "local_only"])
@pytest.mark.parametrize("kind", list(KINDS))
def test_each_destination_kind_at_each_level(db, remote, setup, kind, level):
    project_id = project(db, level)
    declare(db, LOCAL_SERVER)  # the conditions that can be met are met
    method, url, kwargs = KINDS[kind]
    with setup.gate.client(project_id, candidate_id=candidate(db, project_id), approved=True) as client:
        if kind in ALLOWED[level]:
            assert client.request(method, url, **kwargs).status_code == 200
            assert [str(r.url) for r in remote.received] == [url]
        else:
            refused(client, method, url, "not_allowed_at_level", **kwargs)
            assert remote.received == []
    [(row_project, row)] = audit(db)
    assert row_project == project_id
    assert row["kind"] == kind and row["sensitivity"] == level
    assert row["decision"] == ("allow" if kind in ALLOWED[level] else "deny")


# Deny by default


@pytest.mark.parametrize("url", [
    "https://evil.example/collect",
    "https://telemetry.example.com/v1/events",
    "https://openrouter.ai.evil.example/api/v1/chat/completions",
    "https://openrouter.ai@evil.example/api/v1/chat/completions",
    "https://openrouter.ai./api/v1/chat/completions",
    "http://openrouter.ai/api/v1/chat/completions",
    "https://openrouter.ai:8443/api/v1/chat/completions",
    "https://xn--penrouter-0ig.ai/api/v1/chat/completions",
    "http://127.0.0.1:9999/",
    "http://localhost:8765/health",
    "http://127.1:8765/health",
    "http://[::1]:8765/health",
    "ws://127.0.0.1:8765/health",
])
@pytest.mark.parametrize("level", ["normal", "private", "local_only"])
def test_unknown_destinations_are_refused_at_every_level(db, remote, setup, url, level):
    with setup.gate.client(project(db, level), approved=True) as client:
        refused(client, "GET", url, "unknown_destination")
    assert remote.received == []
    assert decisions(db) == [("deny", "unknown_destination")]


def test_host_case_is_normalized_not_a_bypass(db, remote, setup):
    with setup.gate.client(project(db)) as client:
        client.get("https://OPENROUTER.AI/api/v1/models")
    assert [r.url.host for r in remote.received] == ["openrouter.ai"]


@pytest.mark.parametrize("kind", ["model_provider", "local_helper", "local_provider"])
def test_missing_inputs_refuse(db, remote, setup, kind):
    setup.change(provider_urls=(), helper_url=None)
    method, url, kwargs = KINDS[kind]
    with setup.gate.client(project(db)) as client:
        refused(client, method, url, "unknown_destination", **kwargs)
    assert remote.received == []


@pytest.mark.parametrize("level", ["normal", "private", "local_only"])
def test_empty_inputs_leave_only_the_fixed_sources(db, remote, level):
    gate = OutboundGate(db, lambda: GateInputs(), transport=httpx.MockTransport(remote))
    project_id = project(db, level)
    declare(db, LOCAL_SERVER)
    with gate.client(project_id, approved=True) as client:
        for kind in ("model_provider", "local_helper", "local_provider"):
            method, url, kwargs = KINDS[kind]
            refused(client, method, url, "unknown_destination", **kwargs)
        assert remote.received == []
        client.get(KINDS["scholarly_api"][1])  # fixed in the module, allowed at every level
    assert len(remote.received) == 1


def test_unknown_project_is_refused(db, remote, setup):
    with setup.gate.client(new_id()) as client:
        refused(client, "GET", f"{HELPER}/health", "unknown_project")
    assert remote.received == []
    [(_, row)] = audit(db)
    assert row["sensitivity"] is None


def test_open_access_host_must_come_from_a_named_candidate_of_the_project(db, remote, setup):
    project_id, other_id = project(db), project(db)
    mine, theirs = candidate(db, project_id), candidate(db, other_id, "https://other-host.example.org/p.pdf")
    no_link = candidate(db, project_id, None)
    loopback = candidate(db, project_id, "http://127.0.0.1:9999/p.pdf")
    for candidate_id, url in [(None, OA_LINK), (theirs, "https://other-host.example.org/p.pdf"),
                              (no_link, OA_LINK), (loopback, "http://127.0.0.1:9999/p.pdf"),
                              (new_id(), OA_LINK)]:
        with setup.gate.client(project_id, candidate_id=candidate_id) as client:
            refused(client, "GET", url, "unknown_destination")
    assert remote.received == []
    with setup.gate.client(project_id, candidate_id=mine) as client:
        client.get("https://repository.example.org/other/path.pdf")  # the host, not only the exact link
        refused(client, "GET", "https://evil.example/p.pdf", "unknown_destination")
    assert len(remote.received) == 1


def test_local_only_needs_the_researchers_approval_for_outside_sources(db, remote, setup):
    project_id = project(db, "local_only")
    candidate_id = candidate(db, project_id)
    for approved in (False, None, 1, "yes"):
        with setup.gate.client(project_id, candidate_id=candidate_id, approved=approved) as client:
            refused(client, "GET", "https://api.crossref.org/works/10.1000/xyz", "not_approved")
            refused(client, "GET", OA_LINK, "not_approved")
    assert remote.received == []
    with setup.gate.client(project_id, candidate_id=candidate_id, approved=True) as client:
        client.get("https://export.arxiv.org/api/query?search_query=x")
        client.get(OA_LINK)
        # approval does not open what the level forbids
        refused(client, *KINDS["model_download"][:2], "not_allowed_at_level")
        refused(client, "POST", CHAT, "not_allowed_at_level", json=chat(), headers=AUTH)
    assert len(remote.received) == 2


def test_tightening_a_project_applies_to_the_next_request(db, remote, setup):
    project_id = project(db)
    with setup.gate.client(project_id) as client:
        client.post(f"{OTHER_PROVIDER}/chat/completions", json=chat())
        db.write(lambda conn: conn.execute(
            "UPDATE projects SET sensitivity = 'local_only' WHERE id = ?", (project_id,)))
        refused(client, "POST", f"{OTHER_PROVIDER}/chat/completions", "not_allowed_at_level", json=chat())
    assert len(remote.received) == 1


# Private model requests


def private_post(setup, db, body=None, headers=AUTH, url=CHAT, **kwargs):
    with setup.gate.client(project(db, "private")) as client:
        if body is not None:
            kwargs["json"] = body
        return client.post(url, headers=headers, **kwargs)


def test_private_request_with_zdr_and_function_tools_is_allowed(db, remote, setup):
    body = chat(tools=[{"type": "function", "function": {"name": "search_passages", "parameters": {}}}],
                tool_choice="auto", reasoning={"effort": "low"}, stream=True, usage={"include": True},
                models=["example/model-b"], max_tokens=100, temperature=0.2)
    assert private_post(setup, db, body).status_code == 200
    assert json.loads(remote.received[0].content) == body


@pytest.mark.parametrize(("body", "reason"), [
    ({k: v for k, v in chat().items() if k != "provider"}, "missing_flags"),
    (chat(provider={"zdr": False}), "missing_flags"),
    (chat(provider={"zdr": 1}), "missing_flags"),
    (chat(provider={"zdr": "true"}), "missing_flags"),
    (chat(provider={"zdr": None}), "missing_flags"),
    (chat(provider={"order": ["x"]}), "missing_flags"),
    (chat(provider=True), "missing_flags"),
    (chat(plugins=[{"id": "web"}]), "unsupported_feature"),
    (chat(plugins=[]), "unsupported_feature"),
    (chat(plugins=[{"id": "file-parser"}]), "unsupported_feature"),
    (chat(web_search_options={"search_context_size": "high"}), "unsupported_feature"),
    (chat(preset="my-preset"), "unsupported_feature"),
    (chat(transforms=["middle-out"]), "unsupported_feature"),
    (chat(tools=[{"type": "openrouter:web_search"}]), "unsupported_feature"),
    (chat(tools=[{"type": "web_search"}]), "unsupported_feature"),
    (chat(tools=[{"function": {"name": "f"}}]), "unsupported_feature"),
    (chat(tools=["web"]), "unsupported_feature"),
    (chat(tools={"type": "function"}), "unchecked_request"),
    (chat(model="example/model-a:online"), "unsupported_feature"),
    (chat(model="@preset/research"), "unsupported_feature"),
    (chat(models=["example/model-b:online"]), "unsupported_feature"),
    (chat(messages=[{"role": "user", "content": [
        {"type": "text", "text": "x"}, {"type": "file", "file": {"filename": "a.pdf", "file_data": "data:"}}]}]),
     "unsupported_feature"),
    (chat(messages="hello"), "unchecked_request"),
    (chat(model="example/not-listed"), "route_not_allowed"),
    (chat(models=["example/not-listed"]), "route_not_allowed"),
    (chat(models="example/model-b"), "unchecked_request"),
    (chat(model=["example/model-a"]), "route_not_allowed"),
    ({k: v for k, v in chat().items() if k != "model"}, "route_not_allowed"),
    ([chat()], "unchecked_request"),
])
def test_private_refuses_requests_that_cannot_carry_the_controls(db, remote, setup, body, reason):
    with pytest.raises(OutboundDenied) as caught:
        private_post(setup, db, body)
    assert caught.value.reason == reason
    assert remote.received == []
    assert decisions(db) == [("deny", reason)]


@pytest.mark.parametrize("content", [
    b'{"model": "example/model-a", "messages": [], "provider": {"zdr": false}, "provider": {"zdr": true}}',
    b'{"model": "example/model-a", "messages": [], "provider": {"zdr": true, "zdr": false}}',
    b"model=example/model-a&provider.zdr=true",
    b"",
    b"[" * 100_000,
])
def test_private_refuses_bodies_it_cannot_read_unambiguously(db, remote, setup, content):
    with pytest.raises(OutboundDenied, match="unchecked_request"):
        private_post(setup, db, content=content)
    assert remote.received == []


def test_private_refuses_a_streamed_body_and_other_methods(db, remote, setup):
    with pytest.raises(OutboundDenied, match="unchecked_request"):
        private_post(setup, db, content=iter([json.dumps(chat()).encode()]))
    with setup.gate.client(project(db, "private")) as client:
        refused(client, "GET", f"{OPENROUTER_API}/models", "unchecked_request", headers=AUTH)
        refused(client, "PUT", CHAT, "unchecked_request", json=chat(), headers=AUTH)
    assert remote.received == []


def test_private_refuses_other_providers(db, remote, setup):
    with pytest.raises(OutboundDenied, match="not_openrouter"):
        private_post(setup, db, chat(), url=f"{OTHER_PROVIDER}/chat/completions")
    assert remote.received == []


def test_private_needs_the_allowlist_entrys_own_flags_and_always_zdr(db, remote, setup):
    setup.change(private_route=lambda model: {"provider": {"zdr": True, "data_collection": "deny"}})
    with pytest.raises(OutboundDenied, match="missing_flags"):
        private_post(setup, db, chat())
    private_post(setup, db, chat(provider={"zdr": True, "data_collection": "deny"}))
    setup.change(private_route=lambda model: {})  # an entry cannot waive provider.zdr
    with pytest.raises(OutboundDenied, match="missing_flags"):
        private_post(setup, db, chat(provider={}))
    for flags in (None, ["provider"], "zdr"):
        setup.change(private_route=lambda model, flags=flags: flags)
        with pytest.raises(OutboundDenied, match="route_not_allowed"):
            private_post(setup, db, chat())
    assert len(remote.received) == 1


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer sk-or-v1-other-key"}, {"Authorization": f"Basic {KEY}"},
    {"Authorization": "Bearer "}, {"Authorization": KEY},
])
def test_private_refuses_a_key_without_a_current_confirmation(db, remote, setup, headers):
    with pytest.raises(OutboundDenied, match="key_not_confirmed"):
        private_post(setup, db, chat(), headers=headers)
    assert remote.received == []


def test_private_needs_a_confirmation_that_is_exactly_true(db, remote, setup):
    setup.change(key_attested=lambda key: 1)
    with pytest.raises(OutboundDenied, match="key_not_confirmed"):
        private_post(setup, db, chat())
    assert remote.received == []


@pytest.mark.parametrize("missing", ["private_route", "key_attested"])
def test_private_without_its_inputs_refuses(db, remote, setup, missing):
    setup.change(**{missing: None})
    with pytest.raises(OutboundDenied, match="private_inputs_missing"):
        private_post(setup, db, chat())
    assert remote.received == []


def test_an_input_that_fails_sends_nothing(db, remote, setup):
    def broken(key):
        raise RuntimeError("credential store unavailable")

    setup.change(key_attested=broken)
    with pytest.raises(RuntimeError, match="credential store"):
        private_post(setup, db, chat())
    assert remote.received == []

    def broken_inputs():
        raise RuntimeError("settings unreadable")

    gate = OutboundGate(db, broken_inputs, transport=httpx.MockTransport(remote))
    with gate.client(project(db)) as client, pytest.raises(RuntimeError, match="settings unreadable"):
        client.get(f"{HELPER}/health")
    assert remote.received == []


def test_private_rules_do_not_apply_at_normal(db, remote, setup):
    with setup.gate.client(project(db)) as client:
        client.post(CHAT, json=chat(plugins=[{"id": "web"}], provider={}))  # no key either
    assert len(remote.received) == 1


# Audit records


def test_audit_rows_record_the_decision_without_content(db, remote, setup):
    project_id = project(db, "private")
    with setup.gate.client(project_id, headers={"X-Trace": "SECRET-HEADER"}) as client:
        client.post(f"{CHAT}?session=SECRET-PARAM", json=chat(), headers=AUTH)
        refused(client, "POST", CHAT, "missing_flags", json=chat(provider={}), headers=AUTH)
        client.get("https://api.openalex.org/works?search=SECRET-QUERY&filter=SECRET-FILTER")
        refused(client, "GET", "https://evil.example/SECRET-PATH?q=SECRET-QUERY", "unknown_destination")
    rows = audit(db)
    assert [row for _, row in rows] == [
        {"decision": "allow", "reason": None, "kind": "model_provider", "destination": "https://openrouter.ai:443",
         "method": "POST", "sensitivity": "private", "approved": False},
        {"decision": "deny", "reason": "missing_flags", "kind": "model_provider",
         "destination": "https://openrouter.ai:443", "method": "POST", "sensitivity": "private", "approved": False},
        {"decision": "allow", "reason": None, "kind": "scholarly_api", "destination": "https://api.openalex.org:443",
         "method": "GET", "sensitivity": "private", "approved": False},
        {"decision": "deny", "reason": "unknown_destination", "kind": None, "destination": "https://evil.example:443",
         "method": "GET", "sensitivity": "private", "approved": False},
    ]
    assert {row_project for row_project, _ in rows} == {project_id}
    stored = db.read(lambda conn: conn.execute(
        "SELECT group_concat(data) || group_concat(event) FROM audit_log").fetchone()[0])
    for secret in ("SECRET", KEY, "example/model-a", "/api/v1", "works", "search"):
        assert secret not in stored


def test_ipv6_destinations_are_shown_with_brackets(db, remote, setup):
    setup.change(helper_url="http://[::1]:8765")
    with setup.gate.client(project(db)) as client:
        client.get("http://[::1]:8765/health")
    assert audit(db)[0][1]["destination"] == "http://[::1]:8765"


def test_nothing_is_sent_when_the_audit_row_cannot_be_written(db, remote, setup):
    db.write(lambda conn: conn.execute(
        "CREATE TRIGGER audit_full BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'disk full'); END"))
    with setup.gate.client(project(db)) as client, pytest.raises(sqlite3.IntegrityError, match="disk full"):
        client.get(f"{HELPER}/health")
    assert remote.received == []


def test_concurrent_clients_get_one_row_per_decision(db, remote, setup):
    project_id = project(db)
    errors = []

    def work():
        try:
            with setup.gate.client(project_id) as client:
                for _ in range(10):
                    client.get(f"{HELPER}/health")
                    with pytest.raises(OutboundDenied):
                        client.get("https://evil.example/")
        except Exception as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(remote.received) == 80
    assert sorted(decisions(db)) == [("allow", None)] * 80 + [("deny", "unknown_destination")] * 80


# Bypass attempts


@pytest.mark.parametrize("follow", [True, False])
@pytest.mark.parametrize("location", [
    "https://evil.example/collect",
    "//evil.example/collect",
    "https:evil.example",
    "http://openrouter.ai/api/v1/chat/completions",
    "https://openrouter.ai:8443/api/v1/chat/completions",
    f"{HELPER}/health",
    f"{LOCAL_SERVER}/chat/completions",
    "https://api.openalex.org/works",
    "http://[::1",
])
def test_a_redirect_to_another_origin_is_refused(db, remote, setup, follow, location):
    remote.redirects[CHAT] = (307, location)
    with setup.gate.client(project(db), follow_redirects=follow) as client:
        denied = refused(client, "POST", CHAT, "cross_origin_redirect", json=chat())
    assert [str(r.url) for r in remote.received] == [CHAT]
    assert decisions(db) == [("allow", None), ("deny", "cross_origin_redirect")]
    assert audit(db)[1][1]["destination"] == denied.destination
    assert denied.destination in {"https://evil.example:443", "http://openrouter.ai:80", "https://openrouter.ai:8443",
                                  "http://127.0.0.1:8765", "http://127.0.0.1:11434",
                                  "https://api.openalex.org:443", "unknown"}


@pytest.mark.parametrize("location", ["/api/v1/other", f"{OPENROUTER_API}/other", "https://OpenRouter.ai:443/api/v1/other"])
def test_a_same_origin_redirect_is_checked_again(db, remote, setup, location):
    remote.redirects[CHAT] = (307, location)
    with setup.gate.client(project(db, "private"), follow_redirects=True) as client:
        assert client.post(CHAT, json=chat(), headers=AUTH).status_code == 200
    assert [str(r.url) for r in remote.received] == [CHAT, f"{OPENROUTER_API}/other"]
    assert decisions(db) == [("allow", None), ("allow", None)]


def test_a_same_origin_redirect_the_policy_refuses_is_not_followed(db, remote, setup):
    remote.redirects[CHAT] = (303, "/api/v1/other")  # 303 turns the POST into a GET, which Private cannot check
    with setup.gate.client(project(db, "private"), follow_redirects=True) as client:
        refused(client, "POST", CHAT, "unchecked_request", json=chat(), headers=AUTH)
    assert [str(r.url) for r in remote.received] == [CHAT]


@pytest.mark.parametrize("option", [
    {"transport": httpx.MockTransport(lambda r: httpx.Response(200))},
    {"mounts": {"all://": httpx.MockTransport(lambda r: httpx.Response(200))}},
    {"proxy": "http://127.0.0.1:3128"},
    {"verify": False},
    {"trust_env": True},
    {"app": object()},
])
def test_clients_refuse_options_that_change_where_requests_go(db, setup, option):
    with pytest.raises(TypeError):
        setup.gate.client(project(db), **option)
    with pytest.raises(TypeError):
        setup.gate.async_client(project(db), **option)


def test_every_way_of_sending_passes_the_gate(db, remote, setup):
    with setup.gate.client(project(db), base_url=OPENROUTER_API) as client:
        refused(client, "GET", "https://evil.example/", "unknown_destination")
        with pytest.raises(OutboundDenied):
            client.send(httpx.Request("GET", "https://evil.example/"))
        with pytest.raises(OutboundDenied), client.stream("GET", "https://evil.example/"):
            pass
        client.get("/models")  # relative to base_url
    assert [str(r.url) for r in remote.received] == [f"{OPENROUTER_API}/models"]


@pytest.mark.parametrize("kwargs", [
    {"headers": {"Host": "evil.example"}},
    {"headers": {"Host": "api.openalex.org:8443"}},
    {"extensions": {"sni_hostname": "evil.example"}},
    {"extensions": {"sni_hostname": "api.openalex.org"}},
])
def test_a_request_must_name_the_host_it_connects_to(db, remote, setup, kwargs):
    with setup.gate.client(project(db)) as client:
        refused(client, "GET", "https://api.openalex.org/works", "host_mismatch", **kwargs)
        client.get("https://api.openalex.org/works", headers={"Host": "api.openalex.org"})
    assert [r.headers["host"] for r in remote.received] == ["api.openalex.org"]
    assert decisions(db) == [("deny", "host_mismatch"), ("allow", None)]


def test_a_helper_url_off_loopback_is_not_the_helper(db, remote, setup):
    setup.change(helper_url="https://helper.example")
    with setup.gate.client(project(db, "local_only")) as client:
        refused(client, "GET", "https://helper.example/health", "unknown_destination")
    assert remote.received == []


def test_refusal_is_not_a_network_error():
    assert not issubclass(OutboundDenied, (httpx.HTTPError, OSError))


# The async client


@pytest.mark.asyncio
async def test_async_client_checks_and_audits_the_same_way(db, remote, setup):
    project_id = await asyncio.to_thread(project, db, "private")
    async with setup.gate.async_client(project_id) as client:
        assert (await client.post(CHAT, json=chat(), headers=AUTH)).status_code == 200
        with pytest.raises(OutboundDenied, match="missing_flags"):
            await client.post(CHAT, json=chat(provider={}), headers=AUTH)
        with pytest.raises(OutboundDenied, match="not_allowed_at_level"):
            await client.post(f"{LOCAL_SERVER}/chat/completions", json=chat())
        remote.redirects[f"{OPENROUTER_API}/models"] = (302, "https://evil.example/")
        with pytest.raises(OutboundDenied, match="unchecked_request"):
            await client.get(f"{OPENROUTER_API}/models")
    async with setup.gate.async_client(await asyncio.to_thread(project, db), follow_redirects=True) as client:
        with pytest.raises(OutboundDenied, match="cross_origin_redirect"):
            await client.get(f"{OPENROUTER_API}/models")
    assert [str(r.url) for r in remote.received] == [CHAT, f"{OPENROUTER_API}/models"]
    assert await asyncio.to_thread(decisions, db) == [
        ("allow", None), ("deny", "missing_flags"), ("deny", "not_allowed_at_level"),
        ("deny", "unchecked_request"), ("allow", None), ("deny", "cross_origin_redirect")]
