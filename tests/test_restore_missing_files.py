"""After a restore, every content file the database refers to is checked, and the missing ones are reported."""

import asyncio
import json

import pytest

from scholia_app import started

pytestmark = pytest.mark.asyncio


async def test_a_restore_reports_the_referenced_content_files_that_are_missing(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        content = client.state["content"]
        kept = await asyncio.to_thread(content.put, b"a paper that stays", "application/pdf")
        lost = await asyncio.to_thread(content.put, b"a paper whose file is lost", "application/pdf")
        backup = (await client.post("/api/backups")).json()["id"]  # an automatic backup has no content files
        (data / "content" / lost[:2] / lost).unlink()

        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
        assert response.json()["missing_files"] == [
            {"sha256": lost, "size": len(b"a paper whose file is lost"), "media_type": "application/pdf"}]
        [(record,)] = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT data FROM audit_log WHERE event = 'restore'").fetchall())
        assert json.loads(record)["missing_files"] == 1
        assert await asyncio.to_thread(client.state["content"].read, kept) == b"a paper that stays"
