"""A damaged database is never reset: writing stops, and a restore moves it aside, whole, and brings a backup back."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from backend.db import DB_NAME, Database, new_id
from scholia_app import send, started

pytestmark = pytest.mark.asyncio

START = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)


def damage_the_projects_table(data, project):
    """Rename a project's id in the table's only page, so it disagrees with its primary key index:
    quick_check still passes (the app opens it), integrity_check fails (the next backup's check)."""
    conn = sqlite3.connect((data / DB_NAME).as_uri() + "?mode=ro", uri=True)
    (page,) = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name = 'projects'").fetchone()
    (size,) = conn.execute("PRAGMA page_size").fetchone()
    conn.close()
    with open(data / DB_NAME, "r+b") as file:
        file.seek((page - 1) * size)
        content = file.read(size)
        assert content.count(project.encode()) == 1
        file.seek((page - 1) * size)
        file.write(content.replace(project.encode(), new_id().encode()))


async def test_a_database_found_damaged_while_open_is_moved_aside_whole_by_a_restore(tmp_path):
    data, project = tmp_path / "data", new_id()

    def a_backup_then_damage():
        with Database(data) as db:
            db.write(lambda conn: conn.execute(
                "INSERT INTO projects (id, name, kind) VALUES (?, 'Good', 'research')", (project,)))
            good = db.backup(now=START)  # an earlier day's backup, so the launch backs up again
        damage_the_projects_table(data, project)
        return good

    good = await asyncio.to_thread(a_backup_then_damage)
    damaged = (data / DB_NAME).read_bytes()

    async with started(data, setup=False) as client:
        assert client.state["db"].damaged  # the launch backup's full check found it; writing stopped
        listed = (await client.get("/api/backups")).json()["backups"]
        assert [b["id"] for b in listed] == [f"daily/{good.name}"]  # no backup of the damaged file

        response = await client.post("/api/backups/restore", json={"generation": f"daily/{good.name}"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["safety_copy"] is None and result["damaged_copy"].startswith("damaged/")
        aside = data / "backups" / result["damaged_copy"]
        assert (aside / DB_NAME).read_bytes() == damaged  # moved aside unchanged, never deleted
        assert sorted(p.name for p in aside.iterdir() if p.name.startswith(DB_NAME)) <= sorted(
            [DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"])

        # The app runs on the restored database, writes included.
        assert client.state["db"].damaged is None
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Good"}
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201
        [(record,)] = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT data FROM audit_log WHERE event = 'restore'").fetchall())
        assert json.loads(record)["damaged_copy"] == result["damaged_copy"]
    for path in [aside, *aside.rglob("*")]:
        assert (path.stat().st_mode & 0o777) == (0o700 if path.is_dir() else 0o600), path


def damage_beyond_opening(data):
    """Damage a b-tree page header so that even quick_check fails: the app cannot open it at startup."""
    conn = sqlite3.connect((data / DB_NAME).as_uri() + "?mode=ro", uri=True)
    (page,) = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name = 'audit_log'").fetchone()
    (size,) = conn.execute("PRAGMA page_size").fetchone()
    conn.close()
    with open(data / DB_NAME, "r+b") as file:
        file.seek((page - 1) * size)
        file.write(b"\xff" * 16)


async def test_a_database_damaged_at_startup_is_never_reset_and_only_restore_is_offered(tmp_path):
    data = tmp_path / "data"

    def a_backup_then_damage():
        with Database(data) as db:
            db.write(lambda conn: conn.executemany("INSERT INTO audit_log (event) VALUES (?)", [("e",)] * 2000))
            db.write(lambda conn: conn.execute(
                "INSERT INTO projects (id, name, kind) VALUES (?, 'Good', 'research')", (new_id(),)))
            good = db.backup(now=START)
        damage_beyond_opening(data)
        return good

    good = await asyncio.to_thread(a_backup_then_damage)
    damaged = (data / DB_NAME).read_bytes()

    async with started(data, setup=False) as client:
        health = (await client.get("/api/health")).json()
        assert health["ok"] is True and "quick_check" in health["database_damaged"]  # says what happened
        for method, path in (("GET", "/api/projects"), ("POST", "/api/conversations"), ("GET", "/api/activity"),
                             ("GET", "/api/settings"), ("PUT", "/api/instructions"), ("GET", "/api/providers"),
                             ("POST", "/api/setup"), ("POST", "/api/backups"), ("POST", "/api/backups/full")):
            response = await client.request(method, path, json={"text": "x"} if method in ("POST", "PUT") else None)
            assert (response.status_code, response.json()["code"]) == (503, "database_damaged"), path
        assert (data / DB_NAME).read_bytes() == damaged  # never reset, never written
        assert not (data / "AGENTS.md").exists()
        assert [b["id"] for b in (await client.get("/api/backups")).json()["backups"]] == [f"daily/{good.name}"]

        response = await client.post("/api/backups/restore", json={"generation": f"daily/{good.name}"})
        assert response.status_code == 200, response.text
        assert (data / "backups" / response.json()["damaged_copy"] / DB_NAME).read_bytes() == damaged
        assert "database_damaged" not in (await client.get("/api/health")).json()
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Good"}
        assert (await client.post("/api/setup", json={"openrouter_key": "sk-or-test-not-a-real-key"})).status_code == 200
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        assert (await send(client, conversation))[-1]["status"] == "succeeded"  # the harness runs
        restored = client.state["db"]
    assert restored.closed
