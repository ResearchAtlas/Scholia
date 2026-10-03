"""Purging deleted data from the automatic backups: a fresh backup, then no older one (section 10, Retention)."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from backend import backups
from backend.db import DB_NAME, ContentStore, Database, delete, new_id
from backend.db.database import KINDS

START = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
SAID = "Participant 7 said the clinic closed in March"  # stands for what was deleted
TITLE = "Interview with participant 7"


def all_generations(data):
    return [path for kind in KINDS if (data / "backups" / kind).exists()
            for path in sorted((data / "backups" / kind).iterdir())]


def holds(generation, text):
    return any(text.encode() in path.read_bytes() for path in generation.rglob("*") if path.is_file())


def a_project_with_a_conversation(data):
    """A project with its settings folder and one conversation whose turn holds SAID, backed up on nine days."""
    db = Database(data)
    project, conversation, run = new_id(), new_id(), new_id()

    def insert(conn):
        conn.execute("INSERT INTO projects (id, name, kind) VALUES (?, 'Clinic study', 'research')", (project,))
        conn.execute("INSERT INTO conversations (id, project_id, title) VALUES (?, ?, ?)",
                     (conversation, project, TITLE))
        conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, status) VALUES (?, ?, ?, 'turn',"
                     " 'succeeded')", (run, project, conversation))
        conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message) VALUES (?, ?, 0,"
                     " 'researcher', ?)", (run, conversation, json.dumps({"text": SAID})))

    db.write(insert)
    folder = data / "projects" / project
    folder.mkdir(parents=True)
    (folder / "AGENTS.md").write_text(f"Notes: {SAID}\n")
    for day in range(9):
        db.backup(now=START + timedelta(days=day))
    assert len(all_generations(data)) == 8 and all(holds(g, SAID) for g in all_generations(data))
    return db, project, conversation


def rows(generation, sql):
    conn = sqlite3.connect((generation / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("remove_all_trace", [False, True])
def test_a_deleted_conversation_is_in_no_automatic_backup_after_a_purge(tmp_path, remove_all_trace):
    data = tmp_path / "data"
    db, project, conversation = a_project_with_a_conversation(data)
    try:
        delete(db, ContentStore(db), "conversation", conversation, remove_all_trace=remove_all_trace)
        removed = backups.purge(db, project, {"kind": "conversation", "object_id": conversation})
        [(event, audited_project, data_json)] = db.read(lambda conn: conn.execute(
            "SELECT event, project_id, data FROM audit_log WHERE event = 'backup_purge'").fetchall())
    finally:
        db.close()
    [generation] = all_generations(data)  # older generations are gone, daily and weekly
    assert removed == 8 and generation.parent.name == "daily"
    assert rows(generation, "SELECT count(*) FROM conversations") == [(0,)]
    assert rows(generation, "SELECT count(*) FROM turns") == [(0,)]
    assert SAID.encode() not in (generation / DB_NAME).read_bytes()
    [(title,)] = rows(generation, f"SELECT title FROM tombstones WHERE object_id = '{conversation}'")
    assert title == (None if remove_all_trace else TITLE)
    assert (TITLE.encode() in (generation / DB_NAME).read_bytes()) is not remove_all_trace
    record = json.loads(data_json)
    assert (audited_project, record["kind"], record["object_id"], record["deleted_backups"]) == (
        project, "conversation", conversation, 8)
    assert record["kept"] == f"daily/{generation.name}"


def test_a_deleted_project_and_its_settings_are_in_no_automatic_backup_after_a_purge(tmp_path):
    data = tmp_path / "data"
    db, project, _ = a_project_with_a_conversation(data)
    try:
        delete(db, ContentStore(db), "project", project, remove_all_trace=True)
        shutil.rmtree(data / "projects" / project)  # as the app removes its folder after the record
        backups.purge(db, project, {"kind": "project", "object_id": project})
    finally:
        db.close()
    [generation] = all_generations(data)
    assert not holds(generation, SAID) and not holds(generation, "Clinic study")
    assert not (generation / "projects" / project).exists()
    assert rows(generation, "SELECT count(*) FROM projects WHERE kind = 'research'") == [(0,)]


def test_a_purge_whose_fresh_backup_fails_keeps_the_older_ones_and_audits_nothing(tmp_path, monkeypatch):
    import backend.db.database as database_module
    data = tmp_path / "data"
    db, project, conversation = a_project_with_a_conversation(data)
    try:
        delete(db, ContentStore(db), "conversation", conversation)

        def full_disk(path):
            raise OSError("no space left")

        monkeypatch.setattr(database_module, "_fsync", full_disk)
        with pytest.raises(OSError):
            backups.purge(db, project, {"kind": "conversation", "object_id": conversation})
        assert db.read(lambda conn: conn.execute(
            "SELECT count(*) FROM audit_log WHERE event = 'backup_purge'").fetchone()) == (0,)
    finally:
        db.close()
    assert len(all_generations(data)) == 8  # a damaged or full disk never leaves no backup at all
