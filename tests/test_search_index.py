"""The search index file (S1-17; slice-1 spec sections 1.5, 4.3, 7.2 and 14; ticket 17's upkeep and
deletion): the tokenizer, the file's schema, modes and metadata, staleness at open (another model or
tokenizer, a restored database, a damaged or unfinished file), secure delete checked in the file's
own bytes, and sqlite-vec failing to load. Synthetic text only."""

import asyncio
import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

import backend.search_index as search_index
from backend.search_index import DIMENSIONS, SearchIndex, match, tokens
from scholia_app import run_finished, started
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
        assert status["passages"] == {"indexed": 4, "embedded": 4, "embeddable": 4}  # what the backup holds
        keyword = client.state["index"].keyword
        assert await asyncio.to_thread(keyword, project, "最低工资", 50) == []
        assert (await find(client, project, "earnings"))["results"]


@pytest.mark.asyncio
async def test_a_read_during_a_rebuild_never_makes_the_file_and_the_rebuilt_file_is_owner_only(tmp_path):
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
        with pytest.raises(Exception):  # as a read landing while the file is replaced: refused, the file not made
            await asyncio.to_thread(index.counts, project)
        assert not path.exists()
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
        assert response.status_code == 200 and response.json()["coverage"] == {"embedded": 0, "total": 0}
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
    """The internal review of b682b9b, P3: the close's discard of a file a whole rebuild has not finished is
    guarded as a failed rebuild's is (_discard): a failure closing its readers leaves it removed all the same."""
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
async def test_a_close_whose_wal_truncation_stays_owed_discards_the_file(tmp_path, monkeypatch):
    """The internal review of fix batch 5, P3: a deletion's removals applied but the WAL's truncation failing,
    at the pass and again as the index closes: old WAL frames may hold the paper's text, so the close
    discards the file (the next launch rebuilds it) rather than leave them to SQLite's own close."""
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
    assert len(failing.calls) >= 2 and not any(f.exists() for f in files)  # tried again at the close, then discarded
    async with app(tmp_path) as client:
        status = await until_embedded(client, project)
        assert status["passages"]["indexed"] == 4  # the kept paper's
        assert await asyncio.to_thread(client.state["index"].keyword, project, "wombats", 50) == []


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
