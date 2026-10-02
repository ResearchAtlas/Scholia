"""The desktop entry: the data folder's lock first, then the server, then a deliberate shutdown.

The window is a stand-in that talks to the real server over loopback (registered
with the test network block) and returns when the test closes it.
"""

import json
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import httpx

from backend import desktop
from backend.db import Database
from network_guard import allow_subprocess, register_server
from scholia_app import KEY, FakeKeyring

ROOT = Path(__file__).resolve().parents[1]


def test_the_lock_admits_one_holder_and_ignores_what_the_file_says(tmp_path):
    data = tmp_path / "Scholia 数据"
    first = desktop.take_lock(data)
    assert first is not None
    assert desktop.take_lock(data) is None
    assert stat.S_IMODE((data / desktop.LOCK_FILE).stat().st_mode) == 0o600
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    os.close(first)
    (data / desktop.LOCK_FILE).write_text("12345\n")  # a stale process id is not authority
    second = desktop.take_lock(data)
    assert second is not None
    os.close(second)


def test_a_killed_owner_releases_the_lock(tmp_path):
    data = tmp_path / "data"
    holder = ("import sys, time\nfrom backend.desktop import take_lock\n"
              "assert take_lock(sys.argv[1]) is not None\nprint('held', flush=True)\ntime.sleep(60)\n")
    with allow_subprocess(sys.executable):  # the child only takes a file lock
        child = subprocess.Popen([sys.executable, "-c", holder, str(data)], cwd=ROOT, stdout=subprocess.PIPE)
        try:
            assert child.stdout.readline().strip() == b"held"
            assert desktop.take_lock(data) is None
            child.send_signal(signal.SIGKILL)
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
    fd = desktop.take_lock(data)
    assert fd is not None
    os.close(fd)


def test_a_second_instance_stops_before_touching_the_data_folder(tmp_path):
    data = tmp_path / "data"
    held = desktop.take_lock(data)
    (data / "logs").mkdir(mode=0o700)
    (data / "logs" / "scholia.log").write_text("the first instance's log\n")
    windows = []
    try:
        assert desktop.run(data, windows.append) == 1
    finally:
        os.close(held)
    assert windows == [None]  # the "already open" window, not the app
    assert (data / "logs" / "scholia.log").read_text() == "the first instance's log\n"
    assert sorted(p.name for p in data.iterdir()) == ["logs", desktop.LOCK_FILE]  # no database, nothing else


def test_closing_the_window_interrupts_running_work_and_stops_the_server(tmp_path):
    data = tmp_path / "data"
    call_started = threading.Event()

    async def provider(request):
        call_started.set()
        import asyncio
        await asyncio.sleep(3600)  # a model call still waiting when the window closes

    seen = {}

    def window(url):
        seen["url"] = url
        headers = {"X-Scholia-Client": "local", "Origin": url}
        with httpx.Client(base_url=url, headers=headers, timeout=10) as http:
            assert http.get("/api/health").json()["ok"] is True
            assert http.post("/api/setup", json={"openrouter_key": KEY}).status_code == 200
            conversation = http.post("/api/conversations", json={"title": "t"}).json()["id"]
            seen["conversation"] = conversation

        def stream():
            with httpx.Client(base_url=url, headers=headers, timeout=30) as http:
                seen["stream"] = http.post(f"/api/conversations/{conversation}/message/stream",
                                           json={"content": "hi"}).text

        threading.Thread(target=stream, daemon=True).start()
        assert call_started.wait(10)
        # The window closes here, with the turn's model call in flight.

    assert desktop.run(data, window, keyring_backend=FakeKeyring(), transport=httpx.MockTransport(provider),
                       listening=register_server) == 0

    assert not [t for t in threading.enumerate() if t.name == "scholia-server"]  # the server stopped
    with Database(data) as db:
        [(status, cancel_reason, cost)] = db.read(lambda conn: conn.execute(
            "SELECT status, cancel_reason, settled_cost_usd FROM runs WHERE kind = 'turn'").fetchall())
        assert (status, cancel_reason) == ("interrupted", None) and cost > 0
        assert db.read(lambda conn: conn.execute("SELECT status, basis FROM budget_reservations").fetchall()) == [
            ("settled", "estimated")]
    finale = [json.loads(line[6:]) for line in seen.get("stream", "").splitlines() if line.startswith("data: ")]
    assert not finale or finale[-1]["status"] == "interrupted"
    log = data / "logs" / "scholia.log"
    assert log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o600
    fd = desktop.take_lock(data)  # released at exit
    assert fd is not None
    os.close(fd)


def test_an_existing_data_folder_is_narrowed_to_owner_only_without_following_links(tmp_path):
    data, outside = tmp_path / "data", tmp_path / "outside.txt"
    (data / "projects" / "p").mkdir(parents=True)
    for path in (data / "scholia.sqlite3", data / "config.toml", data / "projects" / "p" / "AGENTS.md", outside):
        path.write_text("x")
        os.chmod(path, 0o644)
    for folder in (data, data / "projects", data / "projects" / "p"):
        os.chmod(folder, 0o755)
    strict = data / "credentials.json"
    strict.write_text("{}")
    os.chmod(strict, 0o400)
    os.symlink(outside, data / "link.txt")
    desktop.narrow_tree(data)
    for folder in (data, data / "projects", data / "projects" / "p"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for path in (data / "scholia.sqlite3", data / "config.toml", data / "projects" / "p" / "AGENTS.md"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(strict.stat().st_mode) == 0o400  # never broadened
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644  # a link's target is left alone
