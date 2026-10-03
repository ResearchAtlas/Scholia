"""The desktop entry: the data folder's lock first, then the server, then a deliberate shutdown.

The window is a stand-in that talks to the real server over loopback (registered
with the test network block) and returns when the test closes it.
"""

import fcntl
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


def test_a_data_folder_that_is_itself_a_link_is_refused(tmp_path):
    real = tmp_path / "real"
    (real / "b").mkdir(parents=True)
    (real / "b" / "f").write_text("x")
    os.chmod(real / "b" / "f", 0o644)
    os.symlink(real, tmp_path / "data")  # its target could be swapped after any check
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.narrow_tree(tmp_path / "data")
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.take_lock(tmp_path / "data")
    assert desktop.run(tmp_path / "data", [].append) == 1
    assert not (real / desktop.LOCK_FILE).exists()  # nothing opened through it


def test_a_folder_that_cannot_be_listed_is_closed_and_the_data_folder_refused(tmp_path):
    data = tmp_path / "data"
    for folder in (data / "projects" / "p", data / "b"):
        folder.mkdir(parents=True)
    os.symlink(tmp_path / "elsewhere.md", data / "projects" / "p" / "AGENTS.md")  # hidden from the walk
    (data / "b" / "f").write_text("x")
    os.chmod(data / "b" / "f", 0o644)
    os.chmod(data / "projects" / "p", 0o311)  # its owner cannot list it
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
    seen = []
    assert desktop.run(data, _health(seen), listening=register_server) == 1  # the app does not open it
    assert seen[0]["data_folder_problem"] == "unsafe" and "link" in seen[0]["data_folder_reason"]
    assert seen[0]["data_folder"] == str(data) and not (data / "scholia.sqlite3").exists()


@pytest.mark.parametrize("item, mode", [("config.toml", 0o666), ("projects", 0o777), ("credentials.json", 0o620)])
def test_a_data_folder_others_could_write_is_refused_and_left_as_it_is(tmp_path, item, mode):
    data = tmp_path / "data"
    (data / "projects").mkdir(parents=True)
    for name in ("config.toml", "credentials.json"):
        (data / name).write_text("x")
        os.chmod(data / name, 0o600)
    os.chmod(data / item, mode)  # planted settings could be anyone's
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.narrow_tree(data)
    assert stat.S_IMODE((data / item).stat().st_mode) == mode  # refused until the researcher checks it
    assert desktop.run(data, [].append) == 1
    os.chmod(data / item, mode & ~0o022)  # once checked and narrowed, it opens
    desktop.narrow_tree(data)


def test_a_data_folder_with_an_access_rule_letting_others_in_is_refused(tmp_path):
    from network_guard import allow_subprocess
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text("x")
    os.chmod(data / "config.toml", 0o600)
    with allow_subprocess("/bin/chmod"):
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(data / "config.toml")], check=True)
        try:
            with pytest.raises(desktop.UnsafeDataFolderError):
                desktop.narrow_tree(data)
        finally:
            subprocess.run(["/bin/chmod", "-N", str(data / "config.toml")], check=True)
        subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(data / "config.toml")], check=True)
        try:
            desktop.narrow_tree(data)  # a deny rule lets nobody in
        finally:
            subprocess.run(["/bin/chmod", "-N", str(data / "config.toml")], check=True)


def test_inherited_and_inheritable_access_rules_are_refused(tmp_path):
    from network_guard import allow_subprocess
    data = tmp_path / "data"
    data.mkdir()
    with allow_subprocess("/bin/chmod"):
        subprocess.run(["/bin/chmod", "+a", "everyone allow read,file_inherit,directory_inherit", str(data)],
                       check=True)
        (data / "config.toml").write_text("x")  # inherits the rule
        os.chmod(data / "config.toml", 0o600)
        try:
            assert desktop._acl_problem(data / "config.toml") == "an access rule lets other accounts in"
            with pytest.raises(desktop.UnsafeDataFolderError):
                desktop.narrow_tree(data)
        finally:
            subprocess.run(["/bin/chmod", "-N", str(data), str(data / "config.toml")], check=True)


def test_an_access_list_that_cannot_be_read_is_refused(tmp_path, monkeypatch):
    import ctypes
    import errno

    class Unreadable:
        def acl_get_link_np(self, path, kind):
            ctypes.set_errno(errno.EACCES)
            return None

    monkeypatch.setattr(desktop, "_libc", lambda: Unreadable())
    assert desktop._acl_problem(tmp_path) == "its access rules cannot be checked"


def test_every_file_is_narrowed_even_after_a_problem_was_found(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    os.symlink(tmp_path / "elsewhere", data / "a-link")
    for name in ("b.toml", "credentials.json", "z.toml"):
        (data / name).write_text("x")
        os.chmod(data / name, 0o644)
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.narrow_tree(data)
    for name in ("b.toml", "credentials.json", "z.toml"):
        assert stat.S_IMODE((data / name).stat().st_mode) == 0o600


def _health(seen):
    """A window that reads /api/health, as the interface does first."""
    def window(url):
        origin, session = url.split("/#session=")
        with httpx.Client(base_url=origin, headers={"X-Scholia-Client": "local", "X-Scholia-Session": session},
                          timeout=10) as http:
            seen.append(http.get("/api/health").json())
    return window


def _open_and_close(seen):
    def window(url):
        origin, session = url.split("/#session=")
        with httpx.Client(base_url=origin, headers={"X-Scholia-Client": "local", "X-Scholia-Session": session},
                          timeout=10) as http:
            seen.append(http.get("/api/health").json()["ok"])
    return window


def test_a_slow_daily_backup_does_not_count_against_the_start_deadline(tmp_path, monkeypatch):
    import time
    from backend.db import Database
    real_backup = Database.backup_if_due

    def slow_backup(self, now=None):  # a large folder's backup, longer than the start deadline
        time.sleep(1.5)
        return real_backup(self, now)

    monkeypatch.setattr(Database, "backup_if_due", slow_backup)
    monkeypatch.setattr(desktop, "START_SECONDS", 1)
    seen = []
    assert desktop.run(tmp_path / "data", _open_and_close(seen), keyring_backend=FakeKeyring(),
                       listening=register_server) == 0
    assert seen == [True]


def test_a_start_that_hangs_elsewhere_still_fails_at_the_deadline(tmp_path, monkeypatch):
    import time
    from backend.runs import Harness
    real_recover = Harness.recover

    async def slow_recover(self):
        import asyncio
        await asyncio.sleep(2.5)
        return await real_recover(self)

    monkeypatch.setattr(Harness, "recover", slow_recover)
    monkeypatch.setattr(desktop, "START_SECONDS", 1)
    seen = []
    started_at = time.monotonic()
    assert desktop.run(tmp_path / "data", _open_and_close(seen), keyring_backend=FakeKeyring(),
                       listening=register_server) == 1
    assert seen == [] and time.monotonic() - started_at < 20


@pytest.mark.parametrize("target", ["missing", "locked"])
def test_a_lock_file_that_is_a_link_is_refused_and_never_followed(tmp_path, target):
    data, outside = tmp_path / "data", tmp_path / "outside.lock"
    data.mkdir()
    if target == "locked":
        outside.write_text("")
    os.symlink(outside, data / desktop.LOCK_FILE)
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.take_lock(data)
    assert outside.exists() == (target == "locked")  # nothing created through the link
    windows = []
    assert desktop.run(data, windows.append) == 1
    assert len(windows) == 1 and windows[0] is not None  # why it was refused, not "already open"


@pytest.mark.parametrize("after, started", [(0.39, True), (0.41, False)])
def test_the_start_deadline_counts_only_time_outside_maintenance(monkeypatch, after, started):
    # 0.6 s of startup, then maintenance from 0.6 to 5.0, then `after` more seconds: the budget
    # of 1 s is spent exactly by the time outside maintenance.
    now = [0.0]
    server, state = type("Server", (), {"started": False})(), {}

    class Thread:
        def is_alive(self):
            return True

        def join(self, seconds):
            now[0] = round(now[0] + 0.01, 2)
            if now[0] == 0.6:
                state["maintenance_started"] = 0.6
            if now[0] == 5.0:
                state["maintenance_seconds"] = 4.4
                state.pop("maintenance_started")
            if now[0] == round(5.0 + after, 2) and started:
                server.started = True

    monkeypatch.setattr(desktop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(desktop, "START_SECONDS", 1)
    assert desktop._wait_started(server, Thread(), state) is started


@pytest.mark.parametrize("kind", ["folder", "unreadable"])
def test_a_lock_file_that_cannot_be_opened_is_refused(tmp_path, kind):
    data = tmp_path / "data"
    data.mkdir()
    lock = data / desktop.LOCK_FILE
    if kind == "folder":
        lock.mkdir()
    else:
        lock.write_text("")
        os.chmod(lock, 0o000)
    try:
        with pytest.raises(desktop.UnsafeDataFolderError):
            desktop.take_lock(data)
        assert desktop.run(data, [].append) == 1
    finally:
        if kind == "unreadable":
            os.chmod(lock, 0o600)


def test_a_slow_database_opening_does_not_count_against_the_start_deadline(tmp_path, monkeypatch):
    import time
    from backend import app as app_module
    real_database = app_module.Database

    def slow_database(data_dir):  # a large database's checks, backup and migration
        time.sleep(1.5)
        return real_database(data_dir)

    monkeypatch.setattr(app_module, "Database", slow_database)
    monkeypatch.setattr(desktop, "START_SECONDS", 1)
    seen = []
    assert desktop.run(tmp_path / "data", _open_and_close(seen), keyring_backend=FakeKeyring(),
                       listening=register_server) == 0
    assert seen == [True]


def test_the_start_deadline_reads_the_clock_and_the_maintenance_times_together(monkeypatch):
    import threading
    lock = threading.Lock()
    server, state = type("Server", (), {"started": False})(), {"maintenance_lock": lock}
    readings = []

    def clock():
        readings.append(lock.locked())
        server.started = len(readings) > 2
        return 0.0

    class Thread:
        def is_alive(self):
            return True

        def join(self, seconds):
            pass

    monkeypatch.setattr(desktop.time, "monotonic", clock)
    assert desktop._wait_started(server, Thread(), state) is True
    assert readings[1:] == [True] * (len(readings) - 1)  # every reading after the first, under the app's lock


@pytest.mark.parametrize("mode, refused", [(0o777, True), (0o775, True), (0o1777, False), (0o755, False)])
def test_a_data_folder_whose_parent_others_could_change_is_refused(tmp_path, mode, refused):
    parent = tmp_path / "parent"
    parent.mkdir()
    os.chmod(parent, mode)
    try:
        if refused:
            with pytest.raises(desktop.UnsafeDataFolderError):
                desktop.take_lock(parent / "data")
            assert desktop.run(parent / "data", [].append) == 1
            assert not (parent / "data").exists()  # nothing made or opened under it
        else:
            fd = desktop.take_lock(parent / "data")
            assert fd is not None
            os.close(fd)
    finally:
        os.chmod(parent, 0o755)


def test_a_data_folder_under_a_parent_with_an_access_rule_letting_others_in_is_refused(tmp_path):
    from network_guard import allow_subprocess
    parent = tmp_path / "parent"
    parent.mkdir()
    with allow_subprocess("/bin/chmod"):
        subprocess.run(["/bin/chmod", "+a", "everyone allow add_subdirectory,delete_child", str(parent)], check=True)
        try:
            with pytest.raises(desktop.UnsafeDataFolderError):
                desktop.take_lock(parent / "data")
        finally:
            subprocess.run(["/bin/chmod", "-N", str(parent)], check=True)


def test_a_link_above_the_data_folder_is_followed_and_every_folder_on_its_way_checked(tmp_path):
    safe, shared = tmp_path / "safe", tmp_path / "shared"
    (safe / "actual").mkdir(parents=True)
    shared.mkdir()
    os.symlink(safe / "actual", shared / "hop")  # a link through a folder others can change
    os.symlink(shared / "hop", safe / "entry")
    os.chmod(shared, 0o777)
    try:
        with pytest.raises(desktop.UnsafeDataFolderError):
            desktop.take_lock(safe / "entry" / "data")
        os.chmod(shared, 0o755)
        fd = desktop.take_lock(safe / "entry" / "data")  # the same way through safe folders
        assert fd is not None
        os.close(fd)
    finally:
        os.chmod(shared, 0o755)


def test_nothing_is_made_under_a_folder_others_could_change(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    os.chmod(unsafe, 0o777)
    try:
        with pytest.raises(desktop.UnsafeDataFolderError):
            desktop.take_lock(unsafe / "missing" / "Scholia")
        assert not (unsafe / "missing").exists()
    finally:
        os.chmod(unsafe, 0o755)


def test_a_lock_file_open_to_others_is_refused_at_every_launch_until_removed(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    lock = data / desktop.LOCK_FILE
    lock.write_text("")
    os.chmod(lock, 0o644)  # another account could have opened it and taken the lock
    held = os.open(lock, os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        for _ in range(2):  # the evidence stays: every launch refuses, none reports another Scholia
            with pytest.raises(desktop.UnsafeDataFolderError):
                desktop.take_lock(data)
        assert stat.S_IMODE(lock.stat().st_mode) == 0o644
        lock.unlink()  # the researcher removes it
        fd = desktop.take_lock(data)  # a new, owner-only lock file
        assert fd is not None and stat.S_IMODE(lock.stat().st_mode) == 0o600
        os.close(fd)
    finally:
        os.close(held)


def test_a_data_folder_path_that_goes_back_up_is_refused(tmp_path):
    (tmp_path / "a").mkdir()
    with pytest.raises(desktop.UnsafeDataFolderError):
        desktop.take_lock(tmp_path / "a" / ".." / "data")
    assert not (tmp_path / "data").exists()


def test_a_data_folder_others_could_write_is_refused_before_its_lock_is_opened(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o777)
    try:
        with pytest.raises(desktop.UnsafeDataFolderError):
            desktop.take_lock(data)
        assert not (data / desktop.LOCK_FILE).exists()
    finally:
        os.chmod(data, 0o700)
