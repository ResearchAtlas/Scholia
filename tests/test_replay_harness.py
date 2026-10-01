import json

import httpx
import pytest

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
