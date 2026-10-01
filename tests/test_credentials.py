import stat

import keyring
import keyring.backends.fail
import keyring.backends.null
import pytest

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


@pytest.fixture(autouse=True)
def no_system_keyring(monkeypatch):
    # Any use of the default backend (the real Keychain on macOS) fails the test.
    def refuse():
        raise AssertionError("a test reached the system credential store")

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


def test_store_failing_on_read_uses_the_file(tmp_path):
    save_key(tmp_path, "openrouter", KEY, backend=keyring.backends.fail.Keyring())
    store = MemoryKeyring()
    assert load_key(tmp_path, "openrouter", backend=store) == KEY  # not in the store: the file
    store.items[(SERVICE, "openrouter")] = "sk-newer"
    assert load_key(tmp_path, "openrouter", backend=store) == "sk-newer"  # the store comes first


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


def test_unknown_or_unreadable_keys_are_none(tmp_path):
    assert load_key(tmp_path, "openrouter", backend=MemoryKeyring()) is None
    (tmp_path / "credentials.json").write_text("not json")
    assert load_key(tmp_path, "openrouter", backend=MemoryKeyring()) is None


def test_empty_key_is_refused(tmp_path):
    with pytest.raises(ValueError):
        save_key(tmp_path, "openrouter", "", backend=MemoryKeyring())


def test_legacy_configuration_is_never_read(tmp_path, monkeypatch):
    # The predecessor app kept its key in .env and the environment; neither is a source here.
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.chdir(tmp_path)
    assert load_key(tmp_path, "openrouter", backend=MemoryKeyring()) is None
    assert load_key(tmp_path, "openrouter", backend=keyring.backends.fail.Keyring()) is None
