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


def test_a_chosen_place_is_recorded_owner_only_in_the_default_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    default, chosen = tmp_path / "default", tmp_path / "Research" / "Scholia"
    chosen.parent.mkdir()
    assert data_folder.choose(default, str(chosen)) == chosen
    record = default / data_folder.LOCATION_FILE
    assert json.loads(record.read_text()) == {"path": str(chosen)}
    assert (mode(default), mode(record), mode(chosen)) == (0o700, 0o600, 0o700)
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
    assert not (tmp_path / "default" / data_folder.LOCATION_FILE).exists()


def test_a_folder_others_could_change_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(data_folder, "file_system_type", apfs)
    chosen = tmp_path / "shared"
    chosen.mkdir()
    os.chmod(chosen, 0o777)
    with pytest.raises(UnsafeDataFolderError):
        data_folder.choose(tmp_path / "default", str(chosen))
    assert not (tmp_path / "default" / data_folder.LOCATION_FILE).exists()


@pytest.mark.parametrize("problem", ["writable by others", "a link", "not json", "relative", "goes back up"])
def test_a_record_that_cannot_be_trusted_or_read_refuses_the_data_folder(tmp_path, problem):
    default = tmp_path / "default"
    default.mkdir()
    record = default / data_folder.LOCATION_FILE
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
        answers["chosen"] = http.post("/api/data-folder", json={"path": str(chosen)}).json()

    assert desktop.run(share, _window(seen, on_the_screen), listening=register_server) == 1
    assert seen[0]["data_folder_problem"] == "synced" and seen[0]["data_folder"] == str(share)
    assert "network file system (smbfs)" in seen[0]["data_folder_reason"]
    assert answers["first"] == [{"needed": False}, {"values": {}, "warnings": [], "hash": None}]
    assert answers["other"]["code"] == "data_folder_problem"
    assert answers["refused"]["code"] == "data_folder_synced"
    assert answers["chosen"] == {"ok": True, "data_folder": str(chosen), "restart": True}
    assert sorted(p.name for p in share.iterdir()) == [data_folder.LOCATION_FILE]  # no lock, log or database

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
    assert mode(documents / "run.sh") == 0o755 and not (default / data_folder.LOCATION_FILE).exists()
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
    (default / "notes.txt").write_text("not Scholia's")
    spelled = str(default)[:-len("default")] + "DEFAULT"  # the same folder on a case-insensitive volume
    assert os.path.samefile(spelled, default)
    data_folder.choose(default, spelled)
    assert not (default / data_folder.LOCATION_FILE).exists()


def test_the_synced_check_ignores_unicode_normalization(tmp_path):
    import unicodedata
    home = tmp_path / unicodedata.normalize("NFC", "Zoë")
    written = tmp_path / unicodedata.normalize("NFD", "Zoë") / "Dropbox" / "Scholia"
    assert data_folder.synced(written, home=home, fs_type=apfs) is not None
