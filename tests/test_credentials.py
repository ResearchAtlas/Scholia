import stat
import threading
import time

import keyring
import keyring.backends.fail
import keyring.backends.null
import pytest

from backend import credentials
from backend.credentials import SERVICE, load_key, save_key
from backend.settings import load_settings

KEY = "sk-or-v1-test-0123456789"


class MemoryKeyring:
    """A credential store that lives in this test only."""

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def no_system_keyring(monkeypatch):
    # Any use of the default backend (the real Keychain on macOS) fails the test.
    def refuse():
        # pytest.fail is a BaseException, so no "store unavailable" handler can swallow it.
        pytest.fail("a test reached the system credential store")

    monkeypatch.setattr(keyring, "get_keyring", refuse)


def files_containing(root, secret):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and secret.encode() in p.read_bytes())


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_key_goes_to_the_credential_store(tmp_path):
    store = MemoryKeyring()
    assert save_key(tmp_path, "openrouter", KEY, backend=store) is None
    assert store.items == {(SERVICE, "openrouter"): KEY}
    assert load_key(tmp_path, "openrouter", backend=store) == KEY
    assert files_containing(tmp_path, KEY) == []


def test_default_backend_is_keyring(tmp_path, monkeypatch):
    store = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: store)
    assert save_key(tmp_path, "openrouter", KEY) is None
    assert load_key(tmp_path, "openrouter") == KEY
    assert store.items == {(SERVICE, "openrouter"): KEY}


@pytest.mark.parametrize("unavailable", [keyring.backends.fail.Keyring, keyring.backends.null.Keyring])
def test_unavailable_store_falls_back_to_an_owner_only_file(tmp_path, unavailable):
    data_root = tmp_path / "data"  # not created yet
    warning = save_key(data_root, "openrouter", KEY, backend=unavailable())
    assert warning and "owner-only file" in warning and KEY not in warning
    assert files_containing(data_root, KEY) == ["credentials.json"]
    assert mode(data_root) == 0o700
    assert mode(data_root / "credentials.json") == 0o600
    assert load_key(data_root, "openrouter", backend=unavailable()) == KEY


def test_loading_reads_the_store_first(tmp_path):
    save_key(tmp_path, "openrouter", KEY, backend=keyring.backends.fail.Keyring())
    store = MemoryKeyring()
    assert load_key(tmp_path, "openrouter", backend=store) == KEY  # not in the store: the file
    store.items[(SERVICE, "openrouter")] = "sk-in-store"
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-in-store"  # the store comes first
    assert load_key(tmp_path, "other", backend=keyring.backends.fail.Keyring()) is None


def test_storing_in_the_store_removes_the_fallback_copy(tmp_path):
    failing = keyring.backends.fail.Keyring()
    save_key(tmp_path, "openrouter", KEY, backend=failing)
    save_key(tmp_path, "local", "sk-local-key", backend=failing)
    store = MemoryKeyring()
    assert save_key(tmp_path, "openrouter", KEY, backend=store) is None
    assert files_containing(tmp_path, KEY) == []
    assert load_key(tmp_path, "local", backend=MemoryKeyring()) == "sk-local-key"


def test_keys_never_enter_config_toml(tmp_path):
    (tmp_path / "config.toml").write_text('[providers.openrouter]\nkind = "openrouter"\n')
    loaded = load_settings(tmp_path)
    save_key(tmp_path, "openrouter", KEY, backend=MemoryKeyring())
    save_key(tmp_path, "openrouter", KEY, backend=keyring.backends.fail.Keyring())
    loaded.save({"ui.language": "en"})  # the settings file did not change underneath
    assert KEY not in (tmp_path / "config.toml").read_text()
    assert files_containing(tmp_path, KEY) == ["credentials.json"]


def test_unknown_key_is_none(tmp_path):
    assert load_key(tmp_path, "openrouter", backend=MemoryKeyring()) is None


MALFORMED = [
    b'{"openrouter": "sk-or-saved", "local": "sk-lo',  # cut off mid-write
    b"not json",
    b'["sk-or-saved"]',
    b'{"openrouter": 5}',
    b"\xff\xfe",
    b"[" * 10_000 + b"]" * 10_000,  # nested deeper than the decoder can go
]


@pytest.mark.parametrize("content", MALFORMED)
def test_malformed_fallback_file_is_never_overwritten(tmp_path, content):
    path = tmp_path / "credentials.json"
    path.write_bytes(content)
    with pytest.raises(credentials.CredentialsFileError) as error:
        save_key(tmp_path, "other", KEY, backend=keyring.backends.fail.Keyring())
    assert KEY not in str(error.value)
    assert path.read_bytes() == content
    # With the store working, the key is safe there and the file is still left alone.
    store = MemoryKeyring()
    assert save_key(tmp_path, "other", KEY, backend=store) is None
    assert store.items == {(SERVICE, "other"): KEY}
    assert path.read_bytes() == content


@pytest.mark.parametrize("content", MALFORMED)
def test_malformed_fallback_file_is_ignored_on_load_with_a_warning(tmp_path, content, caplog):
    (tmp_path / "credentials.json").write_bytes(content)
    assert load_key(tmp_path, "openrouter", backend=keyring.backends.fail.Keyring()) is None
    assert "credentials.json" in caplog.text and "sk-" not in caplog.text
    store = MemoryKeyring()
    store.items[(SERVICE, "openrouter")] = KEY
    assert load_key(tmp_path, "openrouter", backend=store) == KEY


def test_unreadable_fallback_file_never_stops_loading(tmp_path, caplog):
    (tmp_path / "credentials.json").mkdir()  # cannot be read as a file
    assert load_key(tmp_path, "openrouter", backend=keyring.backends.fail.Keyring()) is None
    assert "credentials.json" in caplog.text
    with pytest.raises(credentials.CredentialsFileError):
        save_key(tmp_path, "openrouter", KEY, backend=keyring.backends.fail.Keyring())


@pytest.mark.parametrize("provider, key", [
    ("openrouter", ""), ("openrouter", 123), ("openrouter", None), ("openrouter", b"sk-bytes"), ("", KEY), (5, KEY),
])
def test_keys_and_providers_must_be_nonempty_text(tmp_path, provider, key):
    store = MemoryKeyring()
    for backend in (store, keyring.backends.fail.Keyring()):
        with pytest.raises(ValueError):
            save_key(tmp_path, provider, key, backend=backend)
    assert store.items == {}
    assert list(tmp_path.iterdir()) == []


def test_legacy_configuration_is_never_read(tmp_path, monkeypatch):
    # The predecessor app kept its key in .env and the environment; neither is a source here.
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.chdir(tmp_path)
    assert load_key(tmp_path, "openrouter", backend=MemoryKeyring()) is None
    assert load_key(tmp_path, "openrouter", backend=keyring.backends.fail.Keyring()) is None


class ReadOnlyKeyring(MemoryKeyring):
    """A store that still returns what it holds but refuses new keys, as when it is locked."""

    def set_password(self, service, username, password):
        raise keyring.errors.PasswordSetError("locked")


def test_latest_save_wins_after_a_failed_rotation(tmp_path):
    store = MemoryKeyring()
    save_key(tmp_path, "openrouter", "sk-old", backend=store)
    locked = ReadOnlyKeyring()
    locked.items = store.items  # the same store, now refusing writes
    assert save_key(tmp_path, "openrouter", "sk-new", backend=locked)  # fell back, with a warning
    assert store.items == {}  # the older key cannot shadow the new one
    assert load_key(tmp_path, "openrouter", backend=locked) == "sk-new"
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-new"
    assert save_key(tmp_path, "openrouter", "sk-newest", backend=store) is None  # the store works again
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-newest"
    assert files_containing(tmp_path, "sk-new") == []


def test_store_that_cannot_start_falls_back(tmp_path, monkeypatch):
    # What keyring does for a misconfigured PYTHON_KEYRING_BACKEND: the import fails.
    monkeypatch.setattr(keyring, "get_keyring", lambda: keyring.core.load_keyring("no_such_module.Keyring"))
    assert save_key(tmp_path, "openrouter", KEY)
    assert load_key(tmp_path, "openrouter") == KEY
    assert load_key(tmp_path, "other") is None


def test_concurrent_fallback_saves_keep_every_key(tmp_path, monkeypatch):
    real_write = credentials.write_private

    def slow_write(path, data):
        time.sleep(0.02)  # widens the window between reading and replacing the file
        real_write(path, data)

    monkeypatch.setattr(credentials, "write_private", slow_write)
    providers = [f"provider{i}" for i in range(8)]
    threads = [
        threading.Thread(target=save_key, args=(tmp_path, name, f"sk-{name}"),
                         kwargs={"backend": keyring.backends.fail.Keyring()})
        for name in providers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {name: load_key(tmp_path, name, backend=MemoryKeyring()) for name in providers} == {
        name: f"sk-{name}" for name in providers
    }


class FailingKeyring:
    """A store whose calls fail with an ordinary error, as a native or plugin backend can."""

    def __init__(self, error, on_get=True, on_set=True):
        self.error, self.on_get, self.on_set, self.items = error, on_get, on_set, {}

    def get_password(self, service, username):
        if self.on_get:
            raise self.error
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        if self.on_set:
            raise self.error
        self.items[(service, username)] = password


@pytest.mark.parametrize("store", [
    FailingKeyring(OSError("native failure")),
    FailingKeyring(RuntimeError("plugin failure"), on_get=True, on_set=False),  # fails verifying
])
def test_ordinary_store_errors_fall_back_to_the_file(tmp_path, store):
    assert save_key(tmp_path, "openrouter", KEY, backend=store)  # a warning: the file was used
    assert files_containing(tmp_path, KEY) == ["credentials.json"]
    assert load_key(tmp_path, "openrouter", backend=store) == KEY
    assert load_key(tmp_path, "other", backend=store) is None


@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
def test_interrupts_from_the_store_are_not_swallowed(tmp_path, stop):
    with pytest.raises(stop):
        save_key(tmp_path, "openrouter", KEY, backend=FailingKeyring(stop()))
    with pytest.raises(stop):
        load_key(tmp_path, "openrouter", backend=FailingKeyring(stop()))
    assert list(tmp_path.iterdir()) == []


class ReplacingKeyring(MemoryKeyring):
    """Like the macOS Keychain backend: a new key deletes the old item, then adds."""

    def set_password(self, service, username, password):
        self.items.pop((service, username), None)
        raise keyring.errors.PasswordSetError("add failed")


def disk_full(path, data):
    raise OSError("disk full")


@pytest.mark.parametrize("unusable_file", ["malformed", "unwritable"])
def test_failed_save_may_leave_no_key(tmp_path, monkeypatch, unusable_file):
    # The documented limit: the store dropped the old key, and the file cannot take the new one.
    store = MemoryKeyring()
    save_key(tmp_path, "openrouter", "sk-old", backend=store)
    replacing = ReplacingKeyring()
    replacing.items = store.items
    path = tmp_path / "credentials.json"
    if unusable_file == "malformed":
        path.write_bytes(MALFORMED[0])
    else:
        monkeypatch.setattr(credentials, "write_private", disk_full)
    with pytest.raises(credentials.CredentialsFileError, match="not saved") as error:
        save_key(tmp_path, "openrouter", "sk-new", backend=replacing)
    assert "sk-" not in str(error.value)
    assert load_key(tmp_path, "openrouter", backend=store) is None  # the researcher enters it again
    if unusable_file == "malformed":
        assert path.read_bytes() == MALFORMED[0]
    else:
        assert not path.exists()


def test_failed_removal_of_the_old_copy_still_saves(tmp_path, monkeypatch, caplog):
    save_key(tmp_path, "openrouter", "sk-old", backend=keyring.backends.fail.Keyring())  # in the file
    content = (tmp_path / "credentials.json").read_bytes()
    monkeypatch.setattr(credentials, "write_private", disk_full)
    store = MemoryKeyring()
    assert save_key(tmp_path, "openrouter", "sk-new", backend=store) is None
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-new"  # the store comes first
    assert (tmp_path / "credentials.json").read_bytes() == content
    assert "credentials.json" in caplog.text and "sk-" not in caplog.text


class UndeletableKeyring(ReadOnlyKeyring):
    def delete_password(self, service, username):
        raise keyring.errors.PasswordDeleteError("locked")


def test_older_store_key_that_cannot_be_cleared_is_logged(tmp_path, caplog):
    # The documented limit: a store that refuses both the write and the delete keeps its older key.
    store = UndeletableKeyring()
    store.items[(SERVICE, "openrouter")] = "sk-old"
    assert save_key(tmp_path, "openrouter", "sk-new", backend=store)  # fell back, with a warning
    assert "credential store could not be cleared" in caplog.text and "sk-" not in caplog.text
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-old"


def test_backups_carry_settings_and_instructions_but_never_keys(tmp_path):
    # The file names and places used here are the ones the database's backups copy.
    from backend.db import Database, new_id

    data, project = tmp_path / "data", new_id()
    load_settings(data).save({"ui.language": "en"})
    load_settings(data, project).save({"project.target_venue": "Nature"})
    (data / "AGENTS.md").write_text("Personal instructions\n")
    (data / "projects" / project / "AGENTS.md").write_text("Project instructions\n")
    assert save_key(data, "openrouter", KEY, backend=keyring.backends.fail.Keyring())  # in credentials.json
    with Database(data) as db:
        generation = db.backup()
    copied = {str(p.relative_to(generation)) for p in generation.rglob("*") if p.is_file()}
    assert {"config.toml", "AGENTS.md", f"projects/{project}/config.toml", f"projects/{project}/AGENTS.md"} <= copied
    assert files_containing(generation, KEY) == [] and "credentials.json" not in copied


@pytest.mark.parametrize("in_file", [KEY, None])
def test_an_empty_store_value_is_not_a_key(tmp_path, in_file):
    if in_file:
        save_key(tmp_path, "openrouter", in_file, backend=keyring.backends.fail.Keyring())
    store = MemoryKeyring()
    store.items[(SERVICE, "openrouter")] = ""
    assert load_key(tmp_path, "openrouter", backend=store) == in_file


def test_an_empty_fallback_entry_is_not_a_key(tmp_path):
    (tmp_path / "credentials.json").write_text('{"openrouter": ""}')
    assert load_key(tmp_path, "openrouter", backend=MemoryKeyring()) is None


def test_a_load_racing_a_save_still_finds_a_key(tmp_path):
    save_key(tmp_path, "openrouter", "sk-old", backend=keyring.backends.fail.Keyring())  # in the file
    store_read = threading.Event()

    class SlowFirstRead(MemoryKeyring):
        def get_password(self, service, username):
            value = super().get_password(service, username)
            if not store_read.is_set():  # the load's read: the store is still empty
                store_read.set()
                time.sleep(0.1)  # a save may run here, unless loading holds the lock
            return value

    store = SlowFirstRead()
    found = []
    loader = threading.Thread(target=lambda: found.append(load_key(tmp_path, "openrouter", backend=store)))
    loader.start()
    store_read.wait(5)
    save_key(tmp_path, "openrouter", "sk-new", backend=store)  # stores it and removes the file's copy
    loader.join()
    assert found in (["sk-old"], ["sk-new"])  # never None: a key existed throughout
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-new"


class UnreadableKeyring(MemoryKeyring):
    """Refuses writes and, while broken, reads too; deleting still works."""

    broken = True

    def get_password(self, service, username):
        if self.broken:
            raise keyring.errors.KeyringLocked("locked")
        return super().get_password(service, username)

    def set_password(self, service, username, password):
        raise keyring.errors.PasswordSetError("locked")


def test_an_older_store_key_is_deleted_even_when_the_store_cannot_be_read(tmp_path):
    store = UnreadableKeyring()
    store.items[(SERVICE, "openrouter")] = "sk-old"
    assert save_key(tmp_path, "openrouter", "sk-new", backend=store)  # fell back, with a warning
    assert store.items == {}
    store.broken = False  # readable again later: the older key must not shadow the new one
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-new"


def test_nothing_to_delete_in_the_store_is_not_a_warning(tmp_path, caplog):
    assert save_key(tmp_path, "openrouter", KEY, backend=ReadOnlyKeyring())  # fell back; the store is empty
    assert "credential store" not in caplog.text
