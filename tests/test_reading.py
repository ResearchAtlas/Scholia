"""Readings and page images in a child process (backend/reading.py; ticket 60 B1, Harold's decisions of
2026-10-09): the memory and time ceilings, the child's end in every case, its failures, the bounded
results it sends, the same results as in process, and what the child may do. With the real child,
and a test-owned one (tests/reading_stub.py) held or made to fail. Lifecycle cases first; synthetic
files only. The S1-13 lifecycle tests in test_materials.py hold their readings in the test-owned
child too (cancellation, deletion, the 30-minute limit, a restart, two readings at once)."""

import ast
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

import synthetic_materials as synthetic
from backend import extraction, reading
from network_guard import NetworkBlocked, allow_command
from scholia_app import run_finished, started
from test_extraction import _raw_pdf
from test_materials import PDF, Held, added, ended, gone, hold_extraction, project_of, rows, settled

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = ("notes.md", synthetic.paper_markdown(arxiv=""))
WRITTEN = ("extractions", "passages", "index_queue")


def stored(folder, data):
    """data written to folder as the content store keeps a file: (its path, its SHA-256)."""
    sha256 = hashlib.sha256(data).hexdigest()
    path = Path(folder) / sha256
    path.write_bytes(data)
    return path, sha256


def latex_marks(count):
    """LaTeX source with about count marks (each \\emph{x} is two), as dense mathematics has."""
    return ("\\documentclass{article}\\begin{document}" + "\\emph{a} " * (count // 2) + "\\end{document}").encode()


def latex_run(mib):
    """LaTeX source with one run of plain text, no mark in it, mib MiB long: pylatexenc's tokenizer
    takes time growing with the run's square (0.25 MiB about 1.4 s, 1 MiB about 21 s)."""
    return ("\\documentclass{article}\\begin{document}" + "a" * int(mib * 2**20) + "\\end{document}").encode()


async def nothing_written(client):
    for table in WRITTEN:
        assert await rows(client, f"SELECT count(*) FROM {table}") == [(0,)], table
    return True


def real_child(monkeypatch):
    """Back to the real child, after a test-owned one."""
    monkeypatch.setattr(reading, "command", lambda: [sys.executable, "-m", "backend.reading"])



# The memory ceiling


@pytest.mark.asyncio
async def test_a_reading_past_its_memory_ceiling_is_killed_fails_memory_limit_writes_nothing_and_retry_reads_it(
        tmp_path, monkeypatch):
    monkeypatch.setattr(reading, "READING_CEILING", 1024 * 1024)  # below any child's footprint
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "memory_limit"}, True)
        assert await nothing_written(client) and gone()
        [failed] = await settled(client, project)
        assert (failed["state"], failed["reason"], failed["readable"]) == ("needs_attention", "memory_limit", True)
        monkeypatch.undo()
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [ready] = await settled(client, project)
        assert ready["state"] == "ready"


@pytest.mark.asyncio
async def test_dense_latex_passing_the_ceiling_while_it_is_parsed_is_killed_there(tmp_path):
    path, sha256 = stored(tmp_path, latex_marks(200_000))
    stats = {}
    with pytest.raises(extraction.Unreadable) as unreadable:
        await asyncio.to_thread(reading.read, path, sha256, extraction.LATEX, ceiling=64 * 2**20, stats=stats)
    assert unreadable.value.code == "memory_limit" and gone()
    assert 64 < stats["peak_mib"] < 64 + 300  # stopped near the ceiling, not at the parse's own peak


@pytest.mark.asyncio
async def test_a_peak_that_came_and_went_between_two_looks_still_fails_the_reading(tmp_path, monkeypatch, reading_stub):
    reading_stub("spike", 300)  # 300 MiB touched and freed, then a whole reading
    real = reading._peak
    monkeypatch.setattr(reading, "_peak", lambda pid: real(pid) if reading._exited(pid) else 0)  # no look while it runs
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(extraction.Unreadable) as unreadable:
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN, ceiling=200 * 2**20)
    assert unreadable.value.code == "memory_limit" and gone()  # the kernel's record of its peak, read before reaping


@pytest.mark.asyncio
async def test_a_child_that_passed_its_ceiling_and_then_reports_an_unreadable_file_fails_memory_limit(
        tmp_path, monkeypatch, reading_stub):
    reading_stub("spike-fail", 300)  # 300 MiB touched and freed, then an unreadable file reported
    monkeypatch.setattr(reading, "WATCH_SECONDS", 5.0)  # no look between its frames but at them
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(extraction.Unreadable) as unreadable:
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN, ceiling=200 * 2**20)
    assert unreadable.value.code == "memory_limit" and gone()  # the peak decides, whatever came last


@pytest.mark.asyncio
async def test_a_child_that_passed_its_ceiling_and_then_sends_a_frame_past_its_bound_fails_memory_limit(
        tmp_path, monkeypatch, reading_stub):
    reading_stub("spike-frame", 300)  # 300 MiB touched and freed, then a header past MAX_FRAME
    monkeypatch.setattr(reading, "WATCH_SECONDS", 5.0)
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(extraction.Unreadable) as unreadable:
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN, ceiling=200 * 2**20)
    assert unreadable.value.code == "memory_limit" and gone()


@pytest.mark.asyncio
async def test_a_footprint_that_cannot_be_read_as_a_wrong_frame_is_weighed_fails_closed(tmp_path, monkeypatch,
                                                                                         reading_stub):
    reading_stub("frame", "not-json")
    failed, real_take, real_peak = [], reading._Reading.take, reading._peak

    def take(self, body):
        try:
            return real_take(self, body)
        except Exception:
            failed.append(True)
            raise

    monkeypatch.setattr(reading._Reading, "take", take)
    monkeypatch.setattr(reading, "_peak", lambda pid: None if failed else real_peak(pid))
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(reading.ChildError):
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
    assert failed and gone()


@pytest.mark.asyncio
async def test_a_selector_that_cannot_be_made_starts_no_child_and_fails_internal(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a child was started")

    def no_selector():
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(reading.subprocess, "Popen", never)
    monkeypatch.setattr(reading.selectors, "DefaultSelector", no_selector)
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(reading.ChildError):
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
    assert gone()


@pytest.mark.asyncio
async def test_a_reading_stopped_before_its_child_starts_starts_none(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a child was started")

    class Stopped(Exception):
        pass

    def stop():
        raise Stopped()

    monkeypatch.setattr(reading.subprocess, "Popen", never)
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(Stopped):
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN, stop)
    assert gone()


@pytest.mark.asyncio
async def test_a_footprint_that_cannot_be_read_ends_the_child_and_fails_internal(tmp_path, monkeypatch, reading_stub):
    reached, _ = hold_extraction(reading_stub, tmp_path)
    real = reading._peak
    monkeypatch.setattr(reading, "_peak", lambda pid: None if reached.is_set() else real(pid))
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "internal"}, True)
        assert await nothing_written(client) and await ended(*reached.held())


@pytest.mark.asyncio
async def test_two_readings_at_once_each_in_its_child_and_one_killed_leaves_the_other_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(reading, "READING_CEILING", 64 * 2**20)  # the dense source passes it, the notes do not
    started_children, real = [], reading._run

    def run(request, *args):
        started_children.append(request["kind"])
        return real(request, *args)

    monkeypatch.setattr(reading, "_run", run)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        both = (await added(client, project, ("dense.tex", latex_marks(200_000)), MARKDOWN))["materials"]
        dense, notes = [await run_finished(client, paper["run_id"]) for paper in both]
        assert (dense["status"], dense["result"]) == ("failed", {"reason": "memory_limit"})
        assert notes["status"] == "succeeded"
        assert sorted(started_children) == sorted([extraction.LATEX, extraction.MARKDOWN])
        papers = {m["version"]["media_type"]: m for m in await settled(client, project)}
        assert (papers[extraction.MARKDOWN]["state"], papers[extraction.LATEX]["reason"]) == ("ready", "memory_limit")
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)] and gone()
        # A file another project has read already is shared: no child starts for it.
        [shared] = (await added(client, await project_of(client, "Other"), MARKDOWN))["materials"]
        assert shared["run_id"] is None and len(started_children) == 2


# The time ceiling


@pytest.mark.asyncio
async def test_a_child_that_sends_nothing_past_the_step_ceiling_is_killed_and_fails_step_limit(
        tmp_path, monkeypatch, reading_stub):
    reading_stub("stall")
    monkeypatch.setattr(reading, "STEP_SECONDS", 0.5)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, MARKDOWN))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "step_limit"}, True)
        assert await nothing_written(client) and gone()
        [failed] = await settled(client, project)
        assert (failed["state"], failed["reason"]) == ("needs_attention", "step_limit")


def test_the_time_ceilings_text_names_its_length():
    catalogs = {name: json.loads((ROOT / "frontend/src/i18n" / f"{name}.json").read_text()) for name in ("en", "zh-CN")}
    assert reading.STEP_SECONDS == 60  # "a minute" in each catalog: change both with it
    assert all("a minute" in catalogs["en"][f"{prefix}.step_limit"] for prefix in ("library.reason", "errors"))
    assert all("1 分钟" in catalogs["zh-CN"][f"{prefix}.step_limit"] for prefix in ("library.reason", "errors"))


@pytest.mark.asyncio
async def test_pylatexenc_on_a_long_run_of_plain_text_is_stopped_at_the_step_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(reading, "STEP_SECONDS", 1.0)
    path, sha256 = stored(tmp_path, latex_run(0.5))  # about 5 s in one step
    started_at = time.monotonic()
    with pytest.raises(extraction.Unreadable) as unreadable:
        await asyncio.to_thread(reading.read, path, sha256, extraction.LATEX)
    assert unreadable.value.code == "step_limit" and time.monotonic() - started_at < 3.5 and gone()


@pytest.mark.asyncio
async def test_a_reading_that_reports_as_it_works_is_not_stopped_however_long_it_takes(
        tmp_path, monkeypatch, reading_stub):
    reached, go = hold_extraction(reading_stub, tmp_path)  # it calls stop() as it waits: the child reports each second
    monkeypatch.setattr(reading, "STEP_SECONDS", 2.5)
    threading.Timer(5.0, go.set).start()
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    read = await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
    assert read == extraction.extract(MARKDOWN[1], extraction.MARKDOWN) and gone(*reached.held())


@pytest.mark.asyncio
async def test_cancel_ends_a_child_in_the_middle_of_one_long_step_at_once(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("run.tex", latex_run(1))))["materials"]  # about 21 s in one step
        while not reading.LIVE:
            await asyncio.sleep(0.01)
        pids = set(reading.LIVE)
        await asyncio.sleep(0.5)
        asked = time.monotonic()
        assert (await client.post(f"/api/runs/{paper['run_id']}/cancel")).json()["status"] == "cancelled"
        assert time.monotonic() - asked < 2
        assert await nothing_written(client) and gone(*pids)


# The child ends in every case


@pytest.mark.asyncio
async def test_a_shutdown_ends_the_child_and_the_reading_starts_again_at_the_next_launch_once(
        tmp_path, monkeypatch, reading_stub):
    data = tmp_path / "data"
    reached, _ = hold_extraction(reading_stub, tmp_path)
    async with started(data) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        client.state["db"].on_damage()  # as a backup's check finding the database damaged: the harness shuts down
        assert await ended(*reached.held())
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", paper["run_id"]) == [("running",)]
    real_child(monkeypatch)
    async with started(data, setup=False) as client:
        assert (await run_finished(client, paper["run_id"]))["status"] == "succeeded"
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]


@pytest.mark.asyncio
async def test_a_shutdown_ends_a_page_images_child_and_its_request_is_refused(tmp_path, reading_stub):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, PDF)
        [paper] = await settled(client, project)
        reached, _ = hold_extraction(reading_stub, tmp_path)
        page = asyncio.ensure_future(client.get(f"/api/material-versions/{paper['version']['id']}/pages/1"))
        await asyncio.to_thread(reached.wait, 10)
        await client.state["harness"].shutdown()
        response = await page
        assert (response.status_code, response.json()["code"]) == (503, "shutting_down")
        assert gone(*reached.held())


@pytest.mark.asyncio
async def test_a_child_ends_within_two_seconds_when_the_app_is_killed(tmp_path):
    held = tmp_path / "held"
    held.mkdir()
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    stub = [sys.executable, str(ROOT / "tests" / "reading_stub.py"), "hold", str(held)]
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from backend import extraction, reading; "
            "reading.command = lambda: sys.argv[2:6]; reading.read(sys.argv[6], sys.argv[7], extraction.MARKDOWN)")
    command = [sys.executable, "-c", code, str(ROOT), *stub, str(path), sha256]
    with allow_command(*command):
        app = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert await asyncio.to_thread(Held(held).wait, 10)
        [child] = Held(held).held()
        app.kill()  # SIGKILL: no cleanup of its own runs
        app.wait()
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "the child outlived the app by 2 s"
            await asyncio.sleep(0.01)
    finally:
        if app.poll() is None:
            app.kill()
            app.wait()


# Child failures: each fails the reading cleanly, writes nothing, and Retry reads the file


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "reason"), [
    (("exit", 1), "unreadable_file"),
    (("signal", "SIGSEGV"), "unreadable_file"),
    (("signal", "SIGKILL"), "unreadable_file"),
    (("frame", "oversized"), "unreadable_file"),
    (("frame", "not-json"), "unreadable_file"),
    (("frame", "wrong-fields"), "unreadable_file"),
    (("frame", "wrong-types"), "unreadable_file"),
    (("frame", "bad-boxes"), "unreadable_file"),
    (("frame", "two-keys"), "unreadable_file"),
    (("frame", "unknown-error"), "unreadable_file"),
    (("frame", "unknown-kind"), "unreadable_file"),  # each as the database would refuse it at the commit
    (("frame", "surrogate"), "unreadable_file"),
    (("frame", "not-finite"), "unreadable_file"),
    (("frame", "negative"), "unreadable_file"),
    (("frame", "past-int64"), "unreadable_file"),
    (("frame", "deep-path"), "unreadable_file"),  # a section path past what any reading makes
    (("frame", "empty-heading"), "unreadable_file"),
    (("frame", "long-heading"), "unreadable_file"),
    (("frame", "nested"), "unreadable_file"),  # nested past the JSON parser's depth
    (("frame", "list-kind"), "unreadable_file"),  # a list where a name is due
    (("frame", "list-error"), "unreadable_file"),
    (("frame", "bigint-rect"), "unreadable_file"),  # past a float: any value the checks cannot take
    (("frame", "no-page"), "unreadable_file"),  # a page image's report, not a reading's
    (("after-done", "beat"), "unreadable_file"),
    (("after-done", "exit"), "unreadable_file"),
    (("version",), "internal"),
    (("no-start",), "internal"),
])
async def test_a_child_that_fails_fails_its_reading_cleanly_and_retry_reads_it(
        tmp_path, monkeypatch, reading_stub, mode, reason):
    reading_stub(*mode)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": reason}, True)
        assert await nothing_written(client) and gone()
        real_child(monkeypatch)
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        assert (await settled(client, project))[0]["state"] == "ready" and gone()


@pytest.mark.asyncio
async def test_a_missing_or_changed_file_and_a_child_that_cannot_start(tmp_path, monkeypatch):
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with pytest.raises(FileNotFoundError):
        await asyncio.to_thread(reading.read, tmp_path / ("0" * 64), "0" * 64, extraction.MARKDOWN)
    path.write_bytes(b"# Changed\n")
    with pytest.raises(FileNotFoundError):
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
    missing = [str(tmp_path / "no-such-program")]
    monkeypatch.setattr(reading, "command", lambda: missing)
    with allow_command(*missing), pytest.raises(reading.ChildError):
        await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
    assert gone()


@pytest.mark.asyncio
async def test_a_page_images_child_past_its_ceiling_or_crashing_is_a_file_that_cannot_be_read(
        tmp_path, monkeypatch, reading_stub):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, PDF)
        [paper] = await settled(client, project)
        url = f"/api/material-versions/{paper['version']['id']}/pages"
        monkeypatch.setattr(reading, "RENDER_CEILING", 1024 * 1024)
        assert (await client.get(f"{url}/1")).json()["code"] == "file_missing"
        monkeypatch.setattr(reading, "RENDER_CEILING", 512 * 2**20)
        for mode in (("signal", "SIGSEGV"), ("no-start",), ("version",), ("frame", "list-error")):
            reading_stub(*mode)
            assert (await client.get(f"{url}/1")).json()["code"] == "file_missing", mode
        real_child(monkeypatch)
        assert (await client.get(f"{url}/1")).status_code == 200
        assert (await client.get(f"{url}/9")).status_code == 404
        assert gone()


# Results come back bounded


@pytest.mark.asyncio
async def test_a_frame_past_its_bound_is_refused_from_its_header(tmp_path, reading_stub):
    reading_stub("frame", "oversized")  # a header of 4 GiB, then 64 MiB the parent never takes
    path, sha256 = stored(tmp_path, MARKDOWN[1])

    def measured():
        tracemalloc.start()
        try:
            reading.read(path, sha256, extraction.MARKDOWN)
        except extraction.Unreadable as unreadable:
            return unreadable.code, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    code, peak = await asyncio.to_thread(measured)
    assert code == "unreadable_file" and gone()
    assert peak < 2 * 2**20, f"{peak / 2**20:.1f} MiB"


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["MAX_TEXT_CHARS", "MAX_BUILT_CHARS", "MAX_RECTS", "MAX_PASSAGES"])
async def test_a_reading_past_what_its_bounds_allow_is_counted_as_it_arrives_and_refused(tmp_path, monkeypatch, bound):
    monkeypatch.setattr(reading if bound == "MAX_PASSAGES" else extraction, bound, 1)  # here only, not in the child
    path, sha256 = stored(tmp_path, synthetic.paper_pdf())
    with pytest.raises(extraction.Unreadable) as unreadable:
        await asyncio.to_thread(reading.read, path, sha256, extraction.PDF)
    assert unreadable.value.code == "unreadable_file" and gone()


@pytest.mark.asyncio
async def test_the_largest_passage_a_reading_makes_fits_a_frame():
    astral = "\U0001d4b3"  # twelve bytes in a frame
    path = [astral * extraction.MAX_HEADING_CHARS] * 10  # a DOCX heading's levels, 0 to 9
    rects = [[-1.2345678901234567e+300] * 4] * extraction.MAX_PASSAGE  # one a character, each its own line
    passage = [astral * 10, astral * extraction.MAX_PASSAGE, 2**31, path, 2**31, 2**31,
               {"rects": rects, "ocr": {"confidence": 0.123456789}}]
    assert len(json.dumps({"passage": passage}, separators=(",", ":"))) < reading.MAX_FRAME


# The same results as in process


def materials():
    return {"pdf": (synthetic.paper_pdf(), extraction.PDF),
            "rotated": (synthetic.paper_pdf(rotation=90), extraction.PDF),
            "scanned": (synthetic.paper_pdf(scanned=2), extraction.PDF),
            "letter": (synthetic.scanned_letter(), extraction.PDF),
            "docx": (synthetic.paper_docx(), extraction.DOCX), "html": (synthetic.paper_html(), extraction.HTML),
            "markdown": (synthetic.paper_markdown(), extraction.MARKDOWN),
            "latex": (synthetic.paper_latex(), extraction.LATEX),
            "table": (synthetic.docx([("Heading1", "Results"), (None, "Before the table.")], tables=[synthetic.TABLE]),
                      extraction.DOCX)}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(materials()))
async def test_every_synthetic_material_reads_in_its_child_as_in_process(tmp_path, name):
    data, kind = materials()[name]
    path, sha256 = stored(tmp_path, data)
    assert await asyncio.to_thread(reading.read, path, sha256, kind) == extraction.extract(data, kind)
    assert gone()


@pytest.mark.asyncio
async def test_page_images_render_in_their_child_byte_for_byte_as_in_process(tmp_path):
    tall = _raw_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1 1000000] >>"])  # at the side bound
    for data, number, scale in [(synthetic.paper_pdf(), 1, 0.5), (synthetic.paper_pdf(), 2, 1.5),
                                (synthetic.paper_pdf(), 1, 3.0), (synthetic.paper_pdf(rotation=90), 1, 2.0),
                                (synthetic.paper_pdf(scanned=1), 1, 3.0), (tall, 1, 3.0)]:
        path, sha256 = stored(tmp_path, data)
        image = await asyncio.to_thread(reading.render, path, sha256, number, scale)
        assert image == extraction.render_page(data, number, scale)
    assert gone()


# What the child may do


@pytest.mark.asyncio
async def test_the_child_refuses_connections_lookups_and_processes_and_still_reads(tmp_path, reading_stub):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    try:
        reading_stub("probe", tmp_path, listener.getsockname()[1])
        path, sha256 = stored(tmp_path, MARKDOWN[1])
        read = await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
        assert read == extraction.extract(MARKDOWN[1], extraction.MARKDOWN)
        assert json.loads((tmp_path / "probe").read_text()) == {"connect": "refused", "lookup": "refused",
                                                                 "process": "refused"}
        with pytest.raises(BlockingIOError):
            listener.accept()  # nothing reached it
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_a_crashing_child_that_printed_a_file_name_and_text_leaves_neither_in_the_log(
        tmp_path, reading_stub, caplog, capfd):
    import logging
    caplog.set_level(logging.DEBUG)
    secret = "Participant-Nine-Canary"
    reading_stub("canary", secret)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, (f"{secret}.md", f"# {secret}\n\n{secret} said this.".encode())))[
            "materials"]
        assert (await run_finished(client, paper["run_id"]))["result"] == {"reason": "unreadable_file"}
    captured = capfd.readouterr()
    assert secret not in caplog.text + captured.out + captured.err and gone()


@pytest.mark.asyncio
async def test_a_reading_writes_no_file(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    path, sha256 = stored(folder, synthetic.paper_pdf(scanned=1))
    before = sorted(folder.rglob("*"))
    await asyncio.to_thread(reading.read, path, sha256, extraction.PDF)
    await asyncio.to_thread(reading.render, path, sha256, 1, 2.0)
    assert sorted(folder.rglob("*")) == before


def test_the_child_imports_no_database_server_or_window():
    """The reading module, and every parser it drives on a synthetic file (OCR aside: pyobjc's Quartz
    bindings import AppKit, which the app's own process did for OCR before), load none of them."""
    code = ("import sys; sys.path.insert(0, 'tests'); import backend.reading, backend.extraction as x; "
            "import synthetic_materials as s; "
            "[x.extract(d, k) for d, k in [(s.paper_pdf(), x.PDF), (s.paper_docx(), x.DOCX), (s.paper_html(), x.HTML), "
            "(s.paper_markdown(), x.MARKDOWN), (s.paper_latex(), x.LATEX)]]; x.render_page(s.paper_pdf(), 1); "
            "print(' '.join(sorted(sys.modules)))")
    command = [sys.executable, "-c", code]
    with allow_command(*command):
        loaded = set(subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout.split())
    forbidden = {"sqlite3", "_sqlite3", "apsw", "fastapi", "starlette", "uvicorn", "httpx", "webview", "AppKit",
                 "backend.db", "backend.app", "backend.desktop", "backend.runs", "backend.materials"}
    assert "pypdfium2" in loaded and "pylatexenc" in loaded and not loaded & forbidden


def test_only_the_child_parses_a_material():
    """No backend module but backend/reading.py's child side calls extraction.extract or render_page,
    under whatever name it imports backend.extraction."""
    callers = set()
    for path in sorted((ROOT / "backend").rglob("*.py")):
        if path.name == "extraction.py":
            continue
        tree = ast.parse(path.read_text())
        names = {"extraction"}  # the module, as each import names it
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.asname for a in node.names if a.name == "backend.extraction" and a.asname}
            elif isinstance(node, ast.ImportFrom) and node.module == "backend":
                names |= {a.asname or a.name for a in node.names if a.name == "extraction"}
            elif isinstance(node, ast.ImportFrom) and node.module == "backend.extraction" \
                    and {a.name for a in node.names} & {"extract", "render_page"}:
                callers.add(path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("extract", "render_page") \
                    and (isinstance(node.value, ast.Name) and node.value.id in names
                         or ast.unparse(node.value) == "backend.extraction"):
                callers.add(path.name)
    assert callers == {"reading.py"}


def test_only_the_reading_childs_whole_command_is_allowed():
    for command in ([sys.executable, "-c", "pass"], [sys.executable, "-m", "backend.reading", "--other"],
                    [sys.executable, "-m", "backend.app"]):
        with pytest.raises(NetworkBlocked):
            subprocess.run(command, cwd=ROOT)


@pytest.mark.asyncio
async def test_the_packaged_entry_starts_a_reading_before_anything_else(tmp_path, monkeypatch):
    """tools/app.py --read-material, as the frozen app runs it (here from source, through runpy)."""
    command = [sys.executable, "-c", "import runpy, sys; sys.argv = ['Scholia', '--read-material'];"
               " runpy.run_path('tools/app.py', run_name='__main__')"]
    monkeypatch.setattr(reading, "command", lambda: command)
    path, sha256 = stored(tmp_path, MARKDOWN[1])
    with allow_command(*command):
        read = await asyncio.to_thread(reading.read, path, sha256, extraction.MARKDOWN)
    assert read == extraction.extract(MARKDOWN[1], extraction.MARKDOWN) and gone()
