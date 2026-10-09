"""The search index file (S1-17; slice-1 spec sections 1.5, 4.3, 7.2 and 14; ticket 17's upkeep and
deletion): the tokenizer, the file's schema, modes and metadata, staleness at open (another model or
tokenizer, a restored database, a damaged or unfinished file), secure delete checked in the file's
own bytes, and sqlite-vec failing to load. Synthetic text only."""

import asyncio
import os
import re
import stat
from pathlib import Path

import pytest

import backend.search_index as search_index
from backend.search_index import DIMENSIONS, SearchIndex, match, tokens
from scholia_app import started
from test_materials import added, project_of, rows
from test_search import CHINESE, WAGES, app, find, idle, index_rows, paper, vector

ROOT = Path(__file__).resolve().parents[1]


def words(text):
    return [token for _, _, token, _ in tokens(text)]


# The tokenizer (section 7.2: lowercased Latin words, Han bigrams, no stemming)


@pytest.mark.parametrize("text, expected", [
    ("最低工资", ["最低", "低工", "工资"]),
    ("GDP增长率", ["gdp", "增长", "长率"]),
    ("研究。方法", ["研究", "方法"]),  # punctuation splits: no bigram across it
    ("ＧＤＰ　２０２６年", ["gdp", "2026", "年"]),  # full-width forms are their usual ones
    ("COVID-19", ["covid", "19"]),
    ("Running runners ran", ["running", "runners", "ran"]),  # no stemming
    ("Résumé naïve", ["résumé", "naïve"]),
    ("Résumé", ["résumé"]),  # decomposed accents kept with their letters
    ("税", ["税"]),
    ("snake_case", ["snake", "case"]),
    ("Ελληνικά и русский", ["ελληνικά", "и", "русский"]),
])
def test_tokens(text, expected):
    assert words(text) == expected


def test_offsets_are_the_tokens_place_in_the_text():
    text = "A GDP增长率 rise"
    assert [(text[start:end], token) for start, end, token, _ in tokens(text)] == [
        ("A", "a"), ("GDP", "gdp"), ("增长", "增长"), ("长率", "长率"), ("rise", "rise")]


def test_a_query_is_its_tokens_quoted_and_never_fts5_syntax():
    assert match('NEAR(a b) OR "c" d* -e col:f ^g') == '"near" OR "a" OR "b" OR "or" OR "c" OR "d" OR "e" OR "col" OR "f" OR "g"'
    assert match("税") == '"税"*'  # a lone Han character finds the bigrams it starts
    assert match("最低工资 最低") == '"最低" OR "低工" OR "工资"'
    assert match("。、 !?") is None and match("") is None
    assert len(match(" ".join(f"w{i}" for i in range(500))).split(" OR ")) == search_index.MAX_TERMS


# The file


@pytest.mark.asyncio
async def test_the_index_file_is_owner_only_and_records_what_its_vectors_are(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        folder = client.state["data_dir"] / "index"
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        for name in ("search.sqlite3", "search.sqlite3-wal", "search.sqlite3-shm"):
            if (folder / name).exists():
                assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name
        index = client.state["index"]
        meta = dict(await asyncio.to_thread(index._read, lambda conn: conn.execute("SELECT key, value FROM index_meta").fetchall()))
        assert meta["model"] == "qwen3-embedding-0.6b" and meta["dimensions"] == str(DIMENSIONS)
        assert (meta["tokenizer"], meta["tokenizer_version"], meta["quantization"], meta["state"]) == (
            "scholia", "1", "Q8_0", "ready")
        assert re.fullmatch("[0-9a-f]{64}", meta["runtime"]) and re.fullmatch("[0-9a-f]{64}", meta["revision"])
        tables = {name for (name,) in await asyncio.to_thread(index._read, lambda conn: conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall())}
        assert {"fts_passages", "vec_passages", "vec_memory", "index_rows", "index_meta"} <= tables
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]  # applied, then taken off the queue


def test_only_the_index_module_opens_apsw_or_loads_sqlite_vec():
    found = {path.relative_to(ROOT).as_posix() for path in (ROOT / "backend").rglob("*.py")
             if re.search(r"^\s*(import|from)\s+(apsw|sqlite_vec)\b", path.read_text(), re.M)}
    assert found == {"backend/search_index.py"}


# Staleness at open


@pytest.mark.asyncio
async def test_another_model_drops_every_vector_and_embeds_again(tmp_path, monkeypatch):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        assert (await idle(client, project))["passages"]["embedded"] == 4
    real = search_index.SearchIndex.__init__
    monkeypatch.setattr(search_index.SearchIndex, "__init__", lambda self, data_dir, db, identity: real(
        self, data_dir, db, {**identity, "revision": "0" * 64}))
    async with app(tmp_path) as client:
        status = await until_embedded(client, project)
        assert status["passages"]["embedded"] == 4 and len(client.remote.indexing) == 4  # embedded again, all of them


async def until_embedded(client, project, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = await idle(client, project)
        if status["passages"]["embeddable"] and status["passages"]["embedded"] == status["passages"]["embeddable"]:
            return status
        assert asyncio.get_running_loop().time() < deadline, status
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_another_tokenizer_version_tokenizes_the_keyword_rows_again(tmp_path, monkeypatch):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
    monkeypatch.setattr(search_index, "TOKENIZER_VERSION", "2")
    async with app(tmp_path) as client:
        index = client.state["index"]
        meta = dict(await asyncio.to_thread(index._read, lambda conn: conn.execute("SELECT key, value FROM index_meta").fetchall()))
        assert meta["tokenizer_version"] == "2"
        assert (await find(client, project, "earnings"))["results"]
        assert client.remote.indexing == []  # the vectors are kept


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["restored", "damaged", "unfinished", "another schema"])
async def test_a_stale_or_damaged_file_is_rebuilt_from_the_main_database_without_holding_up_launch(tmp_path, cause):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES, CHINESE)
        await idle(client, project)
        before = sorted(r[0] for r in await index_rows(client, project))
        path = client.state["index"].path
    if cause == "restored":  # the database the index was applied to is gone: its queue's mark is lower
        import sqlite3
        with sqlite3.connect(path.parent.parent / "scholia.sqlite3") as conn:
            conn.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'index_queue'")
    elif cause == "damaged":
        data = bytearray(path.read_bytes())
        data[:100] = b"\0" * 100
        path.write_bytes(bytes(data))
    else:
        import apsw
        conn = apsw.Connection(str(path))
        key, value = ("state", "building") if cause == "unfinished" else ("schema", "0")
        conn.execute("UPDATE index_meta SET value = ? WHERE key = ?", (value, key))
        conn.close()
    async with app(tmp_path) as client:
        index = client.state["index"]
        status = await until_embedded(client, project)
        assert sorted(r[0] for r in await index_rows(client, project)) == before
        assert (await find(client, project, "最低工资"))["results"] and index.building is False
        assert status["state"] == "ready"


@pytest.mark.asyncio
async def test_a_rebuild_while_search_runs_keeps_keyword_results(tmp_path):
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        client.remote.hold = asyncio.Event()
        await client.post(f"/api/projects/{project}/index/rebuild")
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        found, again = await asyncio.gather(find(client, project, "earnings"), find(client, project, "prices"))
        assert found["results"] and again["results"]
        assert found["coverage"]["embedded"] < found["coverage"]["total"]  # meaning search covers part, said
        client.remote.hold.set()
        await until_embedded(client, project)


# Secure delete, checked in the file's own bytes


@pytest.mark.asyncio
async def test_after_a_deletion_the_index_file_and_its_wal_hold_nothing_of_the_paper(tmp_path):
    english = "Zanzibarite cormorants quarrel over vermilion parsnips"
    chinese = "鹦鹉螺化石在戈壁滩上闪烁"
    canary = paper("Canary Paper", english, chinese)
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, canary))["materials"]
        await idle(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]

        def held():
            return b"".join(f.read_bytes() for f in files if f.exists())
        embedding = vector(f"Canary Paper\nFindings\n{english}")
        first = next(i for i, value in enumerate(embedding) if value)  # its bytes from its first nonzero value
        secrets = [english.encode(), chinese.encode(), b"zanzibarite", b"vermilion", "鹦鹉".encode(), "戈壁".encode(),
                   search_index.sqlite_vec.serialize_float32(embedding)[4 * first:4 * first + 64]]
        assert all(s in held() for s in secrets[:2] + secrets[-1:])  # it was there
        assert (await client.delete(f"/api/materials/{material['id']}")).status_code == 200
        assert await index_rows(client, project) == []
        left = held()
        assert [s for s in secrets if s in left] == []
        assert not Path(f"{index.path}-wal").exists() or Path(f"{index.path}-wal").stat().st_size == 0


# sqlite-vec that cannot load


@pytest.mark.asyncio
async def test_without_sqlite_vec_search_is_keyword_only_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(search_index.sqlite_vec, "loadable_path", lambda: str(tmp_path / "missing" / "vec0"))
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        status = await idle(client, project)
        assert (status["mode"], status["reason"]) == ("keyword_only", "vectors_unavailable")
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "vectors_unavailable") and found["results"]
        assert client.remote.indexing == []
        [run] = [r for r in (await client.get("/api/activity")).json()["runs"] if r["workflow"] == "index"]
        assert run["result"] == {"mode": "keyword_only", "reason": "vectors_unavailable"}


@pytest.mark.asyncio
async def test_an_index_that_cannot_open_leaves_the_app_running_with_search_unavailable(tmp_path, monkeypatch):
    def refuse(self):
        raise OSError("no space")
    monkeypatch.setattr(SearchIndex, "open", refuse)
    async with started(tmp_path / "data", setup=False) as client:
        project = await project_of(client)
        found = (await client.post(f"/api/projects/{project}/search", json={"query": "x"})).json()
        assert (found["mode"], found["reason"], found["results"]) == ("keyword_only", "index_unavailable", [])
        assert (await client.get(f"/api/projects/{project}/index")).json()["state"] == "unavailable"
        assert os.path.exists(tmp_path / "data" / "scholia.sqlite3")


@pytest.mark.asyncio
async def test_a_restore_reopens_the_index_against_the_restored_database_and_rebuilds_it(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        backup = (await client.post("/api/backups")).json()["id"]
        await added(client, project, CHINESE)
        await idle(client, project)
        assert len(await index_rows(client, project)) == 7
        before = client.state["index"]
        assert (await client.post("/api/backups/restore", json={"generation": backup})).status_code == 200
        assert client.state["index"] is not before and before.closed
        status = await until_embedded(client, project)
        assert status["passages"] == {"indexed": 4, "embedded": 4, "embeddable": 4}  # what the backup holds
        keyword = client.state["index"].keyword
        assert await asyncio.to_thread(keyword, project, "最低工资", 50) == []
        assert (await find(client, project, "earnings"))["results"]
