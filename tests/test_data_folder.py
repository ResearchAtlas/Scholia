"""The data folder's place: synced and network folders refused, another place chosen and recorded.

Sync folders and network file systems are simulated (a stand-in home folder and an injected
file system type); no real sync folder or share is used.
"""

import json
import os
import stat

import httpx
import pytest

from backend import data_folder, desktop
from backend.desktop import UnsafeDataFolderError
from network_guard import register_server
from scholia_app import FakeKeyring


def apfs(path):
    return "apfs"


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("place, named", [
    ("Library/Mobile Documents/com~apple~CloudDocs/Scholia", "iCloud Drive"),
    ("Library/CloudStorage/OneDrive-Personal/Scholia", "cloud storage"),
    ("Library/CloudStorage/Dropbox/Scholia", "cloud storage"),
    ("Library/CloudStorage/GoogleDrive-researcher@example.org/My Drive/Scholia", "cloud storage"),
    ("Dropbox/Research/Scholia", "Dropbox"),
])
def test_a_folder_inside_a_synced_folder_is_refused(tmp_path, place, named):
    assert named in data_folder.synced(tmp_path / place, home=tmp_path, fs_type=apfs)


def test_a_local_folder_and_one_beside_the_synced_ones_are_accepted(tmp_path):
    for place in ("Library/Application Support/Scholia", "Dropbox notes/Scholia", "Research/Scholia"):
        assert data_folder.synced(tmp_path / place, home=tmp_path, fs_type=apfs) is None


def test_a_link_into_a_synced_folder_is_followed(tmp_path):
    (tmp_path / "Dropbox" / "Research").mkdir(parents=True)
    (tmp_path / "Documents").mkdir()
    os.symlink(tmp_path / "Dropbox" / "Research", tmp_path / "Documents" / "Research")
    assert "Dropbox" in data_folder.synced(tmp_path / "Documents" / "Research" / "Scholia", home=tmp_path,
                                           fs_type=apfs)


@pytest.mark.parametrize("kind, refused", [("smbfs", True), ("afpfs", True), ("nfs", True), ("webdav", True),
                                           ("apfs", False), ("hfs", False), (None, False)])
def test_a_folder_on_a_network_file_system_is_refused(tmp_path, kind, refused):
    asked = []

    def fs_type(path):
        asked.append(path)
        return kind

    problem = data_folder.synced(tmp_path / "not" / "made" / "yet", home=tmp_path / "home", fs_type=fs_type)
    assert (problem is not None) == refused
    assert not refused or kind in problem
    assert asked == [tmp_path.resolve()]  # the nearest folder that exists


def test_the_file_system_type_is_read_from_the_system():
    assert data_folder.file_system_type("/") == "apfs"


# The location record


def test_without_a_record_the_default_folder_is_used(tmp_path):
    assert data_folder.located(tmp_path / "default") == tmp_path / "default"


def test_a_chosen_place_is_recorded_owner_only_beside_the_default_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, chosen = tmp_path / "default", tmp_path / "Research" / "Scholia"
    chosen.parent.mkdir()
    assert data_folder.choose(default, str(chosen)) == chosen
    record = default.parent / data_folder.LOCATION_FILE
    assert json.loads(record.read_text()) == {"path": str(chosen)}
    assert (mode(record), mode(chosen)) == (0o600, 0o700) and not default.exists()  # nothing made in it
    assert data_folder.located(default) == chosen
    data_folder.choose(default, str(default))  # back to the default: no record
    assert not record.exists() and data_folder.located(default) == default


@pytest.mark.parametrize("path, message, code", [
    ("Research/Scholia", "full path", "data_folder_invalid"),
    ("/{tmp}/Research/../Scholia", "full path", "data_folder_invalid"),
    ("/{tmp}/share/Scholia", "network file system", "data_folder_synced"),
    ("/{tmp}/missing/Scholia", "does not exist", "data_folder_not_found"),
    ("/{tmp}/a-file", "not a folder", "data_folder_invalid"),
])
def test_a_place_that_cannot_hold_the_data_is_not_recorded(tmp_path, monkeypatch, path, message, code):
    monkeypatch.setattr(data_folder, "file_system_type",
                        lambda p: "smbfs" if "share" in str(p) else "apfs")
    (tmp_path / "share").mkdir()
    (tmp_path / "a-file").write_text("x")
    with pytest.raises(data_folder.FolderRefused, match=message) as refused:
        data_folder.choose(tmp_path / "default", path.format(tmp=str(tmp_path).lstrip("/")))
    assert refused.value.code == code
    assert not (tmp_path / data_folder.LOCATION_FILE).exists()


def test_a_folder_others_could_change_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "shared"
    chosen.mkdir()
    os.chmod(chosen, 0o777)
    with pytest.raises(UnsafeDataFolderError):
        data_folder.choose(tmp_path / "default", str(chosen))
    assert not (tmp_path / data_folder.LOCATION_FILE).exists()


@pytest.mark.parametrize("default_is", ["a link", "synced"])
def test_another_place_is_chosen_without_writing_into_a_refused_default_folder(tmp_path, monkeypatch, default_is):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    home = tmp_path / "home"
    synced = home / "Dropbox" / "Scholia"
    synced.mkdir(parents=True)
    support = home / "Library" / "Application Support"
    support.mkdir(parents=True)
    if default_is == "a link":
        default = support / "Scholia"
        os.symlink(synced, default)  # the default folder leads into Dropbox
    else:
        default = synced
    chosen = tmp_path / "Local" / "Scholia"
    chosen.parent.mkdir()
    assert data_folder.choose(default, str(chosen), home=home) == chosen
    assert list(synced.iterdir()) == []  # the refused folder is never written
    assert data_folder.located(default) == chosen


def test_the_folder_a_linked_default_leads_to_is_recorded_rather_than_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    target = tmp_path / "Research" / "Scholia"
    target.mkdir(parents=True)
    default = tmp_path / "Scholia"
    os.symlink(target, default)  # refused at launch: its target could be swapped
    assert data_folder.choose(default, str(target)) == target
    assert data_folder.located(default) == target  # the next launch opens the folder itself


@pytest.mark.parametrize("problem", ["writable by others", "a link", "not json", "relative", "goes back up"])
def test_a_record_that_cannot_be_trusted_or_read_refuses_the_data_folder(tmp_path, problem):
    default = tmp_path / "default"
    default.mkdir()
    record = default.parent / data_folder.LOCATION_FILE
    text = {"not json": "{", "relative": '{"path": "Scholia"}', "goes back up": '{"path": "/tmp/../etc"}'}.get(
        problem, json.dumps({"path": str(tmp_path / "elsewhere")}))
    if problem == "a link":
        (tmp_path / "planted.json").write_text(text)
        os.symlink(tmp_path / "planted.json", record)
    else:
        record.write_text(text)
        os.chmod(record, 0o666 if problem == "writable by others" else 0o600)
    with pytest.raises(UnsafeDataFolderError):
        data_folder.located(default)


# The desktop entry


def _window(seen, then=None):
    """A window that reads /api/health, then runs then(http) if given."""
    def window(url):
        origin, session = url.split("/#session=")
        with httpx.Client(base_url=origin, headers={"X-Scholia-Client": "local", "X-Scholia-Session": session},
                          timeout=10) as http:
            seen.append(http.get("/api/health").json())
            if then:
                then(http)
    return window


def test_a_data_folder_on_a_network_share_is_never_opened_and_another_place_is_chosen(tmp_path, monkeypatch):
    share, chosen = tmp_path / "share" / "Scholia", tmp_path / "local" / "Scholia"
    share.mkdir(parents=True)
    chosen.parent.mkdir()
    monkeypatch.setattr(data_folder, "file_system_type", lambda p: "smbfs" if "share" in str(p) else "apfs")
    seen, answers = [], {}

    def on_the_screen(http):
        answers["first"] = [http.get(path).json() for path in ("/api/setup", "/api/settings")]  # as the interface starts
        answers["other"] = http.get("/api/projects").json()
        answers["refused"] = http.post("/api/data-folder", json={"path": str(tmp_path / "share" / "Other")}).json()
        answers["odd"] = [http.post("/api/data-folder", json=body).json()["code"]
                          for body in ({"path": "/tmp/x\u0000y"}, {"path": 3}, {})]
        answers["chosen"] = http.post("/api/data-folder", json={"path": str(chosen)}).json()

    assert desktop.run(share, _window(seen, on_the_screen), listening=register_server) == 1
    assert seen[0]["data_folder_problem"] == "synced" and seen[0]["data_folder"] == str(share)
    assert "network file system (smbfs)" in seen[0]["data_folder_reason"]
    assert answers["first"] == [{"needed": False}, {"values": {}, "warnings": [], "hash": None}]
    assert answers["other"]["code"] == "data_folder_problem"
    assert answers["refused"]["code"] == "data_folder_synced"
    assert answers["odd"] == ["data_folder_invalid", "invalid_request", "invalid_request"]
    assert answers["chosen"] == {"ok": True, "data_folder": str(chosen), "restart": True}
    assert list(share.iterdir()) == []  # nothing written there: no lock, log, database or record

    seen.clear()  # the next launch opens the chosen place, and never the share
    assert desktop.run(share, _window(seen), keyring_backend=FakeKeyring(), listening=register_server) == 0
    assert seen[0]["ok"] is True and "data_folder_problem" not in seen[0]
    assert (chosen / "scholia.sqlite3").is_file() and not (share / "scholia.sqlite3").exists()


def test_a_chosen_folder_that_is_not_there_is_not_made_anew(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, disk = tmp_path / "default", tmp_path / "Volumes" / "Research"
    disk.mkdir(parents=True)
    data_folder.choose(default, str(disk / "Scholia"))
    (disk / "Scholia").rmdir()
    disk.rmdir()  # the disk is not connected
    seen = []
    assert desktop.run(default, _window(seen), listening=register_server) == 1
    assert seen[0]["data_folder_problem"] == "missing" and seen[0]["data_folder"] == str(disk / "Scholia")
    assert not disk.exists()


def test_only_an_empty_folder_or_one_holding_scholias_data_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, documents, moved = tmp_path / "default", tmp_path / "Documents", tmp_path / "Moved"
    documents.mkdir()
    (documents / "run.sh").write_text("echo hi\n")
    os.chmod(documents / "run.sh", 0o755)
    with pytest.raises(data_folder.FolderRefused, match="empty folder"):  # its files would be narrowed to owner-only
        data_folder.choose(default, str(documents))
    assert mode(documents / "run.sh") == 0o755 and not (default.parent / data_folder.LOCATION_FILE).exists()
    moved.mkdir()
    (moved / "scholia.sqlite3").write_bytes(b"")  # a data folder the researcher moved there
    assert data_folder.choose(default, str(moved)) == moved


def test_the_synced_check_ignores_case_as_the_file_system_does(tmp_path):
    for place in ("dropbox/Scholia", "library/cloudstorage/OneDrive-x/Scholia", "LIBRARY/Mobile Documents/x"):
        assert data_folder.synced(tmp_path / place, home=tmp_path, fs_type=apfs) is not None


def test_a_folder_finder_shows_as_empty_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "New folder"
    chosen.mkdir()
    (chosen / ".DS_Store").write_bytes(b"\0")
    assert data_folder.choose(tmp_path / "default", str(chosen)) == chosen


def test_the_default_chosen_again_by_another_spelling_removes_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, other = tmp_path / "default", tmp_path / "other"
    other.mkdir()
    data_folder.choose(default, str(other))
    default.mkdir()  # it holds the earlier data, and something else
    (default / "notes.txt").write_text("not Scholia's")
    spelled = str(default)[:-len("default")] + "DEFAULT"  # the same folder on a case-insensitive volume
    assert os.path.samefile(spelled, default)
    data_folder.choose(default, spelled)
    assert not (default.parent / data_folder.LOCATION_FILE).exists()


def test_the_synced_check_ignores_unicode_normalization(tmp_path):
    import unicodedata
    home = tmp_path / unicodedata.normalize("NFC", "Zoë")
    written = tmp_path / unicodedata.normalize("NFD", "Zoë") / "Dropbox" / "Scholia"
    assert data_folder.synced(written, home=home, fs_type=apfs) is not None


def another_apps_database(folder):
    import sqlite3
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "scholia.sqlite3")
    conn.execute("CREATE TABLE notes (text TEXT)")
    conn.commit()
    conn.close()


def a_newer_scholia_database(folder):
    from backend.db import Database
    Database(folder).close()
    conn = __import__("sqlite3").connect(folder / "scholia.sqlite3")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()


def a_newer_scholia_database_in_its_log(folder):
    """As a newer Scholia that stopped mid-way leaves it: its schema only in the write-ahead log."""
    import shutil
    import sqlite3
    from backend.db import Database
    source = folder.parent / "source"
    Database(source).close()
    conn = sqlite3.connect(source / "scholia.sqlite3")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("scholia.sqlite3", "scholia.sqlite3-wal"):
        shutil.copyfile(source / name, folder / name)  # copied while it is open, as a crash leaves them
    conn.close()


@pytest.mark.parametrize("held, code", [(another_apps_database, "data_folder_foreign"),
                                         (a_newer_scholia_database, "data_folder_newer"),
                                         (a_newer_scholia_database_in_its_log, "data_folder_newer")])
def test_a_folder_whose_database_scholia_cannot_open_is_not_recorded(tmp_path, monkeypatch, held, code):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "Other"
    held(chosen)
    before = sorted(p.name for p in chosen.iterdir())
    with pytest.raises(data_folder.FolderRefused) as refused:
        data_folder.choose(tmp_path / "default", str(chosen))
    assert refused.value.code == code
    assert not (tmp_path / data_folder.LOCATION_FILE).exists()
    assert sorted(p.name for p in chosen.iterdir()) == before  # read without writing anything there


def test_a_folder_with_a_damaged_scholia_database_can_be_chosen(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "Other"
    chosen.mkdir()
    (chosen / "scholia.sqlite3").write_bytes(b"not a database " * 400)  # the app offers a restore there
    assert data_folder.choose(tmp_path / "default", str(chosen)) == chosen


@pytest.mark.parametrize("held, problem", [(another_apps_database, "foreign"), (a_newer_scholia_database, "newer"),
                                            (a_newer_scholia_database_in_its_log, "newer")])
def test_a_chosen_folder_holding_a_database_scholia_cannot_open_is_explained_untouched(tmp_path, monkeypatch,
                                                                                       held, problem):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, chosen = tmp_path / "default", tmp_path / "Other"
    chosen.mkdir()
    data_folder.choose(default, str(chosen))  # empty when chosen
    held(chosen)  # then it held another database: moved or copied there by hand
    before = {p.name: p.read_bytes() for p in chosen.iterdir()}
    seen = []
    assert desktop.run(default, _window(seen), listening=register_server) == 1
    assert seen[0]["data_folder_problem"] == problem and seen[0]["data_folder"] == str(chosen)
    assert {p.name: p.read_bytes() for p in chosen.iterdir()} == before  # no lock, log or database written


def test_the_default_folders_newer_database_is_still_refused_at_launch(tmp_path):
    default = tmp_path / "default"
    a_newer_scholia_database(default)
    seen = []
    assert desktop.run(default, _window(seen), keyring_backend=FakeKeyring(), listening=register_server) == 1
    assert seen == []  # as S1-08 refuses it: the app does not open


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any file")
def test_a_folder_whose_database_cannot_be_read_is_left_to_open_as_it_would(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "Other"
    another_apps_database(chosen)
    (chosen / "scholia.sqlite3").chmod(0)
    assert data_folder.database_problem(chosen) is None  # not a crash, at launch or here
    assert data_folder.choose(tmp_path / "default", str(chosen)) == chosen


def test_choosing_the_default_folder_back_checks_its_database(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default = tmp_path / "default"
    a_newer_scholia_database(default)
    with pytest.raises(data_folder.FolderRefused) as refused:
        data_folder.choose(default, str(default))
    assert refused.value.code == "data_folder_newer"


def test_a_folder_others_could_change_is_refused_before_its_database_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "Other"
    another_apps_database(chosen)
    chosen.chmod(0o777)
    read = []
    monkeypatch.setattr(data_folder, "check_identity", read.append)
    with pytest.raises(UnsafeDataFolderError):
        data_folder.choose(tmp_path / "default", str(chosen))
    assert read == [] and not (tmp_path / data_folder.LOCATION_FILE).exists()


def test_a_folder_whose_database_is_a_link_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    elsewhere, chosen = tmp_path / "elsewhere", tmp_path / "Other"
    another_apps_database(elsewhere)
    chosen.mkdir()
    os.symlink(elsewhere / "scholia.sqlite3", chosen / "scholia.sqlite3")
    with pytest.raises(UnsafeDataFolderError):
        data_folder.choose(tmp_path / "default", str(chosen))
    assert not (tmp_path / data_folder.LOCATION_FILE).exists()


def test_a_chosen_folder_that_became_a_link_is_unsafe_whatever_it_leads_to(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, chosen, real = tmp_path / "default", tmp_path / "Other", tmp_path / "Real"
    chosen.mkdir()
    data_folder.choose(default, str(chosen))
    chosen.rename(real)
    another_apps_database(real)
    os.symlink(real, chosen)
    seen = []
    assert desktop.run(default, _window(seen), listening=register_server) == 1
    assert seen[0]["data_folder_problem"] == "unsafe"  # the link is the problem, not what it leads to
