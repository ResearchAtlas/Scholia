"""What the settings pages and the model picker read and write (slice-1 spec F12, sections
4.4 and 8; the stage walk's Settings): providers turned off, the models a provider offers,
model windows, effort steps, the researcher's recent models, and instruction files that
changed since an editor read them."""

import asyncio

import httpx
import pytest

from backend import budget_router, openrouter_client, providers
from backend.db import new_id
from backend.providers import MIN_WINDOW, Provider
from scholia_app import MockProvider, background_idle, send, started

pytestmark = pytest.mark.asyncio

OPENROUTER = Provider("openrouter", "openrouter", "https://openrouter.ai/api/v1")
LOCAL = Provider("local", "openai-compatible", "http://127.0.0.1:11434/v1")
RECOMMENDED = sorted(budget_router.RECOMMENDED)


class CatalogProvider(MockProvider):
    """A MockProvider whose model listing holds `rows`."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def __call__(self, request):
        if request.url.path.endswith("/models"):
            self.requests.append((request.method, request.url.path, None))
            return httpx.Response(200, json={"data": self.rows})
        return await super().__call__(request)


@pytest.fixture(autouse=True)
def fresh_catalog():
    openrouter_client.clear_cache()
    yield
    openrouter_client.clear_cache()


async def save(client, updates, project_id=None):
    current = (await client.get("/api/settings", params={"project_id": project_id} if project_id else {})).json()
    return await client.put("/api/settings", json={"hash": current["hash"], "updates": updates,
                                                   **({"project_id": project_id} if project_id else {})})


@pytest.mark.parametrize("table, model, reported, expected", [
    ({}, "m", 128000, (128000, None, 128000, "ok")),
    ({"default_window": 32768}, "m", 128000, (128000, 32768, 32768, "ok")),  # a setting lowers a report
    ({"windows": {"m": 262144}}, "m", 128000, (128000, 262144, 128000, "ok")),  # never raises it
    ({"default_window": 8192, "windows": {"m": 16384}}, "m", None, (None, 16384, 16384, "ok")),  # the model's wins
    ({"default_window": 8192}, "m", None, (None, 8192, 8192, "ok")),  # fills a window not reported
    ({}, "m", None, (None, None, None, "needed")),
    ({}, "m", 2048, (2048, None, 2048, "too_small")),
    ({"windows": {"m": 8192}}, "m", 2048, (2048, 8192, 2048, "too_small")),
    ({}, "m", "128000", (None, None, None, "needed")),  # a reported value that is not a number
])
async def test_a_models_window_follows_section_8(table, model, reported, expected):
    found = providers.window(table, model, reported)
    assert (found["reported"], found["setting"], found["in_use"], found["status"]) == expected
    assert MIN_WINDOW == 4096


async def test_the_models_a_provider_offers():
    some = RECOMMENDED[0]
    assert providers.offered({}, OPENROUTER, some, budget_router.RECOMMENDED)
    assert not providers.offered({}, OPENROUTER, "x/other", budget_router.RECOMMENDED)  # Recommended by default
    assert providers.offered({"models": "all"}, OPENROUTER, "x/other", budget_router.RECOMMENDED)
    assert providers.offered({}, LOCAL, "llama3", budget_router.RECOMMENDED)  # All by default elsewhere
    assert providers.offered({"models": ["llama3"]}, LOCAL, "llama3", budget_router.RECOMMENDED)
    assert not providers.offered({"models": ["llama3"]}, LOCAL, "qwen", budget_router.RECOMMENDED)
    for table in ({}, {"models": "all"}, {"models": ["auto"]}):  # the id means Auto, never a model
        assert not providers.offered(table, LOCAL, "auto", budget_router.RECOMMENDED)
    plan = budget_router.create_run_plan("hi", "auto", lambda m: None, is_openrouter=False, picked=["auto"],
                                         offered=lambda m: providers.offered({"models": ["auto"]}, LOCAL, m,
                                                                             budget_router.RECOMMENDED))
    assert plan.model is None  # Auto never resolves to a picked model named auto


async def test_provider_model_choice_and_off_are_validated(tmp_path):
    async with started(tmp_path / "data") as client:
        for good in ("recommended", "all", ["a/b", "c/d"]):
            assert (await save(client, {"providers.openrouter.models": good})).status_code == 200, good
        for bad in ("some", 3, [""]):
            response = await save(client, {"providers.openrouter.models": bad})
            assert (response.status_code, response.json()["code"]) == (400, "invalid_setting"), bad
        assert (await save(client, {"providers.openrouter.enabled": "no"})).json()["code"] == "invalid_setting"


async def test_a_provider_turned_off_is_listed_but_never_called(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        assert (await save(client, {"providers.openrouter.enabled": False})).status_code == 200
        listed = (await client.get("/api/providers")).json()["providers"]
        assert [(p["name"], p["has_key"], p["enabled"]) for p in listed] == [("openrouter", True, False)]
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert (response.status_code, response.json()["code"]) == (400, "no_provider")
        assert provider.chats == []
        assert providers.gate_inputs(tmp_path / "data").provider_urls == ()  # its origin is not allowed
        # Its key can still be replaced, and it comes back on.
        assert (await client.put("/api/keys/openrouter", json={"key": "sk-or-test-other"})).status_code == 200
        assert (await save(client, {"providers.openrouter.enabled": True})).status_code == 200
        assert (await client.get("/api/providers")).json()["providers"][0]["enabled"] is True


async def test_the_model_listing_says_window_offer_and_effort(tmp_path):
    rows = [{"id": RECOMMENDED[0], "context_length": 128000}, {"id": "x/unlisted", "context_length": 2048},
            {"id": "x/no-window"}]
    async with started(tmp_path / "data", CatalogProvider(rows)) as client:
        assert (await save(client, {"providers.openrouter.windows": {"x/no-window": 16384}})).status_code == 200
        models = {m["id"]: m for m in (await client.get("/api/providers/openrouter/models")).json()["models"]}
        first = models[RECOMMENDED[0]]
        assert (first["window"]["in_use"], first["window"]["status"]) == (128000, "ok")
        assert first["offered"] and first["recommended"]
        assert first["effort"] == {"surface": "unknown", "steps": []}  # not yet checked: "Default"
        assert models["x/unlisted"]["window"]["status"] == "too_small"
        assert not models["x/unlisted"]["offered"]  # OpenRouter offers Recommended until told otherwise
        assert models["x/no-window"]["window"] == {"reported": None, "setting": 16384, "in_use": 16384, "status": "ok"}


async def test_recent_models_are_the_last_three_chosen(tmp_path):
    async with started(tmp_path / "data") as client:
        assert (await save(client, {"providers.openrouter.models": "all"})).status_code == 200
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        for model in ("a/one", "b/two", "a/one", "c/three", None, "d/four"):
            await send(client, conversation, **({"model": model} if model else {}))  # None is Auto
            await asyncio.sleep(0.01)  # distinct start times
        recent = (await client.get("/api/models/recent")).json()["models"]
        assert recent == [{"provider": "openrouter", "model": m} for m in ("d/four", "c/three", "a/one")]


async def test_an_instruction_file_changed_since_it_was_read_is_not_overwritten(tmp_path):
    async with started(tmp_path / "data") as client:
        read = (await client.get("/api/instructions")).json()
        assert (read["text"], read["combined_bytes"]) == ("", 0)
        saved = await client.put("/api/instructions", json={"text": "Cite sources.", "hash": read["hash"]})
        assert saved.status_code == 200
        (tmp_path / "data" / "AGENTS.md").write_text("Edited by hand.")
        stale = await client.put("/api/instructions", json={"text": "Mine.", "hash": saved.json()["hash"]})
        assert (stale.status_code, stale.json()["code"]) == (409, "settings_changed")
        assert (tmp_path / "data" / "AGENTS.md").read_text() == "Edited by hand."
        fresh = (await client.get("/api/instructions")).json()
        assert fresh["text"] == "Edited by hand." and fresh["combined_bytes"] == len("Edited by hand.")
        assert (await client.put("/api/instructions", json={"text": "Mine."})).status_code == 200  # no hash: as before


async def test_a_pick_of_nothing_offers_nothing():
    assert not providers.offered({"models": []}, OPENROUTER, RECOMMENDED[0], budget_router.RECOMMENDED)
    assert not providers.offered({"models": []}, LOCAL, "llama3", budget_router.RECOMMENDED)


async def test_two_editors_saving_the_same_read_cannot_both_win(tmp_path):
    async with started(tmp_path / "data") as client:
        read = (await client.get("/api/instructions")).json()
        saves = await asyncio.gather(*(client.put("/api/instructions", json={"text": text, "hash": read["hash"]})
                                       for text in ("First.", "Second.")))
        assert sorted(r.status_code for r in saves) == [200, 409]
        kept = (await client.get("/api/instructions")).json()["text"]
        assert kept == ("First." if saves[0].status_code == 200 else "Second.")


async def test_the_combined_size_is_measured_before_the_cap_cuts_it(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        await client.put("/api/instructions", json={"text": "x" * 30_000})
        await client.put("/api/instructions", json={"text": "y" * 10_000, "project_id": project})
        read = (await client.get("/api/instructions", params={"project_id": project})).json()
        assert read["combined_bytes"] == 40_002 > read["cap_bytes"]  # joined by a blank line
        assert (await client.get("/api/instructions")).json()["combined_bytes"] == 30_000


async def test_invalid_settings_are_reported_by_key_and_line(tmp_path):
    async with started(tmp_path / "data") as client:
        (tmp_path / "data" / "config.toml").write_text("[limits]\nagent_steps = 0\n")
        loaded = (await client.get("/api/settings")).json()
        assert loaded["problems"] == [{"key": "limits.agent_steps", "line": 2}]
        assert loaded["values"]["limits"]["agent_steps"] == 12  # the default
        (tmp_path / "data" / "config.toml").write_text("[limits\n")
        assert (await client.get("/api/settings")).json()["problems"] == [{"key": None, "line": 1}]


async def test_recent_models_survive_many_repeats_of_one(tmp_path):
    async with started(tmp_path / "data") as client:
        assert (await save(client, {"providers.openrouter.models": "all"})).status_code == 200
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        for model in ("c/three", "b/two"):
            await send(client, conversation, model=model)
            await asyncio.sleep(0.01)

        def repeat(conn):  # 250 later turns that chose a/one, recorded as admission records them
            for _ in range(250):
                run = new_id()
                conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, workflow, status)"
                             " SELECT ?, project_id, id, 'turn', 'agent', 'succeeded' FROM conversations WHERE id = ?",
                             (run, conversation))
                conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 0, 'route', ?)",
                             (run, '{"route": "openrouter:a/one", "plan": {"policy_reason": "chosen_model", "model": "a/one"}}'))
        await asyncio.to_thread(client.state["db"].write, repeat)
        recent = (await client.get("/api/models/recent")).json()["models"]
        assert [m["model"] for m in recent] == ["a/one", "b/two", "c/three"]


async def test_the_size_counts_replaced_characters_as_the_cap_does(tmp_path):
    async with started(tmp_path / "data") as client:
        (tmp_path / "data" / "AGENTS.md").write_bytes(b"\xff" * 12_000)  # each becomes a 3-byte replacement
        read = (await client.get("/api/instructions")).json()
        assert read["combined_bytes"] == 36_000 > read["cap_bytes"]


async def test_an_unreadable_settings_file_is_reported(tmp_path, monkeypatch):
    from backend import settings as settings_module

    def unreadable(path):
        raise PermissionError("no")

    monkeypatch.setattr(settings_module, "_read", unreadable)
    loaded = settings_module.load_settings(tmp_path)
    assert loaded.problems == [{"key": None, "line": None}]


async def test_an_instruction_file_not_in_utf8_is_flagged_before_it_is_rewritten(tmp_path):
    async with started(tmp_path / "data") as client:
        assert (await client.get("/api/instructions")).json()["replaced"] is False
        (tmp_path / "data" / "AGENTS.md").write_bytes(b"caf\xe9")
        read = (await client.get("/api/instructions")).json()
        assert read["replaced"] is True and read["text"] == "caf�"


async def test_admission_keeps_to_the_models_a_provider_offers(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": "x/not-recommended"})
        assert (refused.status_code, refused.json()["code"]) == (400, "model_not_offered")
        await send(client, conversation, model="auto")  # Auto: the router's preferred, offered model
        assert provider.answers[-1]["model"] == budget_router.MODEL_TIERS["mid"][0]
        await save(client, {"providers.openrouter.models": ["x/picked"]})
        await send(client, conversation, model="auto")  # under a Pick, Auto picks among the picked
        assert provider.answers[-1]["model"] == "x/picked"
        await save(client, {"providers.openrouter.models": []})
        nothing = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert (nothing.status_code, nothing.json()["code"]) == (400, "model_needed")


async def test_auto_and_a_chosen_model_keep_to_usable_windows(tmp_path):
    rows = [{"id": "x/small", "context_length": 2048}, {"id": "x/unknown"}, {"id": "x/good", "context_length": 32768}]
    provider = CatalogProvider(rows)
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.openrouter.models": ["x/small", "x/unknown", "x/good"]})
        await client.get("/api/providers/openrouter/models")  # the catalog, read as the picker reads it
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        for model in ("x/small", "x/unknown"):  # too small; no window reported or set (section 8)
            refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                        json={"content": "hi", "model": model})
            assert (refused.status_code, refused.json()["code"]) == (400, "model_window"), model
        await send(client, conversation, model="auto")  # Auto passes over both
        assert provider.answers[-1]["model"] == "x/good"
        await save(client, {"providers.openrouter.windows": {"x/unknown": 8192}})
        await send(client, conversation, model="x/unknown")  # a window set makes it usable
        assert provider.answers[-1]["model"] == "x/unknown"


async def test_a_title_run_does_not_call_a_model_no_longer_offered(tmp_path):
    data = tmp_path / "data"
    first = MockProvider()
    held = asyncio.Event()

    async def hold(body):  # the title call never finishes: shutdown interrupts it
        await held.wait()

    first.title_replies.append(hold)
    async with started(data, first) as client:
        keyring = client.keyring
        await save(client, {"providers.openrouter.models": "all"})
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation, model="x/picked")
        while not first.titles:
            await asyncio.sleep(0.01)
    config = data / "config.toml"  # while closed (a running call keeps its provider's settings fixed)
    config.write_text(config.read_text().replace('models = "all"', 'models = ["y/other"]'))
    second = MockProvider()
    async with started(data, second, keyring=keyring, setup=False) as client:
        await background_idle(client)  # recovery restarts the interrupted title run
        assert second.titles == []  # ... which does not call a model its provider no longer offers


async def test_auto_records_the_tier_of_the_model_it_picked():
    plan = budget_router.create_run_plan("hello", "auto", lambda m: providers.Route(OPENROUTER, m),
                                         offered=lambda m: m == budget_router.MODEL_TIERS["budget"][0])
    assert (plan.model, plan.model_tier, plan.policy_reason) == (
        budget_router.MODEL_TIERS["budget"][0], "budget", "auto_offered")


async def test_the_pickers_choice_is_a_validated_setting(tmp_path):
    async with started(tmp_path / "data") as client:
        for good in ({"ui.model.id": "auto", "ui.model.provider": None},
                     {"ui.model.id": "llama3:8b", "ui.model.provider": "lab:v2"},  # colons in either part
                     {"ui.model.id": None, "ui.model.provider": None}):
            assert (await save(client, good)).status_code == 200, good
        values = (await client.get("/api/settings")).json()["values"]["ui"]
        assert "model" not in values or not values["model"]
        for bad in ({"ui.model.id": " "}, {"ui.model.provider": 3}, {"ui.model": "auto"}):
            assert (await save(client, bad)).json()["code"] == "invalid_setting", bad


async def test_recent_models_keep_a_provider_name_with_a_colon(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await save(client, {'providers."lab:v2".kind': "openai-compatible",
                            'providers."lab:v2".base_url': "https://lab.example/v1"})

        def record(conn):
            run = new_id()
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, workflow, status)"
                         " SELECT ?, project_id, id, 'turn', 'agent', 'succeeded' FROM conversations WHERE id = ?",
                         (run, conversation))
            conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 0, 'route', ?)",
                         (run, '{"route": "lab:v2:llama3:8b", "plan": {"policy_reason": "chosen_model", "model": "llama3:8b"}}'))
            other = new_id()  # provider "lab", model "v2:llama3:8b": the same route text
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, workflow, status, started_at)"
                         " SELECT ?, project_id, id, 'turn', 'agent', 'succeeded', '2020-01-01T00:00:00.000Z'"
                         " FROM conversations WHERE id = ?", (other, conversation))
            conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 0, 'route', ?)",
                         (other, '{"route": "lab:v2:llama3:8b", "plan": {"policy_reason": "chosen_model", "model": "v2:llama3:8b"}}'))
        await asyncio.to_thread(client.state["db"].write, record)
        assert (await client.get("/api/models/recent")).json()["models"] == [
            {"provider": "lab:v2", "model": "llama3:8b"}, {"provider": "lab", "model": "v2:llama3:8b"}]


async def test_the_personal_editor_is_measured_with_the_projects_instructions(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        await client.put("/api/instructions", json={"text": "x" * 20_000})
        await client.put("/api/instructions", json={"text": "y" * 20_000, "project_id": project})
        read = (await client.get("/api/instructions", params={"with_project": project})).json()
        assert (read["text"], read["combined_bytes"]) == ("x" * 20_000, 40_002)
        assert (await client.get("/api/instructions", params={"with_project": "nope"})).status_code == 404


async def test_the_editor_gets_the_other_files_size_and_empty_files_add_no_separator(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        await client.put("/api/instructions", json={"text": ""})  # an empty personal file
        await client.put("/api/instructions", json={"text": "y" * 100, "project_id": project})
        own = (await client.get("/api/instructions", params={"project_id": project})).json()
        assert (own["other_bytes"], own["combined_bytes"]) == (0, 100)  # no separator for an empty file
        await client.put("/api/instructions", json={"text": "x" * 10})
        personal = (await client.get("/api/instructions", params={"with_project": project})).json()
        assert (personal["other_bytes"], personal["combined_bytes"]) == (100, 112)


async def test_an_unreadable_instruction_file_is_reported_not_an_error(tmp_path):
    import os
    async with started(tmp_path / "data") as client:
        path = tmp_path / "data" / "AGENTS.md"
        path.write_text("secret")
        os.chmod(path, 0)
        try:
            read = await client.get("/api/instructions")
            assert read.status_code == 200 and read.json()["unreadable"] is True and read.json()["text"] == ""
        finally:
            os.chmod(path, 0o600)
