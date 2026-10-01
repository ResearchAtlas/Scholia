import asyncio
import sqlite3
import threading
import time

import pytest

from backend.db import APPLICATION_ID, Database, new_id, utc_now
from backend.db.migrations import MIGRATIONS

TABLES = {
    "projects", "private_routes", "local_declarations", "key_attestations",
    "conversations", "runs", "run_events", "turns",
    "content_files", "materials", "material_versions", "extractions", "passages",
    "candidates", "search_plans", "budget_reservations", "index_queue",
    "citations", "artifacts", "artifact_versions", "suggestion_sets", "section_leases",
    "comment_threads", "comments", "memory_records", "memory_sources",
    "audit_log", "tombstones", "list_checks", "extensions", "mcp_servers", "mcp_tool_pins",
}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data")
    yield database
    database.close()


def general_id(conn):
    return conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0]


def test_new_data_folder_gets_the_latest_schema_and_the_general_project(db):
    def inspect(conn):
        tables = {
            row[1]: row[5] for row in conn.execute("PRAGMA table_list")
            if row[0] == "main" and not row[1].startswith("sqlite_")
        }
        return (
            conn.execute("PRAGMA user_version").fetchone()[0],
            conn.execute("PRAGMA application_id").fetchone()[0],
            conn.execute("PRAGMA journal_mode").fetchone()[0],
            tables,
            conn.execute("SELECT id, name, sensitivity FROM projects").fetchall(),
            conn.execute("PRAGMA foreign_key_check").fetchall(),
        )

    version, app_id, journal, tables, projects, fk_problems = db.read(inspect)
    assert version == len(MIGRATIONS)
    assert app_id == APPLICATION_ID
    assert journal == "wal"
    assert set(tables) == TABLES
    assert all(tables.values()), "every table is STRICT"
    [(project_id, name, sensitivity)] = projects
    assert (name, sensitivity) == ("General", "normal")
    assert len(project_id) == 36 and project_id[14] == "4" and project_id[19] in "89ab"
    assert fk_problems == []


def test_connections_use_the_durability_pragmas(db):
    expected = {
        "foreign_keys": 1, "synchronous": 2, "fullfsync": 1, "checkpoint_fullfsync": 1,
        "secure_delete": 1, "temp_store": 2, "recursive_triggers": 1, "busy_timeout": 5000,
        "journal_size_limit": 64 * 1024 * 1024,
    }

    def pragmas(conn):
        return {name: conn.execute(f"PRAGMA {name}").fetchone()[0] for name in expected}

    assert db.write(pragmas) == expected
    assert db.read(pragmas) == expected


def test_reads_see_committed_writes_and_cannot_write(db):
    db.write(lambda conn: conn.execute("INSERT INTO audit_log (event) VALUES ('test')"))
    assert db.read(lambda conn: conn.execute("SELECT event FROM audit_log").fetchall()) == [("test",)]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.read(lambda conn: conn.execute("INSERT INTO audit_log (event) VALUES ('x')"))


def test_failed_write_rolls_back_and_raises(db):
    def fails(conn):
        conn.execute("INSERT INTO audit_log (event) VALUES ('lost')")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        db.write(fails)
    assert db.read(lambda conn: conn.execute("SELECT count(*) FROM audit_log").fetchone()) == (0,)
    db.write(lambda conn: conn.execute("INSERT INTO audit_log (event) VALUES ('kept')"))
    assert db.read(lambda conn: conn.execute("SELECT count(*) FROM audit_log").fetchone()) == (1,)


def test_writes_from_many_threads_run_one_at_a_time_on_one_thread(db):
    db.write(lambda conn: conn.execute("INSERT INTO audit_log (event, data) VALUES ('counter', '{\"n\": 0}')"))
    writer_threads = set()

    def increment(conn):
        writer_threads.add(threading.get_ident())
        (n,) = conn.execute("SELECT data ->> 'n' FROM audit_log").fetchone()
        time.sleep(0.0005)  # widen the read-modify-write window
        conn.execute("UPDATE audit_log SET data = json_object('n', ?)", (n + 1,))

    def worker():
        for _ in range(25):
            db.write(increment)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert db.read(lambda conn: conn.execute("SELECT data ->> 'n' FROM audit_log").fetchone()) == (200,)
    assert len(writer_threads) == 1 and threading.get_ident() not in writer_threads


def test_write_inside_a_write_is_refused_instead_of_deadlocking(db):
    with pytest.raises(RuntimeError, match="inside a write"):
        db.write(lambda conn: db.write(lambda inner: None))


@pytest.mark.asyncio
async def test_database_calls_are_refused_on_the_event_loop(db):
    with pytest.raises(RuntimeError, match="event loop"):
        db.write(lambda conn: None)
    with pytest.raises(RuntimeError, match="event loop"):
        db.read(lambda conn: None)
    with pytest.raises(RuntimeError, match="event loop"):
        db.backup()
    assert await asyncio.to_thread(db.read, lambda conn: conn.execute("SELECT 1").fetchone()) == (1,)


def test_new_id_and_utc_now_match_the_schema(db):
    def insert(conn):
        conn.execute(
            "INSERT INTO conversations (id, project_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (new_id(), general_id(conn), utc_now(), utc_now()),
        )
        conn.execute(
            "INSERT INTO conversations (id, project_id) VALUES ('0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e', ?)",
            (general_id(conn),),
        )

    db.write(insert)


# Schema rules


def add_run_with_events(conn):
    run_id = new_id()
    conn.execute("INSERT INTO runs (id, project_id, kind) VALUES (?, ?, 'background')", (run_id, general_id(conn)))
    conn.execute(
        "INSERT INTO run_events (run_id, seq, type) VALUES (?, 0, 'route'), (?, 1, 'run_finished')",
        (run_id, run_id),
    )
    return run_id


def events(db, run_id):
    return db.read(lambda conn: conn.execute(
        "SELECT seq, type, data FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall())


@pytest.mark.parametrize("sql", [
    "DELETE FROM projects WHERE kind = 'general'",
    "UPDATE projects SET kind = 'research' WHERE kind = 'general'",
    "UPDATE projects SET id = :new WHERE kind = 'general'",
    "INSERT OR REPLACE INTO projects (id, name, kind) VALUES (:general, 'Replaced', 'research')",
    "INSERT OR REPLACE INTO projects (id, name, kind) VALUES (:new, 'Second', 'general')",
    "INSERT INTO projects (id, name, kind) VALUES (:new, 'Second', 'general')",
], ids=["delete", "rekind", "change-id", "replace", "replace-by-kind", "second-general"])
def test_the_general_project_cannot_be_deleted_or_replaced(db, sql):
    before = db.read(lambda conn: conn.execute("SELECT * FROM projects").fetchall())
    with pytest.raises(sqlite3.IntegrityError):
        db.write(lambda conn: conn.execute(sql, {"general": general_id(conn), "new": new_id()}))
    assert db.read(lambda conn: conn.execute("SELECT * FROM projects").fetchall()) == before


def test_the_general_project_can_be_renamed_and_research_projects_deleted(db):
    project = new_id()

    def change(conn):
        conn.execute("UPDATE projects SET name = 'Personal' WHERE kind = 'general'")
        conn.execute("INSERT INTO projects (id, name, kind) VALUES (?, 'Thesis', 'research')", (project,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project,))

    db.write(change)
    assert db.read(lambda conn: conn.execute("SELECT name, kind FROM projects").fetchall()) == [("Personal", "general")]


@pytest.mark.parametrize("sql", [
    "UPDATE run_events SET data = '{\"edited\": true}' WHERE run_id = :run",
    "DELETE FROM run_events WHERE run_id = :run",
    "INSERT OR REPLACE INTO run_events (run_id, seq, type) VALUES (:run, 0, 'limit_hit')",
    "INSERT INTO run_events (run_id, seq, type) VALUES (:run, 0, 'route') ON CONFLICT DO UPDATE SET type = 'limit_hit'",
], ids=["update", "delete", "replace", "upsert"])
def test_run_events_are_append_only_while_their_run_exists(db, sql):
    run_id = db.write(add_run_with_events)
    before = events(db, run_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.write(lambda conn: conn.execute(sql, {"run": run_id}))
    assert events(db, run_id) == before
    db.write(lambda conn: conn.execute("INSERT INTO run_events (run_id, seq, type) VALUES (?, 2, 'route')", (run_id,)))
    assert [seq for seq, _, _ in events(db, run_id)] == [0, 1, 2]


def test_run_events_are_deleted_only_together_with_their_run(db):
    run_id, other = db.write(add_run_with_events), db.write(add_run_with_events)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):  # checked at commit
        db.write(lambda conn: conn.execute("DELETE FROM runs WHERE id = ?", (run_id,)))
    assert len(events(db, run_id)) == 2

    def purge(conn):  # the deletion path: the run first, then its events, in one transaction
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))

    db.write(purge)
    assert events(db, run_id) == []
    assert len(events(db, other)) == 2


def add_artifact_version(conn):
    artifact, version = new_id(), new_id()
    conn.execute(
        "INSERT INTO artifacts (id, project_id, title, doc) VALUES (?, ?, 'Draft', '{}')", (artifact, general_id(conn)))
    conn.execute(
        "INSERT INTO artifact_versions (id, artifact_id, seq, doc, reason) VALUES (?, ?, 0, '{\"v\": 1}', 'named')",
        (version, artifact))
    return artifact, version


def artifact_versions(db):
    return db.read(lambda conn: conn.execute(
        "SELECT id, artifact_id, seq, doc FROM artifact_versions ORDER BY artifact_id").fetchall())


@pytest.mark.parametrize("sql", [
    "UPDATE artifact_versions SET doc = '{\"v\": 2}' WHERE id = :version",
    "INSERT OR REPLACE INTO artifact_versions (id, artifact_id, seq, doc, reason)"
    " VALUES (:version, :artifact, 0, '{\"v\": 2}', 'named')",
    "INSERT OR REPLACE INTO artifact_versions (id, artifact_id, seq, doc, reason)"
    " VALUES (:new, :artifact, 0, '{\"v\": 2}', 'named')",
    "DELETE FROM artifact_versions WHERE id = :version",
], ids=["update", "replace-by-id", "replace-by-artifact-and-seq", "delete"])
def test_an_artifact_version_document_cannot_change_while_its_artifact_exists(db, sql):
    artifact, version = db.write(add_artifact_version)
    before = artifact_versions(db)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.write(lambda conn: conn.execute(sql, {"version": version, "artifact": artifact, "new": new_id()}))
    assert artifact_versions(db) == before
    db.write(lambda conn: conn.execute("UPDATE artifact_versions SET label = 'v1' WHERE id = ?", (version,)))


@pytest.mark.parametrize("move, delete", [
    ("UPDATE run_events SET run_id = :new WHERE run_id = :run", "DELETE FROM run_events WHERE run_id = :new"),
    ("UPDATE artifact_versions SET artifact_id = :new WHERE id = :version",
     "DELETE FROM artifact_versions WHERE id = :version"),
], ids=["run-event", "artifact-version"])
def test_a_protected_row_cannot_be_moved_off_its_parent_and_then_deleted(db, move, delete):
    run_id = db.write(add_run_with_events)
    _, version = db.write(add_artifact_version)
    before = (events(db, run_id), artifact_versions(db))

    def move_then_delete(conn):  # the moved row's new parent does not exist, so the delete guard would pass
        params = {"new": new_id(), "run": run_id, "version": version}
        conn.execute(move, params)
        conn.execute(delete, params)

    with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
        db.write(move_then_delete)
    assert (events(db, run_id), artifact_versions(db)) == before


def test_artifact_versions_are_deleted_only_together_with_their_artifact(db):
    artifact, _ = db.write(add_artifact_version)
    other, _ = db.write(add_artifact_version)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):  # checked at commit
        db.write(lambda conn: conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact,)))
    assert len(artifact_versions(db)) == 2

    def purge(conn):  # the deletion path: the artifact first, then its versions, in one transaction
        conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact,))
        conn.execute("DELETE FROM artifact_versions WHERE artifact_id = ?", (artifact,))

    db.write(purge)
    assert [row[1] for row in artifact_versions(db)] == [other]


@pytest.mark.parametrize("sql", [
    "INSERT INTO audit_log (event, data) VALUES ('e', '{not json')",
    "INSERT INTO materials (id, project_id, source, csl) VALUES (:id, :general, 'upload', '[1,')",
    "INSERT INTO conversations (id, project_id) VALUES ('not-a-uuid', :general)",
    "INSERT INTO conversations (id, project_id) VALUES (upper(:id), :general)",
    "INSERT INTO conversations (id, project_id) VALUES ('8b1d1e5a-bdce-11f1-8f22-718778ea685b', :general)",
    "INSERT INTO conversations (id, project_id) VALUES ('--------------4----8----------------', :general)",
    "INSERT INTO conversations (id, project_id) VALUES (substr(:id, 1, 2) || '-' || substr(:id, 4), :general)",
    "INSERT INTO audit_log (event, at) VALUES ('e', '2026-10-02 03:18:00.000')",
    "INSERT INTO audit_log (event, at) VALUES ('e', '2026-10-02T11:18:00.000+08:00')",
    "INSERT INTO audit_log (event, at) VALUES ('e', '2026-10-02T03:18:00Z')",
    "INSERT INTO projects (id, name, kind, sensitivity) VALUES (:id, 'p', 'research', 'secret')",
    "INSERT INTO conversations (id, project_id, title_rev) VALUES (:id, :general, 'abc')",
    "INSERT INTO conversations (id, project_id) VALUES (:id, :id)",
    "INSERT INTO runs (id, project_id, kind, attempts) VALUES (:id, :general, 'background', 3)",
    "INSERT INTO memory_records (id, scope, type, content, status) VALUES (:id, 'personal', 'progress', 'x', 'confirmed')",
], ids=[
    "invalid-json", "invalid-json-csl", "id-not-uuid", "id-uppercase", "id-not-version-4",
    "id-only-hyphens", "id-hyphen-inside-a-group",
    "time-without-zone", "time-with-offset", "time-without-milliseconds",
    "unknown-enum", "strict-type", "missing-foreign-key", "background-attempts", "confirmed-progress",
])
def test_the_schema_refuses_invalid_values(db, sql):
    with pytest.raises(sqlite3.IntegrityError):
        db.write(lambda conn: conn.execute(sql, {"id": new_id(), "general": general_id(conn)}))


def test_the_schema_refuses_the_word_now_as_a_time(db):
    with pytest.raises(sqlite3.OperationalError, match="non-deterministic"):
        db.write(lambda conn: conn.execute("INSERT INTO audit_log (event, at) VALUES ('e', 'now')"))


def test_conversations_allow_one_parent_per_project(db):
    def add(conn):
        conn.execute("INSERT INTO conversations (id, project_id, is_parent) VALUES (?, ?, 1)", (new_id(), general_id(conn)))

    db.write(add)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.write(add)
