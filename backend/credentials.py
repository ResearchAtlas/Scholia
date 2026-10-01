"""Provider keys: the OS credential store through keyring, or an owner-only file.

Keys never go into config.toml. When the credential store is unavailable, they
go to credentials.json in the data folder (0600), and the caller shows a warning.
A provider has a fallback entry only while its latest save could not use the
store, so that entry wins on reading; a later successful store save removes it.
"""

import json
import threading
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from backend.settings import write_private

SERVICE = "AAB Research"  # the app's working name, which may change before the first release
FALLBACK_FILE = "credentials.json"
FALLBACK_WARNING = (
    "The system credential store is unavailable, so the key was saved in an "
    "owner-only file in the data folder instead."
)

_lock = threading.Lock()  # ponytail: one lock for all keys; key saves are rare


def _store(backend):
    """The credential store, or None when it cannot start."""
    if backend is not None:
        return backend
    try:
        return keyring.get_keyring()
    except Exception:  # keyring raises whatever its configured backend's import raises
        return None


def _fallback(data_root):
    """Return (path, keys) for the owner-only fallback file; unreadable content counts as empty."""
    path = Path(data_root) / FALLBACK_FILE
    try:
        keys = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        keys = {}
    return path, keys if isinstance(keys, dict) else {}


def save_key(data_root, provider, key, backend=None):
    """Store a provider's key. Returns a warning if it went to the fallback file, else None."""
    if not key:
        raise ValueError("the key is empty")
    store = _store(backend)
    with _lock:
        stored = False
        if store is not None:
            try:
                store.set_password(SERVICE, provider, key)
                stored = store.get_password(SERVICE, provider) == key  # some stores drop keys silently
            except KeyringError:
                pass
        path, keys = _fallback(data_root)
        if stored:
            if keys.pop(provider, None) is not None:
                write_private(path, json.dumps(keys).encode())  # leave no plain copy behind
            return None
        keys[provider] = key
        write_private(path, json.dumps(keys).encode())
        return FALLBACK_WARNING


def load_key(data_root, provider, backend=None):
    """Return a provider's key from the fallback file, else the credential store, else None."""
    with _lock:
        key = _fallback(data_root)[1].get(provider)
    if not isinstance(key, str):
        store = _store(backend)
        try:
            key = store.get_password(SERVICE, provider) if store is not None else None
        except KeyringError:
            key = None
    return key if isinstance(key, str) else None
