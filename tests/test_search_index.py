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
from scholia_app import run_finished, started
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
    attempts = _failing_rebuilds(monkeypatch, second, failures=1, held=held)
    try:
        async with app(tmp_path) as client:
            index = client.state["index"]
            await _until(lambda: len(attempts) == 2)  # the first failed, after First's rows; the retry waits
            assert index.unusable
            status = (await client.get(f"/api/projects/{first}/index")).json()
            assert (status["state"], status["mode"], status["reason"]) == ("unavailable", "keyword_only", "index_unavailable")
            found = await find(client, first, "earnings")  # First's rows are in the partial file: never read
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
    attempts = _failing_rebuilds(monkeypatch, second, failures=10_000)
    async with app(tmp_path) as client:
        index = client.state["index"]
        await _until(lambda: len(attempts) >= 3)  # tried again, and again
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
