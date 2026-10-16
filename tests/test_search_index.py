"""The search index file (S1-17; slice-1 spec sections 1.5, 4.3, 7.2 and 14; ticket 17's upkeep and
deletion): the tokenizer, the file's schema, modes and metadata, staleness at open (another model or
tokenizer, a restored database, a damaged or unfinished file), secure delete checked in the file's
own bytes, and sqlite-vec failing to load. Synthetic text only."""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path

import pytest

import backend.search_index as search_index
from backend.db import DatabaseClosedError
from backend.search_index import DIMENSIONS, SearchIndex, match, tokens
from scholia_app import background_idle, run_finished, started
from test_materials import added, project_of, rows
from test_search import CHINESE, WAGES, app, find, idle, index_rows, paper, runs_of, vector

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
        assert (run["result"]["mode"], run["result"]["reason"]) == ("keyword_only", "vectors_unavailable")


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
        assert status["passages"] == {"indexed": 4, "embedded": 4, "embeddable": 4, "refused": 0}  # what the backup holds
        keyword = client.state["index"].keyword
        assert await asyncio.to_thread(keyword, project, "最低工资", 50) == []
        assert (await find(client, project, "earnings"))["results"]


@pytest.mark.asyncio
async def test_a_read_during_a_rebuild_never_makes_the_file_and_the_rebuilt_file_is_owner_only(tmp_path, monkeypatch):
    """A read that finds the file gone never makes it (only the writer's rebuild does, owner-only); it asks for that
    rebuild (PR 29 recheck 8: a missing file is damaged), which runs here once the read is refused."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        path = index.path
        await asyncio.to_thread(index._write, lambda: index._conn.close())
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        index._close_readers()
        asked = []
        monkeypatch.setattr(SearchIndex, "_rebuild_soon", lambda self, then=None: asked.append(True))
        with pytest.raises(Exception):  # as a read landing while the file is replaced: refused, the file not made
            await asyncio.to_thread(index.counts, project)
        assert not path.exists() and asked == [True]  # the read made nothing, and asked for the rebuild
        monkeypatch.undo()
        await asyncio.to_thread(index._write, index._rebuild_all)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(await index_rows(client, project)) == 4
        found = (await client.post(f"/api/projects/{project}/search", json={"query": "earnings"})).json()
        assert found["results"]


@pytest.mark.asyncio
async def test_vectors_sqlite_vec_cannot_reach_are_replaced_with_the_file_and_embedded_again_later(tmp_path, monkeypatch):
    english = "Zanzibarite cormorants quarrel over vermilion parsnips"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, paper("Canary Paper", english)))["materials"]
        await idle(client, project)
        path = client.state["index"].path
    real = search_index.sqlite_vec.loadable_path
    monkeypatch.setattr(search_index.sqlite_vec, "loadable_path", lambda: str(tmp_path / "missing" / "vec0"))
    async with app(tmp_path) as client:  # sqlite-vec does not load: the file with vectors is replaced
        status = await idle(client, project)
        assert (status["mode"], status["reason"]) == ("keyword_only", "vectors_unavailable")
        tables = {n for (n,) in await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall())}
        assert "vec_passages" not in tables and status["passages"]["indexed"] == 2
        embedding = vector(f"Canary Paper\nFindings\n{english}")
        first = next(i for i, value in enumerate(embedding) if value)
        assert search_index.sqlite_vec.serialize_float32(embedding)[4 * first:4 * first + 64] not in path.read_bytes()
    monkeypatch.setattr(search_index.sqlite_vec, "loadable_path", real)
    async with app(tmp_path) as client:  # it loads again: the passages are embedded again
        status = await until_embedded(client, project)
        assert status["passages"]["embedded"] == 2 and status["materials"][material["id"]]["embedded"] == 2


class FailingCheckpoints:
    """The writer's connection, its first `failures` WAL truncations failing with error."""

    def __init__(self, conn, error, failures):
        self.conn, self.error, self.failures, self.calls = conn, error, failures, []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *exc):
        return self.conn.__exit__(*exc)

    def wal_checkpoint(self, **options):
        self.calls.append(options)
        if len(self.calls) <= self.failures:
            raise getattr(search_index.apsw, self.error)("held off" if self.error == "BusyError" else "disk I/O error")
        return self.conn.wal_checkpoint(**options)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["BusyError", "IOError"])
async def test_a_wal_truncation_that_fails_stays_owed_and_is_done_soon_without_other_work(tmp_path, monkeypatch, error):
    """A reader holding it off, or an I/O error: the deletion's rows leave the index and the queue, the
    truncation stays owed, and it is tried again soon, with no further pass or deletion, until the file and
    its WAL hold nothing of the paper."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (0.05, 0.05))
    english = "Ocelots juggle tangerine harpsichords"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, paper("Ledger of Ocelots", english)))["materials"]
        await idle(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        real = index._conn
        index._conn = failing = FailingCheckpoints(real, error, failures=2)
        try:
            assert (await client.delete(f"/api/materials/{material['id']}")).status_code == 200
            assert await index_rows(client, project) == []  # applied, though the truncation failed
            assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
            deadline = asyncio.get_running_loop().time() + 5
            while index._truncate or len(failing.calls) < 3:  # the deletion's, then two tries of its own
                assert asyncio.get_running_loop().time() < deadline, (index._truncate, len(failing.calls))
                await asyncio.sleep(0.02)
        finally:
            index._conn = real
        assert not files[1].exists() or files[1].stat().st_size == 0
        left = b"".join(f.read_bytes() for f in files if f.exists())
        assert english.encode() not in left and b"ocelots" not in left and b"tangerine" not in left


@pytest.mark.asyncio
async def test_a_search_that_finds_the_file_unreadable_answers_without_an_error(tmp_path, monkeypatch):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)

        def unreadable(self, project_id):
            raise search_index.apsw.CorruptError("damaged")
        monkeypatch.setattr(SearchIndex, "counts", unreadable)
        response = await client.post(f"/api/projects/{project}/search", json={"query": "earnings"})
        assert response.status_code == 200 and response.json()["coverage"] == {"embedded": 0, "total": 0, "refused": 0}
        assert (await client.get(f"/api/projects/{project}/index")).status_code == 200


@pytest.mark.asyncio
async def test_an_index_built_from_another_database_file_is_rebuilt(tmp_path):
    """A restore puts another database file in place, and undoing a failed one puts the previous back;
    an index built from the other file is never taken for this one's, though the queue's mark allows it."""
    import shutil
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        database = client.state["db"].path
        index = client.state["index"]
        await asyncio.to_thread(index._write, lambda: index._conn.execute(  # a row the database never had
            "INSERT INTO fts_passages (rowid, text, project_id, passage_id) VALUES (999999, 'stale ghost', ?, 'x')",
            (project,)))
    copy = database.with_name("copy.sqlite3")
    shutil.copy2(database, copy)
    copy.replace(database)  # the same content, another file
    async with app(tmp_path) as client:
        status = await until_embedded(client, project)
        assert status["passages"]["indexed"] == 4
        index = client.state["index"]
        assert await asyncio.to_thread(index.keyword, project, "ghost", 50) == []
        meta = dict(await asyncio.to_thread(index._read, lambda conn: conn.execute("SELECT key, value FROM index_meta").fetchall()))
        assert meta["database"] == str(os.stat(database).st_ino)


@pytest.mark.asyncio
async def test_a_deleted_papers_title_leaves_the_file_when_another_paper_keeps_its_passages(tmp_path):
    """The passages stay (another paper in the project reads the same file), retitled; the deleted title's
    bytes leave the file and its WAL too."""
    file = ("Quixotic Ledger.md", b"# Plain Heading\n\n## Part\n\nPlain text with nothing special.\n")
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        [first] = (await added(client, project, file))["materials"]
        [second] = (await added(client, project, paper("Zephyr Accounts", "Other text.")))["materials"]
        await idle(client, project)
        await added(client, project, ("zephyr.md", file[1]), material_id=second["id"])  # the same bytes
        await idle(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        assert b"quixotic" in b"".join(f.read_bytes() for f in files if f.exists())
        assert (await client.delete(f"/api/materials/{first['id']}")).status_code == 200
        await idle(client, project)
        assert {r[1] for r in await index_rows(client, project)} == {second["id"]}
        left = b"".join(f.read_bytes() for f in files if f.exists())
        assert b"quixotic" not in left and b"Quixotic" not in left


@pytest.mark.asyncio
async def test_an_index_unreadable_at_open_does_not_stop_the_launch(tmp_path, monkeypatch):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)

    def unreadable(self):
        raise search_index.apsw.CorruptError("damaged")
    monkeypatch.setattr(SearchIndex, "unembedded", unreadable)
    async with app(tmp_path) as client:  # the model in place, the index unreadable: the app runs
        assert (await client.get(f"/api/projects/{project}/index")).status_code == 200
        runs = [r for r in (await client.get("/api/activity")).json()["runs"] if r["workflow"] == "index"]
        assert len(runs) == 2  # the first launch's, and one this launch recorded: its papers taken as pending


@pytest.mark.asyncio
async def test_a_file_found_damaged_while_the_app_runs_is_rebuilt_with_its_embeddings(tmp_path):
    """Found damaged during an idle session (not at a launch): the file is replaced, its keyword rows
    rebuilt, and its papers get index runs that embed them again, without a restart."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES, CHINESE)
        before = await until_embedded(client, project)
        runs = len([r for r in (await client.get("/api/activity")).json()["runs"] if r["workflow"] == "index"])
        index, sent = client.state["index"], len(client.remote.indexing)

        def damaged(conn):
            raise search_index.apsw.CorruptError("database disk image is malformed")
        with pytest.raises(search_index.apsw.CorruptError):
            await asyncio.to_thread(index._read, damaged)
        assert index.damaged is True
        deadline = asyncio.get_running_loop().time() + 10
        while index.damaged or index.building:  # replaced and rebuilt in the writer
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        status = await until_embedded(client, project)
        assert status["passages"] == before["passages"] and index.damaged is False and status["state"] == "ready"
        assert len(client.remote.indexing) - sent == before["passages"]["embeddable"]  # embedded again
        after = [r for r in (await client.get("/api/activity")).json()["runs"] if r["workflow"] == "index"]
        assert len(after) == runs + 1 and after[0]["status"] == "succeeded"
        assert (await find(client, project, "最低工资"))["mode"] == "hybrid"


async def until_rebuilt(index, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while index.damaged or index.building:  # replaced and rebuilt in the writer
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_rebuilt_file_is_embedded_again_though_the_pass_after_its_rebuild_fails(tmp_path, monkeypatch):
    """The papers' runs are asked for once the file is ready, whatever the pass that follows the rebuild
    meets (here a failure, tried again later)."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        before = await until_embedded(client, project)
        index, real, failed = client.state["index"], SearchIndex._apply_queue, []

        def fails_once(self, stop=None):  # the first pass after the damage: the one that follows the rebuild
            if not failed:
                failed.append(True)
                raise search_index.apsw.IOError("disk I/O error")
            return real(self, stop)
        monkeypatch.setattr(SearchIndex, "_apply_queue", fails_once)

        def damaged(conn):
            raise search_index.apsw.CorruptError("database disk image is malformed")
        with pytest.raises(search_index.apsw.CorruptError):
            await asyncio.to_thread(index._read, damaged)
        await until_rebuilt(index)
        status = await until_embedded(client, project)
        assert failed and status["passages"] == before["passages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["_apply_queue", "_store", "_rebuild_project"])
async def test_a_change_that_finds_the_file_damaged_has_it_rebuilt_and_embedded_again(tmp_path, monkeypatch, where):
    """A pass, storing vectors or a project's rebuild that meets a damaged file is routed as a read is: the
    file is replaced and rebuilt, and its papers embedded again."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES, CHINESE)
        before = await until_embedded(client, project)
        index, real, met = client.state["index"], getattr(SearchIndex, where), []
        rebuilt, rebuild_all = [], SearchIndex._rebuild_all

        def damaged_once(self, *args):
            if not met:
                met.append(True)
                raise search_index.apsw.CorruptError("database disk image is malformed")
            return real(self, *args)

        def counted(self):
            rebuilt.append(True)
            return rebuild_all(self)
        monkeypatch.setattr(SearchIndex, where, damaged_once)
        monkeypatch.setattr(SearchIndex, "_rebuild_all", counted)
        if where == "_apply_queue":
            with pytest.raises(search_index.apsw.CorruptError):
                await asyncio.to_thread(index.apply)
        elif where == "_store":
            with pytest.raises(search_index.apsw.CorruptError):
                await asyncio.to_thread(index.store, project, [])
        else:
            response = await client.post(f"/api/projects/{project}/index/rebuild")
            ended = await run_finished(client, response.json()["run_id"])
            assert (ended["status"], ended["result"]) == ("failed", {"reason": "index_unavailable"})  # said, not "internal"
        assert met
        deadline = asyncio.get_running_loop().time() + 10
        while not rebuilt:  # the file is replaced and rebuilt, as when a read finds it damaged
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        await until_rebuilt(index)
        status = await until_embedded(client, project)
        assert status["passages"] == before["passages"] and status["state"] == "ready" and rebuilt == [True]


def _failing_rebuilds(monkeypatch, fail_project, failures, held=None):
    """Make whole rebuilds fail as they reach fail_project's rows (the projects before it committed: a
    partial file), for the first `failures` attempts; with held, the attempt after them waits for it. Returns
    the list of attempts made."""
    import threading
    attempts, real_all, real_add = [], SearchIndex._rebuild_all, SearchIndex._add

    def counted(self):
        attempts.append(True)
        if held is not None and len(attempts) == failures + 1:
            held.wait(20)
        return real_all(self)

    def failing(self, project, pid, reading):
        if self.building and project == fail_project and len(attempts) <= failures:
            raise search_index.apsw.IOError("disk I/O error")
        return real_add(self, project, pid, reading)
    monkeypatch.setattr(SearchIndex, "_rebuild_all", counted)
    monkeypatch.setattr(SearchIndex, "_add", failing)
    return attempts


async def _unfinished(path):
    conn = search_index.apsw.Connection(str(path))
    conn.execute("UPDATE index_meta SET value = 'building' WHERE key = 'state'")  # rebuilt at the next launch
    conn.close()


async def _until(check, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_whole_rebuild_that_fails_once_is_tried_again_and_its_partial_file_is_never_read(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (0.05, 0.05))
    async with app(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        await added(client, first, WAGES)
        await added(client, second, CHINESE)
        before = {p: await until_embedded(client, p) for p in (first, second)}
        path = client.state["index"].path
    await _unfinished(path)
    held = threading.Event()
    early, late = sorted((first, second))  # rebuilt in the order of their ids: the early one's rows commit first
    attempts = _failing_rebuilds(monkeypatch, late, failures=1, held=held)
    try:
        async with app(tmp_path) as client:
            index = client.state["index"]
            await _until(lambda: len(attempts) == 2)  # the first failed, after the early project's rows; the retry waits
            assert index.unusable and not path.exists()  # its partial file discarded
            status = (await client.get(f"/api/projects/{early}/index")).json()
            assert (status["state"], status["mode"], status["reason"]) == ("unavailable", "keyword_only", "index_unavailable")
            found = await find(client, early, "earnings" if early == first else "最低工资")
            assert (found["results"], found["index"], found["reason"]) == ([], "unavailable", "index_unavailable")
            held.set()
            await _until(lambda: not index.unusable and not index.building)
            for project in (first, second):
                status = await until_embedded(client, project)
                assert status["state"] == "ready" and status["passages"] == before[project]["passages"]
            assert (await find(client, second, "最低工资"))["results"]
    finally:
        held.set()


@pytest.mark.asyncio
async def test_a_whole_rebuild_that_keeps_failing_leaves_search_unavailable_never_the_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (0.05, 0.2))
    async with app(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        await added(client, first, WAGES)
        await added(client, second, CHINESE)
        await until_embedded(client, second)
        path = client.state["index"].path
    await _unfinished(path)
    attempts = _failing_rebuilds(monkeypatch, max(first, second), failures=10_000)  # after the other's rows
    async with app(tmp_path) as client:
        index = client.state["index"]
        await _until(lambda: len(attempts) >= 3)  # tried again, and again (each partial file discarded: see above)
        with pytest.raises(search_index.Unavailable):  # nothing is written to it meanwhile
            await asyncio.to_thread(index.store, first, [])
        for project in (first, second):
            status = await idle(client, project)  # the launch's index runs end; none loops on the file
            assert (status["state"], status["reason"], status["passages"]["indexed"]) == ("unavailable", "index_unavailable", 0)
            found = await find(client, project, "earnings")
            assert (found["results"], found["index"]) == ([], "unavailable")
        ended = [r for r in (await client.get("/api/activity")).json()["runs"]  # this launch's: the first's succeeded
                 if r["workflow"] == "index" and r["status"] != "succeeded"]
        assert ended and all((r["status"], (r["result"] or {}).get("reason")) == ("failed", "index_unavailable")
                             for r in ended)
        assert index.unusable and index.building


@pytest.mark.asyncio
async def test_a_deletion_while_a_rebuild_keeps_failing_leaves_no_deleted_text_at_rest(tmp_path, monkeypatch):
    """Finding 19: the failed rebuild's partial file (the early project's rows committed, the deleted
    paper's among them) is discarded, so the paper deleted while the retry waits rests in no index file or
    WAL, before the rebuild succeeds or after."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))  # the retry waits: nothing else rebuilds meanwhile
    english = "Pangolins audit cerulean xylophones"
    async with app(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        early, late = sorted((first, second))
        [doomed] = (await added(client, early, paper("Pangolin Ledger", english)))["materials"]
        await added(client, late, CHINESE)
        await until_embedded(client, late)
        path = client.state["index"].path
    await _unfinished(path)
    files = [path, Path(f"{path}-wal")]
    attempts = _failing_rebuilds(monkeypatch, late, failures=1)  # the first fails after the early project's rows
    async with app(tmp_path) as client:
        index = client.state["index"]
        await _until(lambda: len(attempts) == 1 and index.unusable and index._rebuild_retry is not None)
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        left = b"".join(f.read_bytes() for f in files if f.exists())
        assert english.encode() not in left and b"pangolins" not in left  # no partial file holds it
        index._damaged()  # the rebuild asked for again, now: it succeeds
        await _until(lambda: not index.unusable and not index.building)
        status = await until_embedded(client, late)
        assert status["state"] == "ready" and (await idle(client, early))["passages"]["indexed"] == 0
        left = b"".join(f.read_bytes() for f in files if f.exists())
        assert english.encode() not in left and b"pangolins" not in left and b"cerulean" not in left


@pytest.mark.asyncio
async def test_the_index_closes_with_its_database_and_no_rebuild_outlives_it(tmp_path, monkeypatch):
    """Finding 17: when the main database closes (a restore under way or abandoned, shutdown), its index
    closes first: its retry timers cancelled, its writer drained, and no rebuild runs against the closed
    database."""
    from backend.db import Database
    import backend.search as search
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (0.05, 0.05))
    attempts, real_add = [], SearchIndex._add

    def failing_add(self, project, pid, reading):
        if self.building:
            attempts.append(True)
            raise search_index.apsw.IOError("disk I/O error")
        return real_add(self, project, pid, reading)
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
    data = tmp_path / "data"
    await _unfinished(data / search_index.FILE)
    monkeypatch.setattr(SearchIndex, "_add", failing_add)
    db = await asyncio.to_thread(Database, data)
    state = {"data_dir": data}
    try:
        await search.open_index(state, db)
        index = state["index"]
        await _until(lambda: len(attempts) >= 2)  # failing, and tried again
        assert index._rebuild_retry is not None or index.unusable
    finally:
        await asyncio.to_thread(db.close)
    assert index.closed and index._shut
    assert index._rebuild_retry is None or index._rebuild_retry.finished.is_set()  # cancelled
    tried = len(attempts)
    await asyncio.sleep(0.3)
    assert len(attempts) == tried  # nothing tried again against the closed database
    await asyncio.to_thread(index.close)  # closing again changes nothing


@pytest.mark.asyncio
async def test_a_rebuild_is_said_building_from_the_moment_it_is_asked_for(tmp_path, monkeypatch):
    """Finding 21: an unfinished file found at a launch reads as being rebuilt before the writer starts the
    rebuild, never as ready."""
    import threading
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await until_embedded(client, project)
        path = client.state["index"].path
    await _unfinished(path)
    go, real = threading.Event(), SearchIndex._rebuild_all

    def held(self):
        go.wait(20)
        return real(self)
    monkeypatch.setattr(SearchIndex, "_rebuild_all", held)
    try:
        async with app(tmp_path) as client:
            status = (await client.get(f"/api/projects/{project}/index")).json()
            assert status["state"] == "building"
            go.set()
            await until_embedded(client, project)
    finally:
        go.set()


# Fix batch 4


@pytest.mark.asyncio
async def test_a_deletion_while_a_partial_file_exists_then_a_close_leaves_no_deleted_text(tmp_path):
    """Review 4, P1: the rebuild has committed a project's rows (the paper's among them) when the paper is
    deleted, and the app closes before the rebuild ends: the deletion waits for the rebuild's writer, and
    the close discards the unfinished file, so neither the file nor its WAL keeps the paper's text."""
    import threading
    from backend.db import ContentStore, Database, delete
    import backend.search as search
    english = "Okapis tabulate amber zeppelins"
    async with app(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        early, late = sorted((first, second))
        [doomed] = (await added(client, early, paper("Okapi Ledger", english)))["materials"]
        await added(client, late, CHINESE)
        await until_embedded(client, late)
    data = tmp_path / "data"
    path = data / search_index.FILE
    files = [path, Path(f"{path}-wal")]
    await _unfinished(path)
    reached, go, real_add = threading.Event(), threading.Event(), SearchIndex._add

    def held(self, project, pid, reading):  # the late project's first row: the early project's rows committed
        if self.building and project == late and not go.is_set():
            reached.set()
            go.wait(20)
        return real_add(self, project, pid, reading)
    db = await asyncio.to_thread(Database, data)
    state = {"data_dir": data}
    try:
        SearchIndex._add = held
        await search.open_index(state, db)
        assert await asyncio.to_thread(reached.wait, 10)
        assert english.encode() in b"".join(f.read_bytes() for f in files if f.exists())  # the partial file has it
        deleting = asyncio.ensure_future(asyncio.to_thread(delete, db, ContentStore(db), "material", doomed["id"]))
        await asyncio.sleep(0.2)
        assert not deleting.done()  # its cleanup waits for the rebuild's writer
        closing = asyncio.ensure_future(asyncio.to_thread(db.close))
        await asyncio.sleep(0.2)
        go.set()
        await asyncio.wait_for(asyncio.gather(deleting, closing), 20)
    finally:
        SearchIndex._add = real_add
        go.set()
        await asyncio.to_thread(db.close)
    left = b"".join(f.read_bytes() for f in files if f.exists())
    assert english.encode() not in left and b"okapis" not in left and b"zeppelins" not in left


@pytest.mark.asyncio
async def test_a_rebuild_already_submitted_when_the_index_closes_makes_and_unlinks_nothing(tmp_path, monkeypatch):
    """Finding 22: a whole rebuild waiting its turn in the writer as close() runs neither unlinks the file nor
    makes a new one; the close discards the unfinished file."""
    import threading
    from backend.db import Database
    db = await asyncio.to_thread(Database, tmp_path / "data")
    index = SearchIndex(tmp_path / "data", db, {"model": "m"})
    made, real_create = [], SearchIndex._create

    def create(self):
        if self.closed:
            made.append(True)
        return real_create(self)
    monkeypatch.setattr(SearchIndex, "_create", create)
    try:
        await asyncio.to_thread(index.open)
        await asyncio.to_thread(index.apply)
        go = threading.Event()
        index._writer.submit(go.wait, 20)  # the writer busy
        index._rebuild_soon()  # waits its turn behind it
        closing = asyncio.ensure_future(asyncio.to_thread(index.close))
        await asyncio.sleep(0.2)
        go.set()
        await asyncio.wait_for(closing, 10)
    finally:
        await asyncio.to_thread(db.close)
    assert made == [] and not index.path.exists()


@pytest.mark.asyncio
async def test_a_rebuild_that_succeeds_disarms_a_retry_still_to_come(tmp_path, monkeypatch):
    """A failed whole rebuild arms a retry; one that then succeeds by another path disarms it, so no
    rebuild of a healthy file follows."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))  # the retry never comes due in the test
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await until_embedded(client, project)
        path = client.state["index"].path
    await _unfinished(path)
    attempts, failing, real_all, real_add = [], {"on": True}, SearchIndex._rebuild_all, SearchIndex._add

    def counted(self):
        attempts.append(True)
        return real_all(self)

    def failing_add(self, project_id, pid, reading):
        if self.building and failing["on"]:
            raise search_index.apsw.IOError("disk I/O error")
        return real_add(self, project_id, pid, reading)
    monkeypatch.setattr(SearchIndex, "_rebuild_all", counted)
    monkeypatch.setattr(SearchIndex, "_add", failing_add)
    async with app(tmp_path) as client:
        index = client.state["index"]
        await _until(lambda: len(attempts) == 1 and index.unusable and index._rebuild_retry is not None)
        armed = index._rebuild_retry
        failing["on"] = False
        index._damaged()  # another path asks for a rebuild, which succeeds
        await _until(lambda: not index.unusable and not index.building)
        assert index._rebuild_retry is None and armed.finished.is_set()  # the armed retry cancelled
        assert len(attempts) == 2  # the failed one and the success
        await until_embedded(client, project)


@pytest.mark.asyncio
async def test_removals_a_deletion_queued_but_no_pass_applied_reach_the_file_as_it_closes(tmp_path, monkeypatch):
    """A deletion whose cleanup did not reach the index (the index closing as it came) leaves its removals
    queued; the index's close applies them before the file closes, so its bytes and its WAL hold none of
    the paper."""
    english = "Quokkas annotate vermilion abacuses"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [doomed] = (await added(client, project, paper("Quokka Ledger", english)))["materials"]
        await added(client, project, WAGES)
        await until_embedded(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        from backend.db import deletion
        monkeypatch.setitem(deletion.CLEANUP, client.state["db"], lambda: None)  # its cleanup does not reach the index
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        assert english.encode() in b"".join(f.read_bytes() for f in files if f.exists())  # still there
    left = b"".join(f.read_bytes() for f in files if f.exists())
    assert english.encode() not in left and b"quokkas" not in left and b"vermilion" not in left
    assert files[0].exists()  # applied, not discarded: its embeddings stay for the next launch


@pytest.mark.asyncio
async def test_a_failed_rebuilds_discard_goes_on_whatever_closing_its_connections_meets(tmp_path, monkeypatch):
    """The internal review of 68c4cdd, P2: closing the partial file's connections may fail after an I/O
    error; the file is unlinked and the retry armed all the same."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))
    async with app(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        await added(client, first, WAGES)
        await added(client, second, CHINESE)
        await until_embedded(client, second)
        path = client.state["index"].path
    await _unfinished(path)
    attempts = _failing_rebuilds(monkeypatch, max(first, second), failures=1)
    real = SearchIndex._close_readers

    def failing(self):
        if self.unusable:
            raise search_index.apsw.IOError("disk I/O error")
        return real(self)
    monkeypatch.setattr(SearchIndex, "_close_readers", failing)
    async with app(tmp_path) as client:
        index = client.state["index"]
        await _until(lambda: len(attempts) == 1 and index.unusable)
        await _until(lambda: index._rebuild_retry is not None)  # armed, the discard's failure notwithstanding
        assert not path.exists() and not Path(f"{path}-wal").exists()
        monkeypatch.setattr(SearchIndex, "_close_readers", real)


@pytest.mark.asyncio
async def test_reconciling_a_project_stops_and_rolls_back_when_its_run_is_cancelled(tmp_path, monkeypatch):
    """The internal review of 68c4cdd, P3: the check against the main database asks stop() every
    STOP_EVERY rows; a true answer rolls it back, the drift left to the next run."""
    from backend.db import deletion
    monkeypatch.setattr(search_index, "STOP_EVERY", 1)
    async with app(tmp_path) as client:
        project = await project_of(client)
        [gone] = (await added(client, project, WAGES))["materials"]
        await added(client, project, paper("Kept Paper", "A paragraph that stays."))
        await until_embedded(client, project)
        index = client.state["index"]
        monkeypatch.setitem(deletion.CLEANUP, client.state["db"], lambda: None)  # its rows left behind: drift
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)
        assert (await client.delete(f"/api/materials/{gone['id']}")).status_code == 200
        before = len(await index_rows(client, project))
        import threading
        writer = threading.current_thread

        def stopped():  # asked in the index's writer only: by then the Cancel has come
            return writer().name.startswith("scholia-index-writer")
        assert await asyncio.to_thread(index.reconcile, project, stopped) == (0, 0)
        assert len(await index_rows(client, project)) == before  # rolled back
        assert await asyncio.to_thread(index.reconcile, project, lambda: False) == (0, 4)
        assert {r[1] for r in await index_rows(client, project)} == {m for m in (await idle(client, project))["materials"]}


@pytest.mark.asyncio
async def test_a_whole_rebuild_takes_the_queue_rows_it_read_so_no_later_launch_asks_a_run_for_them(tmp_path,
                                                                                                    monkeypatch):
    """Internal review of 68c4cdd, finding 5: the queue's rows at or below a whole rebuild's last sequence
    (what it read) leave the queue as it finishes, so later launches ask no index run for their project."""
    real_apply, real_reconcile = SearchIndex._apply, SearchIndex._reconcile
    async with app(tmp_path) as client:
        project = await project_of(client)
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)  # the additions stay queued
        monkeypatch.setattr(SearchIndex, "_reconcile", lambda self, project_id, stop=None: (0, 0))
        await added(client, project, WAGES)
        await idle(client, project)
        path = client.state["index"].path
    monkeypatch.setattr(SearchIndex, "_apply", real_apply)
    monkeypatch.setattr(SearchIndex, "_reconcile", real_reconcile)
    import apsw
    conn = apsw.Connection(str(path))
    conn.execute("UPDATE index_meta SET value = 'building' WHERE key = 'state'")  # unfinished: rebuilt at launch
    conn.close()
    async with app(tmp_path) as client:
        await until_rebuilt(client.state["index"])
        await until_embedded(client, project)
        [(queued,)] = await rows(client, "SELECT count(*) FROM index_queue")
        assert queued == 0
        before = await runs_of(client, "index", project)
    async with app(tmp_path) as client:
        await idle(client, project)
        assert await runs_of(client, "index", project) == before


@pytest.mark.asyncio
async def test_a_close_discarding_an_unfinished_rebuilds_file_goes_on_whatever_closing_its_readers_meets(tmp_path,
                                                                                                          monkeypatch):
    """The internal review of b682b9b, P3: the close's discard of a file a whole rebuild has not finished tries
    every step (_discard): a failure closing its readers leaves it removed all the same, and the close says it
    failed (fail closed)."""
    import threading
    from backend.db import Database
    db = await asyncio.to_thread(Database, tmp_path / "data")
    index = SearchIndex(tmp_path / "data", db, {"model": "m"})
    real, failed = SearchIndex._close_readers, []

    def failing(self):
        if self.closed and not failed:
            failed.append(True)
            raise search_index.apsw.IOError("disk I/O error")
        return real(self)
    try:
        await asyncio.to_thread(index.open)
        await asyncio.to_thread(index.apply)
        go = threading.Event()
        index._writer.submit(go.wait, 20)  # the writer busy
        index._rebuild_soon()  # asked for: the file is unfinished from now on
        monkeypatch.setattr(SearchIndex, "_close_readers", failing)
        closing = asyncio.ensure_future(asyncio.to_thread(index.close))
        await asyncio.sleep(0.2)
        go.set()
        with pytest.raises(search_index.apsw.IOError):
            await asyncio.wait_for(closing, 10)
    finally:
        monkeypatch.setattr(SearchIndex, "_close_readers", real)
        await asyncio.to_thread(db.close)
    assert failed and not index.path.exists() and not Path(f"{index.path}-wal").exists()


# Fix batch 5


QUIXOTIC = ("Quixotic Ledger.md", b"# Plain Heading\n\n## Part\n\nPlain text with nothing special.\n")


async def _last_project(client, monkeypatch):
    """A project whose id sorts after every other's (General's is random): the last a whole rebuild fills."""
    import backend.app
    with monkeypatch.context() as patch:
        patch.setattr(backend.app, "new_id", lambda: "ffffffff-ffff-4fff-bfff-ffffffffffff")
        project = await project_of(client)
    assert await rows(client, "SELECT max(id) FROM projects") == [(project,)]
    return project


async def _sharing(client, project):
    """Two papers reading one file: the first added, whose title the shared passages carry in the index, and
    the other, kept."""
    [first] = (await added(client, project, QUIXOTIC))["materials"]
    [second] = (await added(client, project, paper("Zephyr Accounts", "Other text.")))["materials"]
    await idle(client, project)
    await added(client, project, ("zephyr.md", QUIXOTIC[1]), material_id=second["id"])  # the same bytes
    await idle(client, project)
    return first, second


def _deleted_title_in(files):
    left = b"".join(f.read_bytes() for f in files if f.exists())
    return b"Quixotic" in left or b"quixotic" in left


@pytest.mark.asyncio
async def test_a_shared_readings_deletion_during_a_rebuilds_last_project_leaves_no_deleted_title_at_close(tmp_path, monkeypatch):
    """Recheck 5, finding 31: one of two papers reading one file is deleted while a whole rebuild fills the
    last project (its rows read with the deleted paper's title), and the app closes before that project is
    done. The rebuild ends ready, the queue's additions that retitle the passages unapplied; the close
    removes their rows, so neither the file nor its WAL keeps the deleted title, and the next launch adds
    them back with the kept paper's."""
    import threading
    from backend.db import ContentStore, Database, delete
    import backend.search as search
    async with app(tmp_path, install=False) as client:
        project = await _last_project(client, monkeypatch)
        doomed, kept = await _sharing(client, project)
    data = tmp_path / "data"
    path = data / search_index.FILE
    files = [path, Path(f"{path}-wal")]
    await _unfinished(path)
    reached, go, real_add = threading.Event(), threading.Event(), SearchIndex._add

    def held(self, project_id, pid, reading):  # the last project's first row: its readings read already
        if self.building and project_id == project and not go.is_set():
            reached.set()
            go.wait(20)
        return real_add(self, project_id, pid, reading)
    db = await asyncio.to_thread(Database, data)
    state = {"data_dir": data}
    try:
        SearchIndex._add = held
        await search.open_index(state, db)
        index = state["index"]
        assert await asyncio.to_thread(reached.wait, 10)
        deleting = asyncio.ensure_future(asyncio.to_thread(delete, db, ContentStore(db), "material", doomed["id"]))
        deadline = asyncio.get_running_loop().time() + 10
        while not await asyncio.to_thread(db.read, lambda conn: conn.execute(
                "SELECT count(*) FROM index_queue WHERE op = 'add'").fetchone()[0]):  # until the deletion commits
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        closing = asyncio.ensure_future(asyncio.to_thread(db.close))
        await _until(lambda: index.closed)
        go.set()
        await asyncio.wait_for(asyncio.gather(deleting, closing), 20)
    finally:
        SearchIndex._add = real_add
        go.set()
        await asyncio.to_thread(db.close)
    assert path.exists()  # the rebuild ended ready: kept, not discarded
    assert not _deleted_title_in(files)
    async with app(tmp_path, install=False) as client:
        status = await idle(client, project)
        assert list(status["materials"]) == [kept["id"]] and status["passages"]["indexed"]
        texts = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT text FROM fts_passages").fetchall())
        assert texts and all(text.startswith("Zephyr Accounts") for (text,) in texts)


@pytest.mark.asyncio
async def test_a_shared_readings_deletion_no_pass_applied_leaves_no_deleted_title_or_vector_at_close(tmp_path,
                                                                                                    monkeypatch):
    """Recheck 5, finding 31, by the cleanup path: the deletion's cleanup does not reach the index, so the
    additions that retitle the shared passages stay queued; the close removes those passages' rows, their
    vectors made from the deleted title's text with them, and the next launch embeds them again."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        doomed, kept = await _sharing(client, project)
        await until_embedded(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        shared = await asyncio.to_thread(index._read, lambda conn: [rowid for (rowid,) in conn.execute(
            "SELECT id FROM index_rows WHERE project_id = ? AND material_id = ? AND embedded = 1", (project, doomed["id"]))])
        assert shared
        from backend.db import deletion
        monkeypatch.setitem(deletion.CLEANUP, client.state["db"], lambda: None)  # its cleanup does not reach the index
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        assert _deleted_title_in(files)  # still there
    assert files[0].exists() and not _deleted_title_in(files)
    conn, loaded = search_index.connect(files[0])
    try:
        assert loaded and conn.execute("SELECT count(*) FROM vec_passages WHERE rowid IN (SELECT value FROM json_each(?))",
                                       (json.dumps(shared),)).fetchone()[0] == 0
    finally:
        conn.close()
    async with app(tmp_path) as client:
        status = await until_embedded(client, project)
        assert list(status["materials"]) == [kept["id"]]


@pytest.mark.asyncio
async def test_a_retry_the_opening_checkpoint_arms_cannot_take_a_launchs_queued_work(tmp_path, monkeypatch):
    """Recheck 5, finding 32: the opening's WAL truncation fails and arms a retry, whose pass applies the
    queued additions and prunes the queue; run between the launch's two reads of what waits, it still leaves
    the additions their index run."""
    real_apply, real_reconcile = SearchIndex._apply, SearchIndex._reconcile
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)  # the additions stay queued
        monkeypatch.setattr(SearchIndex, "_reconcile", lambda self, project_id, stop=None: (0, 0))
        await added(client, project, WAGES)
        await idle(client, project)
        assert await index_rows(client, project) == []
    monkeypatch.setattr(SearchIndex, "_apply", real_apply)
    monkeypatch.setattr(SearchIndex, "_reconcile", real_reconcile)
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))  # the armed retry runs when the test says
    real_connect, real_unembedded, failing, fired = SearchIndex._connect, SearchIndex.unembedded, [], []

    def connect(self):
        conn, loaded = real_connect(self)
        if failing:
            return conn, loaded
        failing.append(FailingCheckpoints(conn, "BusyError", failures=1))  # the writer's, opened first
        return failing[0], loaded

    def then_the_retry(self):  # the armed retry's pass, run to its end right after this read
        found = real_unembedded(self)
        if not fired:
            fired.append(self._retry)
            self._retry.cancel()
            self._retry.function()
            self._writer.submit(lambda: None).result()
        return found
    monkeypatch.setattr(SearchIndex, "_connect", connect)
    monkeypatch.setattr(SearchIndex, "unembedded", then_the_retry)
    async with app(tmp_path) as client:
        assert fired and fired[0] is not None and len(failing[0].calls) >= 2  # armed by the failure; its pass truncated
        await until_embedded(client, project)
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]


@pytest.mark.asyncio
async def test_a_deletions_cleanup_that_fails_once_asks_for_the_runs_once_its_retry_applies_it(tmp_path, monkeypatch):
    """Connector thread 4237098115: one of two papers reading one file is deleted and its cleanup's pass fails,
    so the check after the deletion finds every row embedded and asks for no run. The retry's pass retitles the
    shared passages (their embeddings dropped) and asks for the project's run, which embeds them again."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))  # the retry runs when the test says
    async with app(tmp_path) as client:
        project = await project_of(client)
        doomed, kept = await _sharing(client, project)
        await until_embedded(client, project)
        index, real, failed = client.state["index"], SearchIndex._apply_queue, []

        def failing_once(self, stop=None):
            if not failed:
                failed.append(True)
                raise search_index.apsw.IOError("disk I/O error")
            return real(self, stop)
        monkeypatch.setattr(SearchIndex, "_apply_queue", failing_once)
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        assert failed and index._retry is not None
        before = await runs_of(client, "index", project)
        index._retry.cancel()
        index._retry.function()
        await asyncio.to_thread(lambda: index._writer.submit(lambda: None).result())  # its pass done
        status = await until_embedded(client, project)
        assert list(status["materials"]) == [kept["id"]]
        assert len(await runs_of(client, "index", project)) == len(before) + 1


@pytest.mark.asyncio
async def test_queue_rows_a_failed_prune_left_leave_at_the_next_pass(tmp_path, monkeypatch):
    """Connector thread 4274410602: the queue's applied rows could not be deleted (a transient failure in the
    main database after the index recorded them applied); the next pass deletes them, though it has nothing
    of its own to apply, so no launch asks an index run for them again."""
    import threading
    from backend.db import Database
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await until_embedded(client, project)
        index, real, failed, failing = client.state["index"], Database.write, [], {"on": True}

        def failing_prunes(self, fn, *args, **kwargs):  # the index writer's writes to the main database: its prunes
            if failing["on"] and threading.current_thread().name.startswith("scholia-index-writer"):
                failed.append(True)
                raise OSError("disk I/O error")
            return real(self, fn, *args, **kwargs)
        db = client.state["db"]
        await asyncio.to_thread(db.write, lambda conn: conn.execute(
            "INSERT INTO index_queue (target, target_id, project_id, op) VALUES ('passage', 'gone', ?, 'remove')", (project,)))
        monkeypatch.setattr(Database, "write", failing_prunes)
        assert await asyncio.to_thread(index.apply) == 1
        assert failed and await rows(client, "SELECT count(*) FROM index_queue") == [(1,)]  # applied, left queued
        failing["on"] = False
        assert await asyncio.to_thread(index.apply) == 0  # nothing of its own to apply
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]


@pytest.mark.asyncio
async def test_a_close_whose_wal_truncation_stays_owed_fails_and_the_next_open_truncates_it_before_any_read(
        tmp_path, monkeypatch):
    """The internal review of fix batch 5, P3, failing closed (Harold, 2026-10-15): a deletion's removals applied
    but the WAL's truncation failing, at the pass and again as the index closes: old WAL frames may hold the
    paper's text, so the close fails (close_failed), and the next open truncates the WAL before anything reads."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))
    english = "Wombats catalogue saffron theodolites"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [doomed] = (await added(client, project, paper("Wombat Ledger", english)))["materials"]
        await added(client, project, WAGES)
        await until_embedded(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        index._conn = failing = FailingCheckpoints(index._conn, "IOError", failures=100)
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        assert index._truncate and failing.calls  # applied, its truncation owed
    from backend.db import close_failed
    assert len(failing.calls) >= 2 and close_failed()  # tried again at the close, which failed
    _restarted()
    async with app(tmp_path) as client:
        assert not any(b"wombat" in f.read_bytes().lower() for f in files if f.exists())  # truncated at open
        status = await until_embedded(client, project)
        assert status["passages"]["indexed"] == 4  # the kept paper's
        assert await asyncio.to_thread(client.state["index"].keyword, project, "wombats", 50) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("papers", ["own", "shared"])
async def test_a_deletion_admitted_as_the_app_shuts_down_leaves_nothing_of_it_in_the_closed_index(tmp_path, monkeypatch,
                                                                                                   papers):
    """Recheck 6, finding 35: a deletion admitted by the main database, its transaction still to commit, as the
    app shuts down. Shutdown refuses later writes and lets those admitted commit before the index closes, so
    the close finds the deletion's queue rows and removes their rows: neither the file nor its WAL keeps the
    deleted paper's title (own: nor its text and vector; shared: a file another paper reads, retitled), and
    the next launch shows the kept paper's rows."""
    import threading
    import time
    from backend.db import delete
    english = "Quokkas annotate vermilion abacuses"
    async with app(tmp_path) as client:
        project = await project_of(client)
        if papers == "shared":
            doomed, kept = await _sharing(client, project)
        else:
            [doomed] = (await added(client, project, paper("Quixotic Ledger", english)))["materials"]
            [kept] = (await added(client, project, WAGES))["materials"]
        await until_embedded(client, project)
        await background_idle(client)  # the lookups too: no run's write waits behind the deletion's
        db, index = client.state["db"], client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        embedding = vector(f"Quixotic Ledger\nFindings\n{english}")
        first = next(i for i, value in enumerate(embedding) if value)
        secrets = [b"Quixotic", b"quixotic"] + ([english.encode(), b"quokkas", b"vermilion",
                   search_index.sqlite_vec.serialize_float32(embedding)[4 * first:4 * first + 64]]
                   if papers == "own" else [])
        held = b"".join(f.read_bytes() for f in files if f.exists())
        assert all(s in held for s in secrets[:1] + secrets[2:3] + secrets[-1:])  # it was there
        entered, go, real_write = threading.Event(), threading.Event(), db.write

        def write(fn):  # the deletion's transaction waits, admitted, before its commit
            if "delete" in fn.__qualname__ and not entered.is_set():
                def waiting(conn):
                    result = fn(conn)
                    entered.set()
                    go.wait(20)
                    return result
                return real_write(waiting)
            return real_write(fn)
        monkeypatch.setattr(db, "write", write)
        deleting = asyncio.ensure_future(asyncio.to_thread(delete, db, client.state["content"], "material", doomed["id"]))
        assert await asyncio.to_thread(entered.wait, 10)

        def release():  # once the close refuses writes, or (were they not refused first) has closed the index
            deadline = time.monotonic() + 20
            while not (getattr(db, "_closing", False) or (index._shut and index._conn is None)) \
                    and time.monotonic() < deadline:
                time.sleep(0.002)
            go.set()
        threading.Thread(target=release, daemon=True).start()
    await asyncio.wait_for(deleting, 20)  # committed, as the app closed
    left = b"".join(f.read_bytes() for f in files if f.exists())
    assert [s for s in secrets if s in left] == []
    async with app(tmp_path) as client:
        status = await until_embedded(client, project)
        assert list(status["materials"]) == [kept["id"]]
        assert {material for _, material, _, _ in await index_rows(client, project)} == {kept["id"]}
        if papers == "shared":
            texts = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
                "SELECT text FROM fts_passages WHERE project_id = ?", (project,)).fetchall())
            assert texts and all(text.startswith("Zephyr Accounts") for (text,) in texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", ["database", "index"])
async def test_a_close_called_while_another_drains_the_index_returns_only_once_that_close_has_finished(tmp_path,
                                                                                                         monkeypatch,
                                                                                                         closing):
    """PR 29 review 1, finding 2: a second close, of the main database or of the index itself, called while the
    database's first close is held inside the index's drain (its queued removals still to apply), returns only
    once that has finished: the index's removals applied and the file closed (and, for the database's, then the
    main database closed). Neither the file nor its WAL keeps the deleted paper's text when it returns."""
    import threading
    import time
    from backend.db import deletion
    english = "Quokkas annotate vermilion abacuses"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [doomed] = (await added(client, project, paper("Quokka Ledger", english)))["materials"]
        await added(client, project, WAGES)
        await until_embedded(client, project)
        await background_idle(client)
        db, index = client.state["db"], client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        monkeypatch.setitem(deletion.CLEANUP, db, lambda: None)  # its removals stay queued for the close
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        reached, go, real = threading.Event(), threading.Event(), SearchIndex._remove_queued

        def held(self):  # the first close, inside the index's drain
            reached.set()
            go.wait(20)
            return real(self)
        monkeypatch.setattr(SearchIndex, "_remove_queued", held)
        first = threading.Thread(target=db.close)
        first.start()
        assert await asyncio.to_thread(reached.wait, 10)
        seen = []

        def second():
            (db.close if closing == "database" else index.close)()
            left = b"".join(f.read_bytes() for f in files if f.exists())
            seen.append((index._conn is None, closing == "index" or db._writer_ident is None,
                         english.encode() in left or b"quokkas" in left))
        later = threading.Thread(target=second)
        later.start()
        try:
            deadline = time.monotonic() + 1  # the second close's chance to return early
            while later.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        finally:
            go.set()
        await asyncio.to_thread(later.join, 20)
        await asyncio.to_thread(first.join, 20)
        assert not first.is_alive() and not later.is_alive()
        assert seen == [(True, True, False)]  # returned after the index closed, its removals applied, then the database


@pytest.mark.asyncio
async def test_the_index_refuses_a_close_from_its_own_writer_and_closes_from_elsewhere(tmp_path):
    """PR 29 recheck 2, finding 4: a close called in the index's own writer (by a job) would wait for that job, so it
    is refused; the index stays open and closes from any other thread."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(asyncio.to_thread(index._writer.submit(index.close).result), 10)
        assert not index.closed and not index._done.is_set()
        await asyncio.wait_for(asyncio.to_thread(index.close), 10)
        assert index.closed and index._conn is None and index._done.is_set()


@pytest.mark.asyncio
async def test_a_close_waits_for_a_read_in_progress_and_keeps_no_connection_open(tmp_path):
    """PR 29 recheck 2, finding 3: a close while a search reads refuses later reads, waits for that read to end, and
    returns with no read connection left open, the one that read included."""
    import threading
    import time
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        reading, go, used = threading.Event(), threading.Event(), []

        def slow(conn):
            used.append(conn)
            reading.set()
            go.wait(20)
            return conn.execute("SELECT count(*) FROM index_rows").fetchone()
        reader = threading.Thread(target=index._read, args=(slow,))
        reader.start()
        assert await asyncio.to_thread(reading.wait, 10)
        closer = threading.Thread(target=index.close)
        closer.start()
        try:
            deadline = time.monotonic() + 1  # the close's chance to return early
            while closer.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            returned_early = not closer.is_alive()
            with pytest.raises(DatabaseClosedError):
                index._read(lambda conn: None)
        finally:
            go.set()
        await asyncio.to_thread(closer.join, 20)
        await asyncio.to_thread(reader.join, 20)
        assert not returned_early and not closer.is_alive() and index._readers.empty()
        with pytest.raises(search_index.apsw.ConnectionClosedError):
            used[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_an_index_close_that_fails_after_its_drain_is_reported_to_every_later_close(tmp_path, monkeypatch):
    """Internal review of 98116c4: a close that fails after its drain (stopping the writer, say) still closes the
    read connections and signals its end, and every later close, on any thread, raises rather than reading as
    done; the dead writer's thread id is forgotten, so no later thread is taken for it."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        await asyncio.to_thread(index._read, lambda conn: None)  # an idle read connection, to be closed
        real = index._writer.shutdown

        def shutdown(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("the writer could not be stopped")
        monkeypatch.setattr(index._writer, "shutdown", shutdown)
        with pytest.raises(OSError):
            await asyncio.to_thread(index.close)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await asyncio.to_thread(index.close)
        assert index._done.is_set() and index._writer_ident is None and index._readers.empty()


@pytest.mark.asyncio
async def test_a_read_connection_that_fails_to_close_closes_the_rest_and_fails_the_close(tmp_path, monkeypatch):
    """Recheck of 3fb92ba, N1, failing closed: an idle read connection whose close fails as the index closes: the
    other idle ones are closed all the same, and the failure is the close's, never only a log line: close_failed()
    then holds."""
    import threading
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        first, second, go = threading.Event(), threading.Event(), threading.Event()

        def holding(event):
            def fn(conn):
                event.set()
                go.wait(10)
            return fn
        readers = [threading.Thread(target=index._read, args=(holding(e),)) for e in (first, second)]
        for reader in readers:
            reader.start()
        assert await asyncio.to_thread(first.wait, 10) and await asyncio.to_thread(second.wait, 10)
        go.set()
        for reader in readers:
            await asyncio.to_thread(reader.join, 10)
        pooled = []
        while not index._readers.empty():
            pooled.append(index._readers.get_nowait())
        assert len(pooled) == 2

        class Failing:
            def __init__(self, conn):
                self.conn = conn

            def close(self):
                self.conn.close()
                raise search_index.apsw.IOError("disk I/O error")
        index._readers.put(Failing(pooled[0]))
        index._readers.put(pooled[1])
        with pytest.raises(search_index.apsw.IOError):
            await asyncio.to_thread(index.close)
        assert index._readers.empty()
        with pytest.raises(search_index.apsw.ConnectionClosedError):
            pooled[1].execute("SELECT 1")
        from backend.db import close_failed
        assert close_failed()



# Failing closed (Harold, 2026-10-15, as simplified: every open purges before any read; one failed close holds file
# replacement off for the rest of the process. PR 29 findings 43 to 46 and 49 to 51, 54)


def _restarted():
    """As a new process: the failed-close flag a test's earlier session set is gone (only a restart clears it)."""
    from backend.db import database
    database._close_failed.clear()


@pytest.mark.parametrize("left", ["queued", "ungranted"])
@pytest.mark.asyncio
async def test_what_a_failed_close_leaves_of_a_deletion_is_purged_at_the_next_open_before_any_read(tmp_path,
                                                                                                    monkeypatch, left):
    """A deletion whose removals the index's close could not apply: the close fails (close_failed), and the deleted
    paper's text stays in the file. The next open, before anything reads it, applies the queue (queued) or, the
    queue lost as after a crash, removes every row of a passage the main database no longer grants (ungranted), and
    truncates the WAL: the first read finds none of it, and the kept paper's rows are as they were."""
    from backend.db import close_failed, deletion
    english = "Quokkas annotate vermilion abacuses"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [doomed] = (await added(client, project, paper("Quokka Ledger", english)))["materials"]
        [kept] = (await added(client, project, WAGES))["materials"]
        await until_embedded(client, project)
        await background_idle(client)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        monkeypatch.setitem(deletion.CLEANUP, client.state["db"], lambda: None)  # its removals left to the close
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        if left == "ungranted":  # the queue rows gone too, as a crash between the commit and the pass may leave none
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute("DELETE FROM index_queue"))

        def failing(self):
            raise search_index.apsw.IOError("disk I/O error")
        monkeypatch.setattr(SearchIndex, "_remove_queued", failing)
    monkeypatch.undo()
    assert close_failed() and english.encode() in b"".join(f.read_bytes() for f in files if f.exists())
    _restarted()
    reads = []
    real_read = SearchIndex._read

    def read(self, fn):
        reads.append([f.read_bytes() for f in files if f.exists()])
        return real_read(self, fn)
    monkeypatch.setattr(SearchIndex, "_read", read)
    async with app(tmp_path) as client:
        status = await until_embedded(client, project)
        assert reads and not any(english.encode() in b or b"quokkas" in b for b in reads[0])  # gone before the first read
        assert list(status["materials"]) == [kept["id"]] and status["passages"]["embedded"] == 4
        assert client.remote.indexing == []  # nothing embedded again: the purge only removes


@pytest.mark.asyncio
async def test_the_purge_at_open_keeps_a_refused_row_of_a_granted_passage_and_removes_an_ungranted_one(tmp_path,
                                                                                                         monkeypatch):
    """The launch purge beside the helper's refusal marks (PR 31; embedded = -1): a refused row of a passage the main
    database still grants survives the purge, still refused (not asked for again); a refused row of a passage it no
    longer grants goes with the rest of that paper's rows, before any read."""
    from backend.db import deletion
    english = "Quokkas annotate vermilion abacuses"
    async with app(tmp_path) as client:
        project = await project_of(client)
        [doomed] = (await added(client, project, paper("Quokka Ledger", english)))["materials"]
        [kept] = (await added(client, project, WAGES))["materials"]
        await until_embedded(client, project)
        await background_idle(client)
        index = client.state["index"]

        def refuse_one_of_each():
            refused = {}
            with index._conn:
                for material in (kept["id"], doomed["id"]):
                    rowid, pid = index._conn.execute("SELECT id, passage_id FROM index_rows WHERE material_id = ?"
                                                     " ORDER BY id LIMIT 1", (material,)).fetchone()
                    index._conn.execute("DELETE FROM vec_passages WHERE rowid = ?", (rowid,))
                    index._conn.execute("UPDATE index_rows SET embedded = -1 WHERE id = ?", (rowid,))
                    refused[material] = pid
            return refused
        refused = await asyncio.to_thread(index._write, refuse_one_of_each)
        monkeypatch.setitem(deletion.CLEANUP, client.state["db"], lambda: None)  # left to the next open
        assert (await client.delete(f"/api/materials/{doomed['id']}")).status_code == 200
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute("DELETE FROM index_queue"))
    monkeypatch.undo()
    async with app(tmp_path) as client:
        left = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT passage_id, material_id, embedded FROM index_rows").fetchall())
        assert (refused[kept["id"]], kept["id"], -1) in left  # granted, refused: kept as it was
        assert not any(material == doomed["id"] for _, material, _ in left)  # ungranted: gone, its refused row too
        status = await idle(client, project)
        assert status["passages"]["refused"] == 1 and list(status["materials"]) == [kept["id"]]
        assert client.remote.indexing == []  # the refused passage is not asked for again


@pytest.mark.asyncio
async def test_a_discard_that_fails_as_the_index_closes_fails_the_close(tmp_path, monkeypatch):
    """Finding 44: the close's discard of a file a whole rebuild has not finished, its unlink failing, is the
    close's failure, never a logged success: the database's close raises and close_failed() holds; the next open
    purges (or rebuilds) the file left before anything reads it."""
    import threading
    from backend.db import Database, DatabaseCloseError, close_failed
    db = await asyncio.to_thread(Database, tmp_path / "data")
    index = SearchIndex(tmp_path / "data", db, {"model": "m"})
    db.closing.append(index.close)
    real_unlink, failed = Path.unlink, []

    def unlink(self, missing_ok=False):
        if self == index.path and not failed:
            failed.append(True)
            raise PermissionError("operation not permitted")
        return real_unlink(self, missing_ok=missing_ok)
    await asyncio.to_thread(index.open)
    await asyncio.to_thread(index.apply)
    go = threading.Event()
    index._writer.submit(go.wait, 20)  # the writer busy
    index._rebuild_soon()  # asked for: the file is unfinished from now on
    monkeypatch.setattr(Path, "unlink", unlink)
    closing = asyncio.ensure_future(asyncio.to_thread(db.close))
    await asyncio.sleep(0.2)
    go.set()
    with pytest.raises(DatabaseCloseError):
        await asyncio.wait_for(closing, 10)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert failed and index.path.exists() and close_failed()
    db = await asyncio.to_thread(Database, tmp_path / "data")
    again = SearchIndex(tmp_path / "data", db, {"model": "m"})
    try:
        await asyncio.to_thread(again.open)
    finally:
        await asyncio.to_thread(again.close)
        await asyncio.to_thread(db.close)


class _FailingClose:
    """A read connection whose close fails (after closing it)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *exc):
        return self.conn.__exit__(*exc)

    def close(self):
        self.conn.close()
        raise search_index.apsw.IOError("disk I/O error")


@pytest.mark.parametrize("generation", ["same", "replaced"])
@pytest.mark.asyncio
async def test_a_read_that_ends_as_the_index_closes_has_its_failed_close_fail_the_close(tmp_path, generation):
    """Findings 46 and 50: a read still running as the index closes, its connection's close failing. Same file: the
    read gives its connection to the close, whose close of it fails. Replaced (a rebuild replaced the file while the
    read ran, before the close began): the read closes its connection itself, and that close is still under way,
    outside any lock, as the index's close begins; it fails. Either way the failure is the close's, and
    close_failed() holds."""
    import threading
    from backend.db import close_failed
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        conn, _ = search_index.connect(index.path)
        closing_reached, closing_go = threading.Event(), threading.Event()

        class Failing(_FailingClose):
            def close(self):
                if generation == "replaced":  # the read's own close, under way as the index's close begins
                    closing_reached.set()
                    closing_go.wait(20)
                super().close()
        while not index._readers.empty():
            index._readers.get_nowait().close()
        index._readers.put(Failing(conn))
        reading, go = threading.Event(), threading.Event()

        def slow(conn):
            reading.set()
            if generation == "replaced":
                with index._lock:
                    index._generation += 1  # as a rebuild that replaced the file meanwhile
            else:
                go.wait(20)
        reader = threading.Thread(target=_ignoring(index._read), args=(slow,))
        reader.start()
        assert await asyncio.to_thread(reading.wait, 10)
        if generation == "replaced":
            assert await asyncio.to_thread(closing_reached.wait, 10)
        closing = asyncio.ensure_future(asyncio.to_thread(index.close))
        await asyncio.sleep(0.3)
        assert not closing.done()  # it waits for the read
        go.set()
        closing_go.set()
        with pytest.raises(search_index.apsw.IOError):
            await asyncio.wait_for(closing, 10)
        await asyncio.to_thread(reader.join, 10)
        with pytest.raises(search_index.apsw.ConnectionClosedError):
            conn.execute("SELECT 1")
        assert close_failed()


@pytest.mark.asyncio
async def test_an_idle_read_connection_a_rebuild_could_not_close_fails_the_indexs_close(tmp_path):
    """Review 6, P3-2: an idle read connection whose close fails as a rebuild replaces the file: the rebuild fails
    and is tried again, and the index's close later fails with that connection's failure (close_failed), never
    reporting success with it open."""
    from backend.db import close_failed
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        conn, _ = search_index.connect(index.path)
        while not index._readers.empty():
            index._readers.get_nowait().close()
        index._readers.put(_FailingClose(conn))
        with pytest.raises(search_index.apsw.IOError):
            await asyncio.to_thread(index._close_readers)  # as a rebuild's or a discard's call meets it
        with pytest.raises(search_index.apsw.IOError):
            await asyncio.to_thread(index.close)
        assert close_failed()


def _ignoring(fn):
    def call(*args):
        try:
            fn(*args)
        except Exception:
            pass
    return call

# The main database's side: the passages a project's current versions read now (M2 follow-up)

# readings and granted as S1-17 wrote them, driven from every current version of every project: the
# reference the cheaper queries must agree with, row for row.
S117_READS = ("FROM materials m JOIN material_versions w ON w.material_id = m.id AND w.is_current = 1"
              " JOIN extractions e ON {} JOIN passages p ON p.extraction_id = e.id WHERE m.project_id = ?")


def s117_readings(conn, project_id, references=True):
    from backend.db.deletion import _reads_now
    found = {}
    for pid, material, title, path, kind, text, page, ordinal, version in conn.execute(
            "SELECT p.id, m.id, m.title, p.section_path, p.kind, p.text, p.page, p.ordinal, w.id "
            + S117_READS.format(_reads_now("w", "e")) + ("" if references else " AND p.kind != 'reference'")
            + " ORDER BY m.created_at, m.id, p.ordinal", (project_id,)):
        found.setdefault(pid, (material, title, json.loads(path) if path else [], kind, text, page, ordinal, version))
    return found


def s117_granted(conn, project_id):
    from backend.db.deletion import _reads_now
    return {pid for (pid,) in conn.execute("SELECT p.id " + S117_READS.format(_reads_now("w", "e")), (project_id,))}


def library(conn):
    """Two projects of synthetic papers, with every way a passage is read or not: a file two papers of a
    project read (the first added gives it), the same bytes added as Markdown and as LaTeX (two readings
    of one file), a version from before media types (its file's type), a replaced version, a reading by
    an earlier extractor version, a paper with no current version, a reference passage and the same
    file in the other project, a file of a type no extractor reads (its extraction's own), and a passage
    with no section path. (project one, project two, every passage id)."""
    from backend.db.database import new_id
    from backend.extraction import LATEX, MARKDOWN, extractor_of
    passages = {}

    def file(name, media_type=MARKDOWN, *readers):
        sha = hashlib.sha256(name.encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO content_files (sha256, size, media_type) VALUES (?, 1, ?)", (sha, media_type))
        for kind, version in readers or [extractor_of(media_type)]:
            extraction = new_id()
            conn.execute("INSERT INTO extractions (id, file_sha256, extractor, extractor_version, status)"
                         " VALUES (?, ?, ?, ?, 'done')", (extraction, sha, kind, version))
            for ordinal, kind in enumerate(("title", "paragraph", "reference")):
                pid = new_id()
                conn.execute("INSERT INTO passages (id, extraction_id, ordinal, kind, text, section_path)"
                             " VALUES (?, ?, ?, ?, ?, ?)", (pid, extraction, ordinal, kind, f"{name} {kind}",
                                                            None if kind == "title" else '["Intro"]'))
                passages.setdefault("all", []).append(pid)
        return sha

    def material(project, when, *versions):  # versions: (sha, media type or None, current)
        mid = new_id()
        conn.execute("INSERT INTO materials (id, project_id, title, source, created_at) VALUES (?, ?, ?, 'upload', ?)",
                     (mid, project, f"Paper {when}", f"2026-10-15T00:00:{when:02d}.000Z"))
        for seq, (sha, media_type, current) in enumerate(versions):
            conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current, media_type)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (new_id(), mid, seq, sha, int(current), media_type))

    one, two = new_id(), new_id()
    for project in (one, two):
        conn.execute("INSERT INTO projects (id, name, kind) VALUES (?, 'P', 'research')", (project,))
    shared = file("shared")
    both = file("both", MARKDOWN, extractor_of(MARKDOWN), extractor_of(LATEX))
    old = file("old", MARKDOWN, ("markdown", "markdown-1"), extractor_of(MARKDOWN))
    material(one, 1, (file("replaced"), MARKDOWN, False), (shared, MARKDOWN, True))
    material(one, 2, (shared, None, True))  # the shared file, added later: the first gives its passages
    material(one, 3, (both, LATEX, True))
    material(one, 4, (both, MARKDOWN, True))
    material(one, 5, (old, MARKDOWN, True))
    material(one, 6, (file("withdrawn"), MARKDOWN, False))
    material(one, 9, (file("unknown", "application/x-unknown", ("plain", "plain-1")), None, True))
    material(two, 7, (shared, MARKDOWN, True))
    material(two, 8, (file("elsewhere"), MARKDOWN, True))
    return one, two, passages["all"]


def test_a_check_for_some_passages_gives_what_the_whole_projects_reading_gives_them(tmp_path):
    """The access check driven from the passage ids returns, for every subset, exactly what S1-17's
    whole-project query did: the same passages, the first added paper for one two papers read, and no
    reference passage without references; a database from before the indexes reads the same after them."""
    import random
    from backend.db.database import Database
    from backend.db.migrations import MIGRATIONS
    from backend.search_index import granted, readings
    data = tmp_path / "data"
    with Database(data, migrations=MIGRATIONS[:4]) as db:
        one, two, every = db.write(library)
        before = db.read(lambda conn: {(p, r): s117_readings(conn, p, r) for p in (one, two) for r in (True, False)})
    with Database(data) as db:
        names = {name for (name,) in db.read(lambda conn: conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'"))}
        assert {"materials_by_project", "material_versions_current_by_file", "extractions_by_file"} <= names
        rng = random.Random(5)
        for (project, references), reference in before.items():
            assert reference and db.read(lambda conn: s117_readings(conn, project, references)) == reference
            assert db.read(lambda conn: readings(conn, project, references=references)) == reference
            assert db.read(lambda conn: granted(conn, project)) == db.read(lambda conn: s117_granted(conn, project))
            subsets = [[pid] for pid in every] + [rng.sample(every, k) for k in (2, 5, 9) for _ in range(20)] + [[], every, every[:4] * 2]
            for ids in subsets:
                assert db.read(lambda conn: readings(conn, project, ids, references)) == {
                    pid: reading for pid, reading in reference.items() if pid in ids}


def test_a_check_for_a_few_passages_reads_only_their_rows_and_a_projects_reading_only_its_own(tmp_path):
    """The plans: a check for given passage ids searches each table by key from the passages up (no scan
    of passages, extractions, versions or materials, whatever the project's size); a whole project's
    reading and its granted ids search from its materials down, so neither reads other projects' rows."""
    from backend.db.database import Database
    from backend.search_index import granted, readings
    with Database(tmp_path / "data") as db:
        one, _, every = db.write(library)

        def plans(conn):
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                readings(conn, one, every[:3], references=False)
                readings(conn, one)
                granted(conn, one)
            finally:
                conn.set_trace_callback(None)
            return [[row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")] for sql in statements]
        some, whole, ids = db.read(plans)
    for plan in (some, whole, ids):
        scans = [step for step in plan if step.startswith("SCAN") and "json_each" not in step]
        assert scans == [], plan
    assert "sqlite_autoindex_passages_1" in some[0]  # the passages' primary key, first
    assert any("material_versions_current_by_file" in step for step in some)
    for plan in (whole, ids):
        assert "materials_by_project" in plan[0]
        assert any("extractions_by_file" in step for step in plan)


# PR 29 recheck 5 (cycle 5): every connection close sets the flag at once; no read during a whole rebuild


@pytest.mark.asyncio
async def test_no_read_of_the_old_file_while_an_open_s_whole_rebuild_is_under_way(tmp_path, monkeypatch):
    """Finding 1: an open that requires a whole rebuild (here an unfinished file) skips the purge; until that rebuild
    is done every read is refused (as an unavailable index), and search says the index is being built, so a passage
    the main database no longer grants is never found in the old file, which is still there when the rebuild waits
    to begin (held here at its start)."""
    import threading
    from backend.db import Database
    import backend.search as search
    english = "Quokkas annotate vermilion abacuses"
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        [doomed] = (await added(client, project, paper("Quokka Ledger", english)))["materials"]
        await added(client, project, WAGES)
        await idle(client, project)
    data = tmp_path / "data"
    await _unfinished(data / search_index.FILE)
    db = await asyncio.to_thread(Database, data)
    await asyncio.to_thread(db.write, lambda conn: conn.execute(  # ungranted, as a deletion the index never applied
        "UPDATE material_versions SET is_current = 0 WHERE material_id = ?", (doomed["id"],)))
    reached, go, real_rebuild = threading.Event(), threading.Event(), SearchIndex._rebuild_all

    def held(self):  # the rebuild about to begin: the old file, with the ungranted passage's rows, still there
        reached.set()
        go.wait(20)
        return real_rebuild(self)
    monkeypatch.setattr(SearchIndex, "_rebuild_all", held)
    state = {"data_dir": data, "db": db}
    try:
        await search.open_index(state, db)
        index = state["index"]
        assert await asyncio.to_thread(reached.wait, 10) and index.building
        old, _ = search_index.connect(index.path)
        try:  # the old file still holds it
            assert old.execute("SELECT count(*) FROM index_rows WHERE material_id = ?", (doomed["id"],)).fetchone()[0]
        finally:
            old.close()
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.keyword, project, "quokkas", 50)
        found = await search.search(state, project, "quokkas")
        assert (found["results"], found["index"]) == ([], "building")
    finally:
        go.set()
        await asyncio.to_thread(db.close)


class _FailingOnce:
    """A connection whose first close fails (without closing it); later closes close it."""

    def __init__(self, conn):
        self.conn, self.failed = conn, False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *exc):
        return self.conn.__exit__(*exc)

    def close(self):
        if not self.failed:
            self.failed = True
            raise search_index.apsw.IOError("disk I/O error")
        self.conn.close()


@pytest.mark.asyncio
async def test_a_rebuild_whose_close_of_the_file_fails_once_sets_the_flag_then_though_a_retry_succeeds(tmp_path):
    """Finding 2: a whole rebuild whose close of the old file's connection fails, then succeeds when the discard tries
    it again: the failure set close_failed() as it happened, and nothing later clears it."""
    from backend.db import Database, close_failed
    db = await asyncio.to_thread(Database, tmp_path / "data")
    index = SearchIndex(tmp_path / "data", db, {"model": "m"})
    try:
        await asyncio.to_thread(index.open)
        await asyncio.to_thread(index.apply)
        index._conn = failing = _FailingOnce(index._conn)
        await asyncio.wrap_future(index._rebuild_soon())  # it fails, is logged, and its discard closes the file
        assert failing.failed and index._conn is None  # the discard's second try closed it
        assert close_failed()  # set by the first failure, before any close of the index
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(index.close)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(db.close)
    assert close_failed()


@pytest.mark.parametrize("where", ["idle", "replaced"])
@pytest.mark.asyncio
async def test_a_read_connection_whose_close_fails_sets_the_flag_at_once(tmp_path, where):
    """Finding 4: a read connection whose close fails, idle as a rebuild or a discard closes them, or a read's own (a
    file replaced while it read), sets close_failed() at that moment, not only at the index's close."""
    from backend.db import close_failed
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        conn, _ = search_index.connect(index.path)
        while not index._readers.empty():
            index._readers.get_nowait().close()
        index._readers.put(_FailingClose(conn))
        if where == "idle":
            with pytest.raises(search_index.apsw.IOError):
                await asyncio.to_thread(index._close_readers)
        else:
            def replaced(conn):
                with index._lock:
                    index._generation += 1
            with pytest.raises(search_index.apsw.IOError):
                await asyncio.to_thread(index._read, replaced)
        assert close_failed() and not index.closed


@pytest.mark.asyncio
async def test_a_read_admitted_before_a_rebuild_never_reaches_the_file_the_rebuild_makes(tmp_path, monkeypatch):
    """PR 29 recheck 6: a read admitted, its connection not yet made, as damage found elsewhere starts a whole rebuild.
    The read takes its connection within its admission, and the rebuild waits for the reads admitted before it
    before it removes the file and makes the new one: the read reads the file it was admitted to (ready), never the
    new, unfinished one."""
    import threading
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        while not index._readers.empty():  # the read opens a connection of its own
            index._readers.get_nowait().close()
        connecting, go, adding, add_go = threading.Event(), threading.Event(), threading.Event(), threading.Event()
        reader_thread, real_connect, real_add = [], SearchIndex._connect, SearchIndex._add

        def connect(self):
            if threading.current_thread() in reader_thread:  # the read, admitted, about to make its connection
                connecting.set()
                go.wait(20)
            return real_connect(self)

        def add(self, project_id, pid, reading):
            if self.building:  # the rebuild, its new file made, filling it
                adding.set()
                add_go.wait(20)
            return real_add(self, project_id, pid, reading)
        monkeypatch.setattr(SearchIndex, "_connect", connect)
        monkeypatch.setattr(SearchIndex, "_add", add)
        seen = []

        def read():
            try:
                seen.append(index._read(lambda conn: conn.execute(
                    "SELECT value FROM index_meta WHERE key = 'state'").fetchone()))
            except Exception as error:
                seen.append(error)
        reader = threading.Thread(target=read)
        reader_thread.append(reader)
        reader.start()
        assert await asyncio.to_thread(connecting.wait, 10)
        damage = threading.Thread(target=index._damaged)  # damage found by another read or a pass
        damage.start()
        await asyncio.to_thread(adding.wait, 1)  # the rebuild's chance to make its file while the read waits
        go.set()
        await asyncio.to_thread(reader.join, 10)
        add_go.set()
        await asyncio.to_thread(damage.join, 10)
        assert seen == [("ready",)]
        await asyncio.wait_for(_until(lambda: not index.building), 20)


@pytest.mark.asyncio
async def test_a_read_after_damage_is_found_is_refused_at_once(tmp_path, monkeypatch):
    """PR 29 recheck 6, the _damaged() gap: once damage is found, no read is admitted, even before the rebuild it asks
    for has been queued."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        monkeypatch.setattr(SearchIndex, "_rebuild_soon", lambda self, then=None: None)  # not queued yet
        await asyncio.to_thread(index._damaged)
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.keyword, project, "wage", 5)
        index.building = index.damaged = False  # as no rebuild ran, for the app's close


@pytest.mark.parametrize("replacing", ["rebuild", "discard"])
@pytest.mark.asyncio
async def test_a_rebuild_or_discard_waits_for_the_reads_admitted_before_it(tmp_path, replacing):
    """PR 29 recheck 6, review 8 P3-1: a read still running as a whole rebuild (damage found) or a discard of the
    file begins: the file is neither removed nor replaced, nor its connection closed, until that read has ended."""
    import os
    import threading
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        reading, go, seen = threading.Event(), threading.Event(), []

        def slow(conn):
            reading.set()
            go.wait(20)
            return conn.execute("SELECT value FROM index_meta WHERE key = 'state'").fetchone()
        reader = threading.Thread(target=lambda: seen.append(index._read(slow)))
        reader.start()
        assert await asyncio.to_thread(reading.wait, 10)
        inode, conn = os.stat(index.path).st_ino, index._conn
        if replacing == "rebuild":
            await asyncio.to_thread(index._damaged)
        else:
            index._writer.submit(index._discard)
        await asyncio.sleep(0.5)  # its chance to remove or replace the file under the read
        assert os.stat(index.path).st_ino == inode and index._conn is conn  # waiting
        go.set()
        await asyncio.to_thread(reader.join, 10)
        assert seen == [("ready",)]
        if replacing == "rebuild":
            await asyncio.wait_for(_until(lambda: not index.building), 20)
            assert os.stat(index.path).st_ino != inode  # then replaced
        else:
            await asyncio.wait_for(_until(lambda: not index.path.exists()), 20)  # then removed


@pytest.mark.parametrize("error", ["CorruptError", "NotADBError", "NoFTS5Error"])
@pytest.mark.asyncio
async def test_a_read_whose_connection_cannot_open_on_a_damaged_file_has_it_rebuilt(tmp_path, monkeypatch, error):
    """PR 29 recheck 7: with no idle connection, the read's own connection meets a damaged file (as SQLite says it,
    or as the tokenizer's registration fails on a corrupt header): the read is refused as unavailable, and the
    file is replaced and rebuilt, as for a read that finds it damaged."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        while not index._readers.empty():
            index._readers.get_nowait().close()
        real, failed = SearchIndex._connect, []

        def connect(self):
            if not failed and self._writer_ident != threading.get_ident():  # a read's, not the rebuild's
                failed.append(True)
                raise getattr(search_index.apsw, error)("damaged")
            return real(self)
        monkeypatch.setattr(SearchIndex, "_connect", connect)
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.keyword, project, "wage", 5)
        monkeypatch.undo()
        assert failed and index.damaged
        await asyncio.wait_for(_until(lambda: not index.building), 20)
        assert await asyncio.to_thread(index.keyword, project, "wage", 5)  # rebuilt, read again



# PR 29 recheck 9 (findings 64, 65; Harold's simplification): a read's APSW error is damage unless the file is busy
# or locked; no connection to the file outside a read's admission; a file this process rebuilt, damaged again before
# any read of it succeeded, makes the index unusable (its retry later), not a loop of rebuilds


@pytest.mark.asyncio
async def test_a_healthy_index_another_connection_holds_is_busy_opening_not_damaged(tmp_path):
    """A lock is not damage: opening a read's connection to a healthy file another connection holds (exclusive
    locking) fails BusyError after the busy timeout, where the tokenizer's registration said NoFTS5Error."""
    from backend.db import Database
    db = await asyncio.to_thread(Database, tmp_path / "data")
    index = SearchIndex(tmp_path / "data", db, {"model": "m"})
    _, rebuild = await asyncio.to_thread(index.open)
    if rebuild is not None:
        await asyncio.wrap_future(rebuild)
    await asyncio.to_thread(index.close)
    holder = search_index.apsw.Connection(str(index.path))
    try:
        holder.execute("PRAGMA locking_mode = EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("UPDATE index_meta SET value = value WHERE key = 'state'")
        with pytest.raises(search_index.apsw.BusyError):
            await asyncio.to_thread(search_index.connect, index.path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        await asyncio.to_thread(db.close)


@pytest.mark.parametrize("where", ["connect", "query"])
@pytest.mark.parametrize("error", ["BusyError", "LockedError", "ProtocolError"])
@pytest.mark.asyncio
async def test_a_read_that_finds_the_file_busy_or_locked_is_refused_and_nothing_rebuilt(tmp_path, monkeypatch, where,
                                                                                         error):
    """A read whose new connection, or whose query, finds the file busy or locked (or a WAL lock race, ProtocolError)
    is refused, and the index is not replaced (its embeddings stay); the next read is answered."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        while not index._readers.empty():
            index._readers.get_nowait().close()
        real, failed = SearchIndex._connect, []

        def connect(self):
            if not failed and self._writer_ident != threading.get_ident():  # a read's, once
                failed.append(True)
                raise getattr(search_index.apsw, error)("database is locked")
            return real(self)
        if where == "connect":
            monkeypatch.setattr(SearchIndex, "_connect", connect)
            with pytest.raises(search_index.Unavailable):
                await asyncio.to_thread(index.keyword, project, "wage", 5)
        else:
            def held(conn):
                failed.append(True)
                raise getattr(search_index.apsw, error)("database is locked")
            with pytest.raises(getattr(search_index.apsw, error)):
                await asyncio.to_thread(index._read, held)
        assert failed and not index.damaged and not index.building  # nothing rebuilt
        assert await asyncio.to_thread(index.keyword, project, "wage", 5)  # the next read is answered


@pytest.mark.parametrize("damage", ["header", "emptied"])
@pytest.mark.asyncio
async def test_no_connection_to_the_index_file_is_opened_outside_a_reads_admission(tmp_path, monkeypatch, damage):
    """Finding 64: a read that finds the file damaged (opening its connection, or in its query) opens no other
    connection to it (the damage check did, outside admission, so neither a rebuild nor the close waited for it):
    every connection to the file outside the writer is opened under the read's admission (_lock held)."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        while not index._readers.empty():
            index._readers.get_nowait().close()
        real_apsw, opened = search_index.apsw, []

        class Recording:  # apsw as search_index sees it, its connections to the index file recorded
            def __getattr__(self, name):
                return getattr(real_apsw, name)

            def Connection(self, path, *args, **kwargs):
                if path == str(index.path) and threading.get_ident() != index._writer_ident:
                    opened.append(index._lock.locked())
                return real_apsw.Connection(path, *args, **kwargs)
        if damage == "header":
            with open(index.path, "r+b") as file:  # SQLite's header string, overwritten
                file.write(b"not a database!!")
        else:
            for suffix in ("-wal", "-shm"):
                Path(f"{index.path}{suffix}").unlink(missing_ok=True)
            os.truncate(index.path, 0)
        monkeypatch.setattr(search_index, "apsw", Recording())
        with pytest.raises((search_index.Unavailable, real_apsw.SQLError)):
            await asyncio.to_thread(index.keyword, project, "wage", 5)
        monkeypatch.undo()
        assert opened and all(opened), opened  # the read's own connection only, under its admission
        assert index.damaged
        await until_rebuilt(index)
        assert await asyncio.to_thread(index.keyword, project, "wage", 5)


@pytest.mark.asyncio
async def test_a_corrupt_header_met_opening_a_read_is_damage_and_rebuilt(tmp_path):
    """The file's header made unreadable while the app runs: a read's new connection fails, which is damage, and
    the file is replaced and rebuilt."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        while not index._readers.empty():
            index._readers.get_nowait().close()
        with open(index.path, "r+b") as file:  # SQLite's header string, overwritten
            file.write(b"not a database!!")
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.keyword, project, "wage", 5)
        assert index.damaged
        await until_rebuilt(index)
        assert await asyncio.to_thread(index.keyword, project, "wage", 5)


@pytest.mark.asyncio
async def test_an_emptied_index_file_is_damage_and_rebuilt(tmp_path):
    """Recheck 8, item 2: the index file emptied (zero bytes) while the app runs. A read's new connection opens, and
    its query fails for the missing tables: damage, so the file is replaced and rebuilt."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        while not index._readers.empty():
            index._readers.get_nowait().close()
        for suffix in ("-wal", "-shm"):
            Path(f"{index.path}{suffix}").unlink(missing_ok=True)
        os.truncate(index.path, 0)
        with pytest.raises(search_index.apsw.SQLError):
            await asyncio.to_thread(index.keyword, project, "wage", 5)
        assert index.damaged
        await until_rebuilt(index)
        assert await asyncio.to_thread(index.keyword, project, "wage", 5)


@pytest.mark.parametrize("table", ["fts_passages_config", "vec_passages_chunks"])
@pytest.mark.asyncio
async def test_a_virtual_tables_missing_backing_table_is_damage_and_rebuilt(tmp_path, table):
    """Finding 65: an FTS5 or vec0 backing table gone while the index's own tables stay: the read that meets it fails
    SQLError, which is damage, so the file is replaced and rebuilt, and the read is answered after."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await until_embedded(client, project)
        index = client.state["index"]
        assert index.vectors
        conn, _ = search_index.connect(index.path)
        try:
            conn.execute(f"DROP TABLE {table}")
        finally:
            conn.close()
        query = [0.1] * DIMENSIONS

        def read():
            return index.keyword(project, "wage", 5) if table.startswith("fts") else index.dense(project, query, 5)
        with pytest.raises(search_index.apsw.SQLError):
            await asyncio.to_thread(read)
        assert index.damaged
        await until_rebuilt(index)
        await until_embedded(client, project)
        assert await asyncio.to_thread(read)


@pytest.mark.asyncio
async def test_a_query_vector_of_other_dimensions_is_refused_before_the_read_and_is_not_damage(tmp_path):
    """dense: a query vector the file's vectors cannot match is refused before its read, so its error is not taken for
    damage (a read's error is)."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await until_embedded(client, project)
        index = client.state["index"]
        with pytest.raises(ValueError):
            await asyncio.to_thread(index.dense, project, [0.1] * (DIMENSIONS - 1), 5)
        for k in (-1, 4097):  # nor a k vec0 refuses
            with pytest.raises(ValueError):
                await asyncio.to_thread(index.dense, project, [0.1] * DIMENSIONS, k)
        assert not index.damaged and not index.building


@pytest.mark.asyncio
async def test_a_read_connection_without_sqlite_vec_is_refused_not_taken_for_damage(tmp_path, monkeypatch):
    """Review 11, P2-1: a read's new connection in which sqlite-vec did not load (the index's vectors are there) is
    refused as unavailable, and not used: its vector read would fail for want of the module, which is not damage."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await until_embedded(client, project)
        index = client.state["index"]
        assert index.vectors
        while not index._readers.empty():
            index._readers.get_nowait().close()
        real, plain = SearchIndex._connect, []

        def connect(self):
            if not plain and self._writer_ident != threading.get_ident():  # a read's, once: no sqlite-vec
                plain.append(search_index.apsw.Connection(str(self.path)))
                return plain[0], False
            return real(self)
        monkeypatch.setattr(SearchIndex, "_connect", connect)
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.dense, project, [0.1] * DIMENSIONS, 5)
        assert plain and not index.damaged and not index.building  # nothing rebuilt
        assert await asyncio.to_thread(index.dense, project, [0.1] * DIMENSIONS, 5)  # the next read is answered


@pytest.mark.asyncio
async def test_a_rebuilt_file_damaged_again_before_any_read_leaves_the_index_unusable_and_its_retry_rebuilds(
        tmp_path, monkeypatch):
    """The loop bound: damage found, the file is rebuilt; that new file is found damaged before any read of it has
    succeeded, so it is not rebuilt again at once (no loop): the index is unusable, its file discarded, and the
    retry armed. The retry rebuilds it; a read of the new file succeeds, which clears that."""
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (60, 60))  # the retry runs when the test says
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        real_all, real_connect, rebuilt, damaged = SearchIndex._rebuild_all, SearchIndex._connect, [], []

        def counted(self):
            if not rebuilt:  # the first read's connection to the new file finds it damaged (no read is admitted before)
                damaged.append(None)
            finished = real_all(self)
            rebuilt.append(finished)
            return finished

        def connect(self):
            if damaged and damaged[-1] is None and self._writer_ident != threading.get_ident():
                damaged[-1] = True
                raise search_index.apsw.CorruptError("database disk image is malformed")
            return real_connect(self)
        monkeypatch.setattr(SearchIndex, "_rebuild_all", counted)
        monkeypatch.setattr(SearchIndex, "_connect", connect)
        assert not index._fresh  # read since the launch
        await asyncio.to_thread(index._damaged)  # damage found: rebuilt at once
        await _until(lambda: rebuilt == [True])
        assert index._fresh
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.keyword, project, "wage", 5)  # (or a run's read, first) meets it damaged
        await _until(lambda: index._rebuild_retry is not None)
        assert damaged == [True] and rebuilt == [True]  # not rebuilt again: no loop
        assert index.unusable and index.building and not index.path.exists()  # unusable, its file discarded
        with pytest.raises(search_index.Unavailable):
            await asyncio.to_thread(index.keyword, project, "wage", 5)
        assert (await client.get(f"/api/projects/{project}/index")).json()["state"] == "unavailable"
        retry = index._rebuild_retry
        retry.cancel()
        await asyncio.to_thread(retry.function)  # the retry, now: it rebuilds
        await _until(lambda: rebuilt == [True, True] and not index.unusable and not index.building)
        assert await asyncio.to_thread(index.keyword, project, "wage", 5)  # a read of it (or a run's, first): that clears
        assert not index._fresh and index._rebuild_failures == 0


@pytest.mark.asyncio
async def test_damage_found_while_a_rebuild_is_under_way_asks_for_no_second_one(tmp_path, monkeypatch):
    """Review 10, P3-1: damage reported while a whole rebuild is already asked for or under way (a read whose check met
    the file the rebuild is replacing) asks for no second rebuild."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        asked = []
        monkeypatch.setattr(SearchIndex, "_rebuild_soon", lambda self, then=None: asked.append(True))
        index.building = True  # as a rebuild under way
        await asyncio.to_thread(index._damaged)
        assert asked == [] and not index.damaged
        index.building = False
