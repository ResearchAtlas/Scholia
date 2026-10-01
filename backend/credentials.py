"""Provider keys: the OS credential store through keyring, or an owner-only file.

Keys never go into config.toml. When the credential store is unavailable, they
go to credentials.json in the data folder (0600), and the caller shows a warning.
A provider has a fallback entry only while its latest save could not use the
store, so that entry wins on reading; a later successful store save removes it.
"""

import json
import logging
import threading
from pathlib import Path

import keyring

from backend.settings import write_private

SERVICE = "AAB Research"  # the app's working name, which may change before the first release
FALLBACK_FILE = "credentials.json"
FALLBACK_WARNING = (
    "The system credential store is unavailable, so the key was saved in an "
    "owner-only file in the data folder instead."
)

_lock = threading.Lock()  # ponytail: one lock for all keys; key saves are rare
_log = logging.getLogger(__name__)


class CredentialsFileError(Exception):
    """credentials.json exists but cannot be read; it is left exactly as it is."""


def _store(backend):
    """The credential store, or None when it cannot start."""
    if backend is not None:
        return backend
    try:
        return keyring.get_keyring()
    except Exception:  # keyring raises whatever its configured backend's import raises
        return None


def _fallback(data_root):
    """Return (path, keys) for the owner-only fallback file; a missing file holds no keys.

    Raises CredentialsFileError if the file exists but cannot be read or is malformed,
    so it is never replaced by a file that drops the keys it still holds.
    """
    path = Path(data_root) / FALLBACK_FILE
    try:
        keys = json.loads(path.read_bytes())
    except FileNotFoundError:
        return path, {}
    except (OSError, ValueError, RecursionError) as error:  # unreadable, not JSON, or nested too deep
        raise CredentialsFileError(f"{FALLBACK_FILE} cannot be read; it was left unchanged") from error
    if not (isinstance(keys, dict) and all(isinstance(v, str) for v in keys.values())):
        raise CredentialsFileError(f"{FALLBACK_FILE} is malformed; it was left unchanged")
    return path, keys


def save_key(data_root, provider, key, backend=None):
    """Store a provider's key. Returns a warning if it went to the fallback file, else None.

    Raises CredentialsFileError if the key needs the fallback file and that file
    cannot be read or written. The file is left unchanged, and a key the store
    removed while failing is put back; the error says if that was impossible.
    """
    if not (isinstance(provider, str) and provider and isinstance(key, str) and key):
        raise ValueError("the provider and the key must be non-empty text")
    store = _store(backend)
    with _lock:
        stored, previous = False, None
        if store is not None:
            try:
                previous = store.get_password(SERVICE, provider)  # some stores delete it before adding
            except Exception:
                pass
            try:
                store.set_password(SERVICE, provider, key)
                stored = store.get_password(SERVICE, provider) == key  # some stores drop keys silently
            except Exception:  # any ordinary failure means the store is unavailable; interrupts propagate
                pass
        if stored:
            try:
                path, keys = _fallback(data_root)
            except CredentialsFileError:
                return None  # the key is safe in the store, and an unreadable file is never used
            if keys.pop(provider, None) is not None:
                write_private(path, json.dumps(keys).encode())  # leave no plain copy behind
            return None
        try:
            path, keys = _fallback(data_root)
            keys[provider] = key
            write_private(path, json.dumps(keys).encode())
        except (CredentialsFileError, OSError) as error:
            lost = "" if _restore(store, provider, previous) else (
                "; the previous key could not be put back in the credential store")
            raise CredentialsFileError(f"the key was not saved: {error}{lost}") from error
        return FALLBACK_WARNING


def _restore(store, provider, previous):
    """Put back a key a failed store write removed. Returns False only if it is still missing."""
    if store is None or previous is None:
        return True
    try:
        if store.get_password(SERVICE, provider) != previous:
            store.set_password(SERVICE, provider, previous)
        return store.get_password(SERVICE, provider) == previous
    except Exception:
        return False


def load_key(data_root, provider, backend=None):
    """Return a provider's key from the fallback file, else the credential store, else None."""
    with _lock:
        try:
            key = _fallback(data_root)[1].get(provider)
        except CredentialsFileError as error:
            _log.warning("%s; its keys are not used", error)
            key = None
    if key is None:
        store = _store(backend)
        try:
            key = store.get_password(SERVICE, provider) if store is not None else None
        except Exception:  # any ordinary failure means the store is unavailable; interrupts propagate
            key = None
    return key if isinstance(key, str) else None
