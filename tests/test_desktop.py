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
import pytest

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
        origin, session = url.split("/#session=")
        discovered = data / desktop.SESSION_FILE  # what a native client of this account reads
        assert json.loads(discovered.read_text()) == {"origin": origin, "session": session}
        assert stat.S_IMODE(discovered.stat().st_mode) == 0o600
        headers = {"X-Scholia-Client": "local", "Origin": origin}
        with httpx.Client(base_url=origin, headers=headers, timeout=10) as http:
            assert http.get("/api/health").json()["code"] == "session_required"  # another account's view
            headers["X-Scholia-Session"] = session
            http.headers["X-Scholia-Session"] = session
            assert http.get("/api/health").json()["ok"] is True
            assert http.post("/api/setup", json={"openrouter_key": KEY}).status_code == 200
            conversation = http.post("/api/conversations", json={"title": "t"}).json()["id"]
            seen["conversation"] = conversation

        def stream():
            with httpx.Client(base_url=origin, headers=headers, timeout=30) as http:
                seen["stream"] = http.post(f"/api/conversations/{conversation}/message/stream",
                                           json={"content": "hi"}).text

        threading.Thread(target=stream, daemon=True).start()
        assert call_started.wait(10)
        # The window closes here, with the turn's model call in flight.

    assert desktop.run(data, window, keyring_backend=FakeKeyring(), transport=httpx.MockTransport(provider),
                       listening=register_server) == 0

    assert not [t for t in threading.enumerate() if t.name == "scholia-server"]  # the server stopped
    assert not (data / desktop.SESSION_FILE).exists()  # the session ended with the launch
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


def test_an_existing_data_folder_is_narrowed_to_owner_only(tmp_path):
    data = tmp_path / "data"
    (data / "projects" / "p").mkdir(parents=True)
    for path in (data / "scholia.sqlite3", data / "config.toml", data / "projects" / "p" / "AGENTS.md"):
        path.write_text("x")
        os.chmod(path, 0o644)
    for folder in (data, data / "projects", data / "projects" / "p"):
        os.chmod(folder, 0o755)
    strict = data / "credentials.json"
    strict.write_text("{}")
    os.chmod(strict, 0o400)
    desktop.narrow_tree(data)
    for folder in (data, data / "projects", data / "projects" / "p"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for path in (data / "scholia.sqlite3", data / "config.toml", data / "projects" / "p" / "AGENTS.md"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(strict.stat().st_mode) == 0o400  # never broadened


def test_a_linked_data_folder_is_narrowed_at_its_real_path(tmp_path):
    real = tmp_path / "real"
    (real / "b").mkdir(parents=True)
    (real / "b" / "f").write_text("x")
    os.chmod(real / "b" / "f", 0o644)
    os.symlink(real, tmp_path / "data")  # the data folder is a link the researcher made
    desktop.narrow_tree(tmp_path / "data")
    assert stat.S_IMODE((real / "b" / "f").stat().st_mode) == 0o600


def test_a_folder_that_cannot_be_listed_is_closed_and_the_data_folder_refused(tmp_path):
    data = tmp_path / "data"
    for folder in (data / "projects" / "p", data / "b"):
        folder.mkdir(parents=True)
    os.symlink(tmp_path / "elsewhere.md", data / "projects" / "p" / "AGENTS.md")  # hidden from the walk
    (data / "b" / "f").write_text("x")
    os.chmod(data / "b" / "f", 0o644)
    os.chmod(data / "projects" / "p", 0o333)  # its owner cannot list it
    try:
        with pytest.raises(desktop.UnsafeDataFolderError):
            desktop.narrow_tree(data)
        assert stat.S_IMODE((data / "projects" / "p").stat().st_mode) == 0o300  # closed to others
        assert stat.S_IMODE((data / "b" / "f").stat().st_mode) == 0o600  # everything reachable was narrowed
        assert desktop.run(data, [].append) == 1  # the app does not open it
    finally:
        os.chmod(data / "projects" / "p", 0o700)


@pytest.mark.parametrize("linked", ["config.toml", "credentials.json", "projects/p/AGENTS.md", "projects/p"])
def test_a_data_folder_holding_a_link_is_refused_and_the_link_is_never_followed(tmp_path, linked):
    data, outside = tmp_path / "data", tmp_path / "outside"
    (data / "projects" / "p").mkdir(parents=True)
    outside.mkdir()
    (outside / "target").write_text('[providers.openrouter]\nbase_url = "https://attacker.example/v1"\n')
    os.chmod(outside / "target", 0o666)
    link = data / linked
    if link.exists():
        link.rmdir()
    os.symlink(outside / ("target" if "." in linked else ""), link)
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.narrow_tree(data)
    assert stat.S_IMODE((outside / "target").stat().st_mode) == 0o666  # left alone
    windows = []
    assert desktop.run(data, windows.append) == 1  # the app does not open it
    assert windows == [] and not (data / "scholia.sqlite3").exists()
