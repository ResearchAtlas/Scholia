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


async def test_no_reference_file_canary_reaches_the_log(tmp_path, caplog):
    # S1-14: a good reference file, a damaged one, a merge and a lookup log no file name, title, author,
    # DOI or record text.
    import base64

    from test_reference_import import answer, merge_asks, run_finished
    data = tmp_path / "data"
    data.mkdir()
    handler = logs.configure(data)
    caplog.set_level(logging.DEBUG)
    record = ("TY  - JOUR\nTI  - CANARY-REF-TITLE\nAU  - CANARY-REF-AUTHOR\nDO  - 10.5555/canary.ref\n"
              "AB  - CANARY-REF-ABSTRACT\nER  - \n")
    damaged = "@article{CANARY-REF-KEY, title={CANARY-REF-TITLE unbalanced\n@book{b, title={CANARY-REF-OTHER}}\n"

    async def post(client, project, text, name):
        return await client.post(f"/api/projects/{project}/materials/import-references",
                                 json={"name": name, "data": base64.b64encode(text.encode()).decode()})

    try:
        async with started(data, MockProvider()) as client:
            project = (await client.post("/api/projects", json={"name": "Study"})).json()["id"]
            assert (await post(client, project, record, "CANARY-REF-NAME.ris")).status_code == 201
            assert (await post(client, project, damaged, "CANARY-REF-NAME.bib")).status_code == 201
            assert (await post(client, project, "CANARY-REF-NOT-A-FORMAT", "CANARY-REF-NAME.txt")).status_code == 400
            assert (await post(client, project, record.replace("ER  -", "VL  - 4\nER  -"), "CANARY-REF-NAME.ris")).status_code == 201
            [ask] = await merge_asks(client, project)
            assert (await answer(client, ask, "merge")).status_code == 200
            await run_finished(client, ask["run_id"])
            await background_idle(client)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    log_file = data / "logs" / "scholia.log"
    text = log_file.read_text() if log_file.exists() else ""  # written once something is logged
    for canary in ("CANARY-REF-TITLE", "CANARY-REF-AUTHOR", "CANARY-REF-ABSTRACT", "CANARY-REF-KEY", "CANARY-REF-OTHER",
                   "CANARY-REF-NAME", "CANARY-REF-NOT-A-FORMAT", "canary.ref"):
        assert canary not in text, canary
        assert not any(canary in record.getMessage() for record in caplog.records), canary


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
    (folder / "scholia.log.2026-10-01").write_text("older\n")  # a dated log from an earlier rotation
    os.chmod(folder / "scholia.log.2026-10-01", 0o644)
    handler = logs.configure(tmp_path)
    try:
        logging.getLogger("backend.test").warning("a line")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "scholia.log").stat().st_mode) == 0o600
    assert stat.S_IMODE((folder / "scholia.log.2026-10-01").stat().st_mode) == 0o600
    os.chmod(folder / "scholia.log", 0o400)  # stricter than owner-only: never broadened
    handler = logs.configure(tmp_path)
    handler.close()
    logging.getLogger().removeHandler(handler)
    assert stat.S_IMODE((folder / "scholia.log").stat().st_mode) == 0o400


async def test_the_log_never_writes_through_a_link(tmp_path):
    import os
    import pytest
    outside = tmp_path / "outside.txt"
    outside.write_text("")
    os.chmod(outside, 0o644)
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    os.symlink(outside, data / "logs" / "scholia.log")
    handler = logs.configure(data)
    try:
        logging.raiseExceptions, raised = False, logging.raiseExceptions  # the failed write is reported, not raised
        logging.getLogger("backend.test").warning("a line")
    finally:
        logging.raiseExceptions = raised
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert outside.read_text() == "" and stat.S_IMODE(outside.stat().st_mode) == 0o644
    linked = tmp_path / "linked"
    (linked).mkdir()
    os.symlink(tmp_path / "outside-logs", linked / "logs", target_is_directory=True)
    (tmp_path / "outside-logs").mkdir()
    with pytest.raises(NotADirectoryError):
        logs.configure(linked)


async def test_no_search_canary_reaches_the_log(tmp_path, caplog, monkeypatch):
    """S1-17: indexing, search and a failed index cleanup log no passage text, title or query."""
    from backend.search_index import SearchIndex
    from test_materials import added, project_of
    from test_search import app, find, idle, paper

    data = tmp_path / "data"
    data.mkdir()
    handler = logs.configure(data)
    caplog.set_level(logging.DEBUG, logger="backend")
    canaries = ["CANARY-PASSAGE", "CANARY-TITLE-SEARCH", "CANARY-QUERY", "CANARY-CLEANUP", "金丝雀段落"]
    try:
        async with app(tmp_path, data=data) as client:
            project = await project_of(client)
            [material] = (await added(client, project, paper("CANARY-TITLE-SEARCH", "CANARY-PASSAGE 金丝雀段落")))[
                "materials"]
            await idle(client, project)
            client.remote.failing = True
            assert (await find(client, project, "CANARY-QUERY"))["reason"] == "request_failed"
            monkeypatch.setattr(SearchIndex, "_verify", lambda self, rowid: (_ for _ in ()).throw(
                RuntimeError("CANARY-CLEANUP CANARY-PASSAGE")))
            assert (await client.delete(f"/api/materials/{material['id']}")).status_code == 200
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    text = (data / "logs" / "scholia.log").read_text()
    assert "the search index's cleanup after a deletion failed (RuntimeError)" in text
    for canary in canaries:
        assert canary not in text, canary
        assert not any(canary in record.getMessage() for record in caplog.records), canary


async def test_no_cited_answer_canary_reaches_the_log(tmp_path, caplog):
    """S1-19b: the router, the evidence, the answer, its citations and the judge, failing too, log no
    question, passage, answer, sentence, verdict or provider body."""
    from backend import budget_router
    from cited import Provider, cited, conversation_in, with_papers
    from test_search import paper

    data = tmp_path / "data"
    data.mkdir()
    handler = logs.configure(data)
    caplog.set_level(logging.DEBUG, logger="backend")
    canaries = ["CANARY-QUESTION", "CANARY-EVIDENCE", "CANARY-CITED", "CANARY-VERDICT", "CANARY-JUDGE-BODY",
                "CANARY-PAPER", "金丝雀证据"]
    replies = iter([(500, {"error": {"message": "CANARY-JUDGE-BODY"}}), "1: supported CANARY-VERDICT"])
    provider = Provider(answer_text="CANARY-CITED earnings rise [S1.1].", verdicts=lambda body: next(replies))
    try:
        async with cited(tmp_path, provider) as client:
            current = (await client.get("/api/settings")).json()
            await client.put("/api/settings", json={"hash": current["hash"], "updates": {
                "models.router": sorted(budget_router.RECOMMENDED)[0]}})
            project, _ = await with_papers(client, paper("CANARY-PAPER", "CANARY-EVIDENCE earnings 金丝雀证据"))
            conversation = await conversation_in(client, project)
            for _ in range(2):  # the judge fails, then answers
                await send(client, conversation, "CANARY-QUESTION earnings")
            await background_idle(client)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    text = (data / "logs" / "scholia.log").read_text()
    for canary in canaries:
        assert canary not in text, canary
        assert not any(canary in record.getMessage() for record in caplog.records), canary
