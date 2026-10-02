"""Default logs carry no prompts, answers, reasoning, keys, names or provider error bodies.

Canary strings go through every path that logs: a successful turn with reasoning
and a title, a provider error with a body, a transport error carrying text, an
unexpected exception, project and conversation names, and instructions. The log
file the app writes must hold none of them, and neither may any record from the
app's own loggers, even at DEBUG.
"""

import asyncio
import logging
import stat

import httpx
import pytest

from backend import logs, runs
from scholia_app import KEY, MockProvider, background_idle, send, started

pytestmark = pytest.mark.asyncio
CANARIES = ["CANARY-PROMPT", "CANARY-ANSWER", "CANARY-REASONING", "CANARY-TITLE", "CANARY-ERROR-BODY",
            "CANARY-TRANSPORT", "CANARY-EXCEPTION", "CANARY-PROJECT", "CANARY-CONVERSATION",
            "CANARY-INSTRUCTIONS", KEY]


async def test_no_canary_reaches_the_log(tmp_path, caplog, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    handler = logs.configure(data)
    caplog.set_level(logging.DEBUG, logger="backend")
    provider = MockProvider(
        (200, {"choices": [{"message": {"content": "CANARY-ANSWER", "reasoning": "CANARY-REASONING"}}],
               "usage": {"cost": 0.001}}),
        (500, {"error": {"message": "CANARY-ERROR-BODY", "metadata": {"raw": "CANARY-ERROR-BODY"}}}),
    )
    provider.title_replies.append(provider.answer("CANARY-TITLE"))

    def transport_error(body):
        raise httpx.ReadError("CANARY-TRANSPORT connection reset")

    def unexpected(body):
        raise ValueError("CANARY-EXCEPTION in a handler")

    provider.replies += [transport_error, unexpected]
    try:
        async with started(data, provider) as client:
            await client.put("/api/instructions", json={"text": "CANARY-INSTRUCTIONS"})
            project = (await client.post("/api/projects", json={"name": "CANARY-PROJECT"})).json()["id"]
            untitled = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
            named = (await client.post("/api/conversations", json={"title": "CANARY-CONVERSATION"})).json()["id"]
            await send(client, untitled, "CANARY-PROMPT one")
            await background_idle(client)
            for _ in range(3):
                await send(client, named, "CANARY-PROMPT again")

            def broken_commit(self, conn, claim, answer, ctx):  # a defect, not a provider failure
                raise ValueError(f"CANARY-EXCEPTION {answer['text']}")

            monkeypatch.setattr(runs.Harness, "_commit_answer", broken_commit)
            unsaved = await send(client, named, "CANARY-PROMPT unsaved")
            assert unsaved[-1]["status"] == "failed" and unsaved[-2]["result_saved"] is False
            monkeypatch.undo()

            def broken_reserve(conn, **kwargs):
                raise ValueError("CANARY-EXCEPTION in admission")

            monkeypatch.setattr(runs.spending, "reserve", broken_reserve)
            assert (await send(client, named, "CANARY-PROMPT last"))[-1]["status"] == "failed"
            monkeypatch.undo()
            await client.delete(f"/api/conversations/{named}")
            await client.delete(f"/api/projects/{project}")
            await asyncio.sleep(0)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    log_file = data / "logs" / "scholia.log"
    text = log_file.read_text()
    assert "model call failed" in text and "turn failed unexpectedly (ValueError at" in text
    assert "the answer could not be saved (ValueError at" in text
    for canary in CANARIES:
        assert canary not in text, canary
        assert not any(canary in record.getMessage() for record in caplog.records), canary
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "logs").stat().st_mode) == 0o700


async def test_log_files_rotate_daily_and_are_kept_fourteen_days(tmp_path):
    handler = logs.PrivateRotatingHandler(tmp_path / "scholia.log")
    try:
        assert (handler.when, handler.backupCount) == ("MIDNIGHT", 14)
    finally:
        handler.close()


async def test_an_existing_log_folder_and_file_are_narrowed_to_owner_only(tmp_path):
    import os
    folder = tmp_path / "logs"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    (folder / "scholia.log").write_text("earlier\n")
    os.chmod(folder / "scholia.log", 0o644)
    handler = logs.configure(tmp_path)
    try:
        logging.getLogger("backend.test").warning("a line")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "scholia.log").stat().st_mode) == 0o600
    os.chmod(folder / "scholia.log", 0o400)  # stricter than owner-only: never broadened
    handler = logs.configure(tmp_path)
    handler.close()
    logging.getLogger().removeHandler(handler)
    assert stat.S_IMODE((folder / "scholia.log").stat().st_mode) == 0o400
