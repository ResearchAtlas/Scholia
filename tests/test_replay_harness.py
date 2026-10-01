import json

import httpx
import pytest

import replay_harness
from network_guard import NetworkBlocked
from replay_harness import FIXTURES, UnrecordedRequest, replay

RECORDED = json.loads((FIXTURES / "chat_completion.json").read_text(encoding="utf-8"))[0]
PATH = RECORDED["request"]["path"]
BODY = RECORDED["request"]["body"]


def test_recorded_exchange_is_replayed():
    with replay("chat_completion") as base_url:
        response = httpx.post(base_url + PATH, json=BODY)
    assert response.status_code == 200
    assert response.json() == RECORDED["response"]["body"]


@pytest.mark.asyncio
async def test_recorded_exchange_is_replayed_async():
    reordered = dict(reversed(list(BODY.items())))  # key order is not part of the match
    with replay("chat_completion") as base_url:
        async with httpx.AsyncClient() as client:
            response = await client.post(base_url + PATH, json=reordered)
    assert response.json() == RECORDED["response"]["body"]


def test_unrecorded_request_fails_the_test():
    changed = {**BODY, "model": "example/other-model"}
    with pytest.raises(UnrecordedRequest, match="POST /api/v1/chat/completions"):
        with replay("chat_completion") as base_url:
            response = httpx.post(base_url + PATH, json=changed)
            assert response.status_code == 501


def test_each_recorded_exchange_is_served_once():
    with pytest.raises(UnrecordedRequest):
        with replay("chat_completion") as base_url:
            assert httpx.post(base_url + PATH, json=BODY).status_code == 200
            assert httpx.post(base_url + PATH, json=BODY).status_code == 501


def test_replay_server_is_unreachable_after_the_block():
    with replay("chat_completion") as base_url:
        pass
    with pytest.raises(NetworkBlocked):
        httpx.post(base_url + PATH, json=BODY)


@pytest.fixture
def two_turns(tmp_path, monkeypatch):
    """A fixture recording turn A, then turn B."""
    first = json.loads(json.dumps(RECORDED))
    second = json.loads(json.dumps(RECORDED))
    second["request"]["body"]["messages"].append({"role": "user", "content": "And another?"})
    second["response"]["body"]["id"] = "gen-replay-0002"
    (tmp_path / "two_turns.json").write_text(json.dumps([first, second]), encoding="utf-8")
    monkeypatch.setattr(replay_harness, "FIXTURES", tmp_path)
    return first, second


def test_exchanges_replay_in_recorded_order(two_turns):
    with replay("two_turns") as base_url:
        for exchange in two_turns:
            response = httpx.post(base_url + PATH, json=exchange["request"]["body"])
            assert response.json() == exchange["response"]["body"]


def test_request_out_of_recorded_order_fails_the_test(two_turns):
    first, second = two_turns
    with pytest.raises(UnrecordedRequest):
        with replay("two_turns") as base_url:
            assert httpx.post(base_url + PATH, json=second["request"]["body"]).status_code == 501
            assert httpx.post(base_url + PATH, json=first["request"]["body"]).status_code == 200
