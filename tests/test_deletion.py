import json
import logging
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import backend.db.database as database_module
import backend.db.deletion as deletion_module
from backend.db import DB_NAME, ContentStore, Database, delete, new_id
from backend.db.deletion import DELETE, KINDS, NOT_DELETED, ON_DELETE
from backend.db.migrations import MIGRATIONS
from network_guard import allow_subprocess

ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE = {"audit_log", "tombstones", "index_queue"}
RUNS = ("r1", "r2", "r3", "r4", "r5", "r6")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data")
    yield database
    database.close()


@pytest.fixture
def store(db):
    return ContentStore(db)


def later():
    return datetime.now(UTC) + timedelta(hours=2)


def add_project(db, name):
    project = new_id()
    db.write(lambda conn: conn.execute("INSERT INTO projects (id, name, kind) VALUES (?, ?, 'research')", (project, name)))
    return project


def populate(db, store, project, tag, paper=None):
    """One row in every table a project owns, linked along every foreign key the deletion policy covers.

    c1 holds r1 (a turn), r2 (its retry), r3 (r1's child) and r4 (post-answer work
    keyed to r1's turn). r1 dispatched r5 into c2, and r6 is r5's child. mr1 comes
    only from r1's turn; mr2 would replace mr1 and also comes from r5's turn.
    """
    paper = store.put(paper or f"paper {tag}".encode(), "application/pdf")
    body = store.put(f"tool output {tag}".encode())
    x = {name: new_id() for name in (
        "c1", "c2", *RUNS, "m1", "v1", "e1", "s1", "k1", "b1", "ct1",
        "a1", "av1", "ss1", "t1", "cm1", "mr1", "mr2")}
    x.update(project=project, paper=paper, body=body)

    def add(conn):
        conn.execute(
            "INSERT INTO conversations (id, project_id, title) VALUES (?, ?, ?), (?, ?, ?)",
            (x["c1"], project, f"Chat {tag}", x["c2"], project, f"Other chat {tag}"))
        conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, kind) VALUES (?, ?, ?, 'turn'), (?, ?, ?, 'turn')",
            (x["r1"], project, x["c1"], x["r2"], project, x["c1"]))
        conn.execute(
            "INSERT INTO turns (run_id, conversation_id, seq, author, user_message, retry_of_run_id)"
            " VALUES (?, ?, 0, 'researcher', '{}', NULL), (?, ?, 1, 'researcher', '{}', ?)",
            (x["r1"], x["c1"], x["r2"], x["c1"], x["r1"]))
        conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, parent_run_id, kind) VALUES (?, ?, ?, ?, 'child')",
            (x["r3"], project, x["c1"], x["r1"]))
        conn.execute(
            "INSERT INTO runs (id, project_id, source_turn_id, kind) VALUES (?, ?, ?, 'background')",
            (x["r4"], project, x["r1"]))
        conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, dispatched_by_run_id, kind) VALUES (?, ?, ?, ?, 'turn')",
            (x["r5"], project, x["c2"], x["r1"]))
        conn.execute(
            "INSERT INTO turns (run_id, conversation_id, seq, author, user_message) VALUES (?, ?, 0, 'parent', '{}')",
            (x["r5"], x["c2"]))
        conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, parent_run_id, kind) VALUES (?, ?, ?, ?, 'child')",
            (x["r6"], project, x["c2"], x["r5"]))
        conn.execute(
            "INSERT INTO run_events (run_id, seq, type, body_ref) VALUES (?, 0, 'tool_result', ?), (?, 1, 'run_finished', NULL)",
            (x["r1"], body, x["r1"]))
        conn.execute("INSERT INTO run_events (run_id, seq, type) VALUES (?, 0, 'route')", (x["r5"],))

        conn.execute(
            "INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, ?, 'upload')",
            (x["m1"], project, f"Paper {tag}"))
        conn.execute(
            "INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current) VALUES (?, ?, 0, ?, 1)",
            (x["v1"], x["m1"], paper))
        if not conn.execute("SELECT 1 FROM extractions WHERE file_sha256 = ?", (paper,)).fetchone():
            conn.execute(
                "INSERT INTO extractions (id, file_sha256, extractor, extractor_version, status) VALUES (?, ?, 'pdf', '1', 'done')",
                (x["e1"], paper))
            conn.execute(
                "INSERT INTO passages (id, extraction_id, ordinal, kind, text) VALUES (?, ?, 0, 'paragraph', ?)",
                (x["s1"], x["e1"], f"text {tag}"))
        else:  # the same file in another material shares its extraction
            x["e1"], x["s1"] = conn.execute(
                "SELECT e.id, p.id FROM extractions e JOIN passages p ON p.extraction_id = e.id WHERE e.file_sha256 = ?",
                (paper,)).fetchone()
        conn.execute(
            "INSERT INTO candidates (id, run_id, project_id, source, material_id, status) VALUES (?, ?, ?, 'openalex', ?, 'added')",
            (x["k1"], x["r1"], project, x["m1"]))
        conn.execute("INSERT INTO search_plans (run_id, version, plan) VALUES (?, 0, '{}')", (x["r1"],))
        conn.execute(
            "INSERT INTO budget_reservations (id, run_id, step_seq, paying_conversation_id, project_id, estimate_usd)"
            " VALUES (?, ?, 0, ?, ?, 0.5)",
            (x["b1"], x["r1"], x["c1"], project))
        conn.execute(
            "INSERT INTO citations (id, owner_kind, owner_id, material_id, material_version_id, passage_id, quote,"
            " existence, support_run_id) VALUES (?, 'answer', ?, ?, ?, ?, ?, 'ok', ?)",
            (x["ct1"], x["r1"], x["m1"], x["v1"], x["s1"], f"quote {tag}", x["r1"]))

        conn.execute(
            "INSERT INTO artifacts (id, project_id, title, source_conversation_id, doc) VALUES (?, ?, ?, ?, '{}')",
            (x["a1"], project, f"Draft {tag}", x["c1"]))
        conn.execute(
            "INSERT INTO artifact_versions (id, artifact_id, seq, doc, reason, run_id) VALUES (?, ?, 0, '{}', 'after_run', ?)",
            (x["av1"], x["a1"], x["r1"]))
        conn.execute(
            "INSERT INTO suggestion_sets (id, artifact_id, run_id, section_id, base_rev, section_hash, payload)"
            " VALUES (?, ?, ?, 's1', 0, 'h', '{}')",
            (x["ss1"], x["a1"], x["r1"]))
        conn.execute("INSERT INTO section_leases (artifact_id, section_id, run_id) VALUES (?, 's1', ?)", (x["a1"], x["r1"]))
        conn.execute(
            "INSERT INTO comment_threads (id, artifact_id, anchor, run_id) VALUES (?, ?, '{}', ?)", (x["t1"], x["a1"], x["r1"]))
        conn.execute(
            "INSERT INTO comments (id, thread_id, author, body) VALUES (?, ?, 'researcher', ?)", (x["cm1"], x["t1"], f"note {tag}"))

        conn.execute(
            "INSERT INTO memory_records (id, scope, project_id, type, content, status, created_by_run_id)"
            " VALUES (?, 'project', ?, 'fact', ?, 'inferred', ?)",
            (x["mr1"], project, f"fact {tag}", x["r4"]))
        conn.execute(
            "INSERT INTO memory_records (id, scope, project_id, type, content, status, supersedes_id, created_by_run_id)"
            " VALUES (?, 'project', ?, 'fact', ?, 'proposed', ?, ?)",
            (x["mr2"], project, f"newer fact {tag}", x["mr1"], x["r4"]))
        conn.execute(
            "INSERT INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, 'turn', ?), (?, 'turn', ?), (?, 'turn', ?)",
            (x["mr1"], x["r1"], x["mr2"], x["r1"], x["mr2"], x["r5"]))

    db.write(add)
    return x


def dump(db, skip=()):
    """Every row of every table, except the tables in skip."""
    def read(conn):
        tables = [name for (name,) in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in tables if table not in skip
        }
    return db.read(read)


def one(db, sql, *params):
    return db.read(lambda conn: conn.execute(sql, params).fetchone())


def ids_in(db, table, ids, column="id"):
    marks = ", ".join("?" * len(ids))
    return {row[0] for row in db.read(lambda conn: conn.execute(
        f"SELECT {column} FROM {table} WHERE {column} IN ({marks})", ids).fetchall())}


def tombstones(db):
    return set(db.read(lambda conn: conn.execute("SELECT object_id, kind, title FROM tombstones").fetchall()))


def audit(db):
    return [(event, project, json.loads(data)) for event, project, data in db.read(lambda conn: conn.execute(
        "SELECT event, project_id, data FROM audit_log ORDER BY seq").fetchall())]


def queued(db):
    return set(db.read(lambda conn: conn.execute("SELECT target, target_id, project_id, op FROM index_queue").fetchall()))


def foreign_key_problems(db):
    return db.read(lambda conn: conn.execute("PRAGMA foreign_key_check").fetchall())


# The policy


def test_the_policy_covers_every_foreign_key_into_a_table_rows_are_deleted_from(db):
    foreign_keys = db.read(lambda conn: conn.execute(
        'SELECT m.name, f."from", f."table" FROM sqlite_schema m, pragma_foreign_key_list(m.name) f'
        " WHERE m.type = 'table'").fetchall())
    assert set(ON_DELETE) == {(child, column) for child, column, parent in foreign_keys if parent not in NOT_DELETED}
    assert {parent for _, _, parent in foreign_keys if parent in NOT_DELETED} == NOT_DELETED
    for (child, column), action in ON_DELETE.items():
        assert action is DELETE or f"{column} = NULL" in action, (child, column)


def test_the_fixture_exercises_every_policy(db, store):
    populate(db, store, add_project(db, "Thesis"), "p")
    for child, column in ON_DELETE:
        assert one(db, f"SELECT count(*) FROM {child} WHERE {column} IS NOT NULL") > (0,), (child, column)


def test_a_foreign_key_without_a_policy_stops_the_deletion(db, store, monkeypatch):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    before = dump(db)
    monkeypatch.delitem(ON_DELETE, ("artifacts", "source_conversation_id"))
    with pytest.raises(RuntimeError, match="no deletion policy for artifacts.source_conversation_id"):
        delete(db, store, "conversation", x["c1"])
    assert dump(db) == before


def test_a_reference_left_dangling_fails_the_deletion_at_commit(db, store, monkeypatch):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    before = dump(db)
    monkeypatch.setitem(ON_DELETE, ("artifacts", "source_conversation_id"), "source_conversation_id = source_conversation_id")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        delete(db, store, "conversation", x["c1"])
    assert dump(db) == before
    monkeypatch.undo()
    delete(db, store, "conversation", x["c1"])  # the temporary state was rolled back with it


# Projects


def test_deleting_a_project_removes_everything_it_owned_and_nothing_else(db, store, tmp_path):
    other = populate(db, store, add_project(db, "Other"), "q")
    before = dump(db, skip=GOVERNANCE)
    project = add_project(db, "Thesis")
    x = populate(db, store, project, "p")

    revoked = delete(db, store, "project", project)

    assert revoked == sorted(x[run] for run in RUNS)  # every run was active
    assert dump(db, skip=GOVERNANCE | {"content_files"}) == {
        table: rows for table, rows in before.items() if table != "content_files"}
    assert foreign_key_problems(db) == []
    assert tombstones(db) == {
        (project, "project", "Thesis"),
        (x["c1"], "conversation", "Chat p"), (x["c2"], "conversation", "Other chat p"),
        (x["m1"], "material", "Paper p"), (x["a1"], "artifact", "Draft p"),
        (x["mr1"], "memory", None), (x["mr2"], "memory", None),
    }
    assert queued(db) == {
        ("passage", x["s1"], project, "remove"),
        ("memory", x["mr1"], project, "remove"), ("memory", x["mr2"], project, "remove"),
    }
    [(event, audited_project, data)] = audit(db)
    assert (event, audited_project, data["kind"], data["object_id"]) == ("deletion", project, "project", project)
    assert data["deleted"]["runs"] == 6 and data["revoked_runs"] == 6
    assert not any(text in json.dumps(data) for text in ("Thesis", "Chat p", "Paper p", "Draft p", "fact p", "quote p"))
    assert (tmp_path / "data" / f"{DB_NAME}-wal").stat().st_size == 0  # no deleted page left in the WAL

    store.collect_garbage(now=later())  # the deleted project's files were added just now
    assert dump(db, skip=GOVERNANCE)["content_files"] == before["content_files"]
    assert store.read(other["paper"]) == b"paper q"
    with pytest.raises(FileNotFoundError):
        store.read(x["paper"])


def test_the_general_project_cannot_be_deleted(db, store):
    general = one(db, "SELECT id FROM projects WHERE kind = 'general'")[0]
    populate(db, store, general, "g")
    before = dump(db)
    with pytest.raises(ValueError, match="General project cannot be deleted"):
        delete(db, store, "project", general)
    assert dump(db) == before


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_an_object_that_does_not_exist_changes_nothing(db, store, kind):
    populate(db, store, add_project(db, "Thesis"), "p")
    before = dump(db)
    with pytest.raises(LookupError):
        delete(db, store, kind, new_id())
    assert dump(db) == before


@pytest.mark.parametrize("kind", ["run", "turn", "projects", ""])
def test_an_unknown_kind_is_refused(db, store, kind):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    before = dump(db)
    with pytest.raises(ValueError, match="cannot delete"):
        delete(db, store, kind, x["r1"])
    assert dump(db) == before


def test_an_object_can_be_deleted_only_once(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    delete(db, store, "material", x["m1"])
    before = dump(db)
    with pytest.raises(LookupError):
        delete(db, store, "material", x["m1"])
    assert dump(db) == before


# Conversations


def test_deleting_a_conversation_removes_its_runs_and_keeps_what_others_own(db, store):
    project = add_project(db, "Thesis")
    x = populate(db, store, project, "p")
    db.write(lambda conn: conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = ?", (x["r2"],)))

    revoked = delete(db, store, "conversation", x["c1"])

    assert revoked == sorted(x[run] for run in ("r1", "r3", "r4", "r5", "r6"))  # r2 had finished
    gone = [x[name] for name in ("c1", "r1", "r2", "r3", "r4", "k1", "ct1", "mr1")]
    assert ids_in(db, "conversations", gone) | ids_in(db, "runs", gone) | ids_in(db, "candidates", gone) == set()
    assert ids_in(db, "citations", gone) | ids_in(db, "memory_records", gone) == set()
    # Spending stays in the project, content-free, without its run and conversation.
    assert one(db, "SELECT run_id, paying_conversation_id, project_id, estimate_usd FROM budget_reservations WHERE id = ?",
               x["b1"]) == (None, None, project, 0.5)
    assert ids_in(db, "turns", gone, "run_id") | ids_in(db, "run_events", gone, "run_id") == set()
    assert ids_in(db, "search_plans", gone, "run_id") | ids_in(db, "section_leases", gone, "run_id") == set()

    # The dispatched turn and its child stay in their conversation, revoked.
    assert db.read(lambda conn: conn.execute(
        "SELECT id, dispatched_by_run_id, status, cancel_reason FROM runs ORDER BY id").fetchall()) == sorted([
            (x["r5"], None, "running", "revoked"), (x["r6"], None, "running", "revoked")])
    assert ids_in(db, "turns", [x["r5"]], "run_id") == {x["r5"]}
    assert ids_in(db, "run_events", [x["r5"]], "run_id") == {x["r5"]}
    # The artifact and its records stay, without their links to the deleted runs.
    assert one(db, "SELECT source_conversation_id FROM artifacts WHERE id = ?", x["a1"]) == (None,)
    for table, row in (("artifact_versions", "av1"), ("suggestion_sets", "ss1"), ("comment_threads", "t1")):
        assert one(db, f"SELECT run_id FROM {table} WHERE id = ?", x[row]) == (None,), table
    assert ids_in(db, "comments", [x["cm1"]]) == {x["cm1"]}
    # The material is the project's, not the conversation's.
    assert ids_in(db, "materials", [x["m1"]]) == {x["m1"]}
    assert ids_in(db, "passages", [x["s1"]]) == {x["s1"]}
    # mr1 came only from the deleted turn; mr2 keeps its other source and loses its links.
    assert one(db, "SELECT supersedes_id, created_by_run_id FROM memory_records WHERE id = ?", x["mr2"]) == (None, None)
    assert db.read(lambda conn: conn.execute("SELECT memory_id, source_id FROM memory_sources").fetchall()) == [
        (x["mr2"], x["r5"])]

    assert tombstones(db) == {(x["c1"], "conversation", "Chat p"), (x["mr1"], "memory", None)}
    assert queued(db) == {("memory", x["mr1"], x["project"], "remove")}
    assert foreign_key_problems(db) == []


def test_work_for_a_deleted_run_cannot_recreate_it(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    delete(db, store, "conversation", x["c1"])
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):  # a late step's events
        db.write(lambda conn: conn.execute("INSERT INTO run_events (run_id, seq, type) VALUES (?, 2, 'route')", (x["r1"],)))
    changed = db.write(lambda conn: conn.execute(
        "UPDATE runs SET status = 'succeeded' WHERE id = ?", (x["r1"],)).rowcount)
    assert changed == 0
    assert ids_in(db, "run_events", [x["r1"]], "run_id") == set()


# Materials


def test_deleting_a_material_removes_its_versions_and_marks_citations_elsewhere(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    only_source = new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO memory_records (id, scope, project_id, type, content, status) VALUES (?, 'project', ?, 'fact', 'x', 'inferred')",
        (only_source, x["project"])) and conn.execute(
        "INSERT INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, 'material', ?)", (only_source, x["m1"])))

    assert delete(db, store, "material", x["m1"]) == []

    assert ids_in(db, "materials", [x["m1"]]) | ids_in(db, "material_versions", [x["v1"]]) == set()
    assert ids_in(db, "extractions", [x["e1"]]) | ids_in(db, "passages", [x["s1"]]) == set()
    assert one(db, "SELECT material_id, material_version_id, passage_id, existence, quote FROM citations WHERE id = ?",
               x["ct1"]) == (None, None, None, "source_removed", "quote p")
    assert one(db, "SELECT material_id, status FROM candidates WHERE id = ?", x["k1"]) == (None, "added")
    assert ids_in(db, "memory_records", [only_source]) == set()
    assert tombstones(db) == {(x["m1"], "material", "Paper p"), (only_source, "memory", None)}
    assert queued(db) == {("passage", x["s1"], x["project"], "remove"), ("memory", only_source, x["project"], "remove")}
    assert foreign_key_problems(db) == []
    store.collect_garbage(now=later())
    with pytest.raises(FileNotFoundError):
        store.read(x["paper"])
    assert store.read(x["body"]) == b"tool output p"  # still in a run's record


def test_a_file_shared_with_another_material_keeps_its_extraction(db, store):
    paper = b"the same paper"
    x = populate(db, store, add_project(db, "Thesis"), "p", paper=paper)
    other = populate(db, store, add_project(db, "Other"), "q", paper=paper)
    same_project = new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO materials (id, project_id, source) VALUES (?, ?, 'upload')", (same_project, x["project"])))
    db.write(lambda conn: conn.execute(
        "INSERT INTO material_versions (id, material_id, seq, file_sha256) VALUES (?, ?, 0, ?)",
        (new_id(), same_project, x["paper"])))
    assert other["s1"] == x["s1"]  # one extraction for the file

    delete(db, store, "material", x["m1"])
    assert ids_in(db, "passages", [x["s1"]]) == {x["s1"]}
    assert one(db, "SELECT material_id, passage_id, existence FROM citations WHERE id = ?", x["ct1"]) == (
        None, None, "source_removed")  # no way back to the passage through another project's material
    assert queued(db) == set()  # another material in the project still uses the file

    delete(db, store, "material", same_project)
    assert queued(db) == {("passage", x["s1"], x["project"], "remove")}  # only this project's index rows
    assert ids_in(db, "passages", [x["s1"]]) == {x["s1"]}  # the other project still uses it
    assert one(db, "SELECT existence FROM citations WHERE id = ?", other["ct1"]) == ("ok",)

    delete(db, store, "material", other["m1"])
    assert ids_in(db, "extractions", [x["e1"]]) | ids_in(db, "passages", [x["s1"]]) == set()
    assert foreign_key_problems(db) == []


# Artifacts and memory


def test_deleting_an_artifact_removes_its_versions_suggestions_and_comments(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    cited = new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO citations (id, owner_kind, owner_id, material_id, existence) VALUES (?, 'artifact', ?, ?, 'ok')",
        (cited, x["a1"], x["m1"])))

    assert delete(db, store, "artifact", x["a1"]) == []

    for table, name in (("artifacts", "a1"), ("artifact_versions", "av1"), ("suggestion_sets", "ss1"),
                        ("comment_threads", "t1"), ("comments", "cm1")):
        assert ids_in(db, table, [x[name]]) == set(), table
    assert ids_in(db, "section_leases", [x["a1"]], "artifact_id") == set()
    assert ids_in(db, "citations", [cited, x["ct1"]]) == {x["ct1"]}
    assert ids_in(db, "runs", [x["r1"]]) == {x["r1"]}
    assert one(db, "SELECT count(*) FROM runs WHERE cancel_reason IS NOT NULL") == (0,)
    assert tombstones(db) == {(x["a1"], "artifact", "Draft p")}
    assert foreign_key_problems(db) == []


def test_forgetting_a_memory_record(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    assert delete(db, store, "memory", x["mr1"]) == []
    assert ids_in(db, "memory_records", [x["mr1"], x["mr2"]]) == {x["mr2"]}
    assert ids_in(db, "memory_sources", [x["mr1"]], "memory_id") == set()
    assert one(db, "SELECT supersedes_id FROM memory_records WHERE id = ?", x["mr2"]) == (None,)
    assert tombstones(db) == {(x["mr1"], "memory", None)}
    assert queued(db) == {("memory", x["mr1"], x["project"], "remove")}
    [(_, audited_project, data)] = audit(db)
    assert (audited_project, data["kind"]) == (x["project"], "memory")


def test_personal_memory_loses_only_deleted_sources(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    other = populate(db, store, add_project(db, "Other"), "q")
    mixed, only_here, unsourced, project_unsourced = new_id(), new_id(), new_id(), new_id()

    def add(conn):
        for record in (mixed, only_here, unsourced):
            conn.execute(
                "INSERT INTO memory_records (id, scope, project_id, type, content, status)"
                " VALUES (?, 'personal', ?, 'preference', 'x', 'confirmed')", (record, x["project"]))
        conn.execute(
            "INSERT INTO memory_records (id, scope, project_id, type, content, status)"
            " VALUES (?, 'project', ?, 'agent_note', 'x', 'inferred')", (project_unsourced, x["project"]))
        conn.execute(
            "INSERT INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, 'turn', ?), (?, 'turn', ?), (?, 'turn', ?)",
            (mixed, x["r1"], mixed, other["r1"], only_here, x["r1"]))

    db.write(add)
    delete(db, store, "project", x["project"])
    assert db.read(lambda conn: conn.execute(
        "SELECT id, project_id FROM memory_records WHERE scope = 'personal' ORDER BY id").fetchall()) == sorted([
            (mixed, None), (unsourced, None)])
    assert db.read(lambda conn: conn.execute(
        "SELECT source_id FROM memory_sources WHERE memory_id = ?", (mixed,)).fetchall()) == [(other["r1"],)]
    assert {(only_here, "memory", None), (project_unsourced, "memory", None)} <= tombstones(db)
    assert ids_in(db, "memory_records", [project_unsourced]) == set()  # project memory goes with its project


def test_a_turns_run_goes_with_the_turn(db, store):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    db.write(lambda conn: conn.execute("UPDATE runs SET conversation_id = NULL WHERE id = ?", (x["r2"],)))
    delete(db, store, "conversation", x["c1"])
    assert ids_in(db, "runs", [x["r2"]]) == set()


def test_remove_all_trace_drops_the_titles(db, store):
    project = add_project(db, "Thesis")
    x = populate(db, store, project, "p")
    delete(db, store, "project", project, remove_all_trace=True)
    assert {title for _, _, title in tombstones(db)} == {None}
    assert {object_id for object_id, _, _ in tombstones(db)} == {project, x["c1"], x["c2"], x["m1"], x["a1"], x["mr1"], x["mr2"]}
    [(_, _, data)] = audit(db)
    assert data["remove_all_trace"] is True


# Tombstones guard deleted ids


NEW_ROW = {  # kind: a row for that table, with id :id, in project :project where it needs one
    "project": "INTO projects (id, name, kind) VALUES (:id, 'Back', 'research')",
    "conversation": "INTO conversations (id, project_id) VALUES (:id, :project)",
    "material": "INTO materials (id, project_id, source) VALUES (:id, :project, 'upload')",
    "artifact": "INTO artifacts (id, project_id, title, doc) VALUES (:id, :project, 'Back', '{}')",
    "memory": "INTO memory_records (id, scope, type, content, status) VALUES (:id, 'personal', 'fact', 'x', 'confirmed')",
}
FIXTURE_NAME = {"conversation": "c1", "material": "m1", "artifact": "a1", "memory": "mr1"}
assert set(NEW_ROW) == set(KINDS) == set(FIXTURE_NAME) | {"project"}


def deleted_object(db, store, kind, remove_all_trace=False):
    """Delete an object of kind; returns its id and a project that still exists."""
    project = add_project(db, "Thesis")
    x = populate(db, store, project, "p")
    object_id = project if kind == "project" else x[FIXTURE_NAME[kind]]
    delete(db, store, kind, object_id, remove_all_trace=remove_all_trace)
    return object_id, one(db, "SELECT id FROM projects WHERE kind = 'general'")[0]


@pytest.mark.parametrize("statement", [
    "INSERT {row}", "INSERT OR REPLACE {row}", "REPLACE {row}", "INSERT {row} ON CONFLICT DO NOTHING",
], ids=["insert", "insert-or-replace", "replace", "upsert"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_deleted_id_cannot_be_inserted_again(db, store, kind, statement):
    object_id, project = deleted_object(db, store, kind)
    before = dump(db)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be used again"):
        db.write(lambda conn: conn.execute(statement.format(row=NEW_ROW[kind]), {"id": object_id, "project": project}))
    assert dump(db) == before


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_row_cannot_take_a_deleted_id(db, store, kind):
    object_id, project = deleted_object(db, store, kind, remove_all_trace=True)  # the tombstone keeps its id
    other = new_id()
    table = KINDS[kind][0]
    db.write(lambda conn: conn.execute(f"INSERT {NEW_ROW[kind]}", {"id": other, "project": project}))
    before = dump(db)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be used again"):
        db.write(lambda conn: conn.execute(f"UPDATE {table} SET id = ? WHERE id = ?", (object_id, other)))
    assert dump(db) == before


def test_a_database_at_schema_1_gets_the_guard_for_its_earlier_deletions(tmp_path):
    data = tmp_path / "data"
    with Database(data, migrations=MIGRATIONS[:1]) as db:
        store = ContentStore(db)
        x = populate(db, store, add_project(db, "Thesis"), "p")
        delete(db, store, "conversation", x["c1"])

    with Database(data) as db:
        assert one(db, "PRAGMA user_version") == (len(MIGRATIONS),)
        with pytest.raises(sqlite3.IntegrityError, match="cannot be used again"):
            db.write(lambda conn: conn.execute(
                "INSERT INTO conversations (id, project_id) VALUES (?, ?)", (x["c1"], x["project"])))
        db.write(lambda conn: conn.execute(
            "INSERT INTO conversations (id, project_id) VALUES (?, ?)", (new_id(), x["project"])))
    generation = sorted((data / "backups" / "daily").iterdir())[0]  # taken before the first migration it ran
    backup = sqlite3.connect(f"{(generation / DB_NAME).as_uri()}?mode=ro", uri=True)
    try:
        assert backup.execute("PRAGMA user_version").fetchone() == (1,)
    finally:
        backup.close()


# Revocation


def test_a_scope_link_revokes_runs_using_a_deleted_record(db, store, monkeypatch):
    x = populate(db, store, add_project(db, "Thesis"), "p")
    indexing, child, finished = new_id(), new_id(), new_id()

    def add(conn):
        for run, status in ((indexing, "running"), (finished, "succeeded")):
            conn.execute(
                "INSERT INTO runs (id, project_id, kind, inputs, status) VALUES (?, ?, 'background', json_object('material', ?), ?)",
                (run, x["project"], x["m1"], status))
        conn.execute(
            "INSERT INTO runs (id, project_id, parent_run_id, kind) VALUES (?, ?, ?, 'child')", (child, x["project"], indexing))

    db.write(add)
    monkeypatch.setattr(deletion_module, "SCOPE_LINKS", (
        "SELECT id FROM runs WHERE inputs ->> 'material' IN"
        " (SELECT id FROM materials WHERE rowid IN (SELECT rid FROM temp.doomed WHERE tbl = 'materials'))",
    ))
    assert delete(db, store, "material", x["m1"]) == sorted([indexing, child])
    assert db.read(lambda conn: conn.execute(
        "SELECT id, status, cancel_reason FROM runs WHERE cancel_reason IS NOT NULL ORDER BY id").fetchall()) == sorted([
            (indexing, "running", "revoked"), (child, "running", "revoked")])


# Failures


def test_a_failure_late_in_the_deletion_changes_nothing(db, store):
    project = add_project(db, "Thesis")
    populate(db, store, project, "p")
    db.write(lambda conn: conn.execute(
        "CREATE TRIGGER fail_late AFTER DELETE ON run_events BEGIN SELECT RAISE(ABORT, 'injected failure'); END"))
    before, files = dump(db), sorted(store.root.rglob("*"))
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        delete(db, store, "project", project)
    assert dump(db) == before  # no rows, tombstones, audit record, queued removals or revocations
    assert sorted(store.root.rglob("*")) == files
    db.write(lambda conn: conn.execute("DROP TRIGGER fail_late"))
    delete(db, store, "project", project)  # the temporary state was rolled back with it


def test_failures_after_the_commit_keep_the_deletion_and_are_retried(db, store, monkeypatch, caplog):
    x = populate(db, store, add_project(db, "Thesis"), "p")

    def fails(*args, **kwargs):
        raise OSError(f"cannot remove {x['paper']}")

    monkeypatch.setattr(store, "collect_garbage", fails)
    monkeypatch.setattr(db, "checkpoint", fails)
    with caplog.at_level(logging.WARNING, logger="backend.db.deletion"):
        delete(db, store, "material", x["m1"])
    assert ids_in(db, "materials", [x["m1"]]) == set()
    assert len(caplog.records) == 2
    assert x["paper"] not in caplog.text  # no names or paths in the log
    monkeypatch.undo()
    store.collect_garbage(now=later())
    with pytest.raises(FileNotFoundError):
        store.read(x["paper"])


def test_a_truncation_blocked_by_a_reader_is_retried_after_the_next_write(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(database_module, "_PRAGMAS", tuple(
        "busy_timeout = 1000" if pragma.startswith("busy_timeout") else pragma for pragma in database_module._PRAGMAS))
    data = tmp_path / "data"
    wal = data / f"{DB_NAME}-wal"

    def add_audit_row(conn):
        conn.execute("INSERT INTO audit_log (event) VALUES ('test')")

    with Database(data) as db:
        store = ContentStore(db)
        x = populate(db, store, add_project(db, "Thesis"), "p")
        reader = sqlite3.connect((data / DB_NAME).resolve().as_uri() + "?mode=ro", uri=True, autocommit=True)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM materials").fetchone()  # holds a snapshot that needs the WAL
        with caplog.at_level(logging.WARNING, logger="backend.db.deletion"):
            delete(db, store, "material", x["m1"])
        assert "could not be truncated" in caplog.text
        assert wal.stat().st_size > 0

        started = time.monotonic()
        db.write(add_audit_row)  # retried without waiting for the reader
        assert time.monotonic() - started < 0.5
        assert wal.stat().st_size > 0

        reader.close()
        db.write(add_audit_row)  # retried after this write, now that the reader is gone
        assert wal.stat().st_size == 0
        db.write(add_audit_row)  # done: later writes use the WAL as usual
        assert wal.stat().st_size > 0


KILLED_DELETION = r"""
import os, signal, sqlite3, sys, time


def crash(*args):
    os.kill(os.getpid(), signal.SIGKILL)
    while True:  # the signal is delivered asynchronously; never return to the next statement
        time.sleep(1)


real_connect = sqlite3.connect

def connect(*args, **kwargs):
    conn = real_connect(*args, **kwargs)
    conn.create_function("crash", 0, crash)
    conn.execute("PRAGMA cache_size = 10")  # spill uncommitted pages into the WAL
    return conn

sqlite3.connect = connect
from backend.db import ContentStore, Database, delete

data, when, kind, object_id = sys.argv[1:]
db = Database(data)
store = ContentStore(db)
if when == "inside":
    db.write(lambda conn: conn.execute("CREATE TRIGGER crash_inside AFTER DELETE ON run_events BEGIN SELECT crash(); END"))
else:
    store.collect_garbage = crash
delete(db, store, kind, object_id)
"""


def run_killed(data, when, kind, object_id):
    with allow_subprocess(sys.executable):  # the child imports only the standard library and backend.db
        child = subprocess.run(
            [sys.executable, "-c", KILLED_DELETION, str(data), when, kind, object_id],
            cwd=ROOT, capture_output=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()


def test_a_process_killed_inside_the_deletion_changes_nothing(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        project = add_project(db, "Thesis")
        populate(db, ContentStore(db), project, "p")
        before = dump(db)
    files = sorted((data / "content").rglob("*"))

    run_killed(data, "inside", "project", project)

    with Database(data) as db:
        db.write(lambda conn: conn.execute("DROP TRIGGER crash_inside"))
        assert dump(db) == before
        assert sorted((data / "content").rglob("*")) == files
        delete(db, ContentStore(db), "project", project)


def test_a_process_killed_after_the_commit_keeps_the_deletion(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        x = populate(db, ContentStore(db), add_project(db, "Thesis"), "p")

    run_killed(data, "after", "material", x["m1"])

    with Database(data) as db:
        store = ContentStore(db)
        assert ids_in(db, "materials", [x["m1"]]) == set()
        assert (x["m1"], "material", "Paper p") in tombstones(db)
        assert store.read(x["paper"]) == b"paper p"  # not collected before the kill
        store.collect_garbage(now=later())
        with pytest.raises(FileNotFoundError):
            store.read(x["paper"])
