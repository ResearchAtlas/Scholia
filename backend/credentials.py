"""Provider keys: the OS credential store through keyring, or an owner-only file.

Keys never go into config.toml. The credential store comes first. Only when it
cannot be used does a key go to credentials.json in the data folder (0600), and
the caller shows a warning. Reading takes the store's key when it has one, and
the file's otherwise.
"""

import json
import logging
import threading
from pathlib import Path

import keyring

from backend.settings import write_private

SERVICE = "io.github.researchatlas.scholia"  # the Keychain service, the same as the bundle id
FALLBACK_FILE = "credentials.json"
FALLBACK_WARNING = (
    "The system credential store is unavailable, so the key was saved in an "
    "owner-only file in the data folder instead."
)

_lock = threading.Lock()  # ponytail: one lock for all keys; key saves are rare
_log = logging.getLogger(__name__)


class CredentialsFileError(Exception):
    """The key could not be saved in the fallback file, which is left exactly as it was."""


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

    Raises CredentialsFileError if the store cannot be used and the fallback file
    cannot be read or written; the file is left unchanged. A save that raises may
    leave no key stored at all, because some stores (the macOS one among them)
    replace a key by deleting it first; the researcher then enters the key again.
    Interrupts such as KeyboardInterrupt propagate.
    """
    if not (isinstance(provider, str) and provider and isinstance(key, str) and key):
        raise ValueError("the provider and the key must be non-empty text")
    store = _store(backend)
    with _lock:
        stored = False
        if store is not None:
            try:
                store.set_password(SERVICE, provider, key)
                stored = store.get_password(SERVICE, provider) == key  # some stores drop keys silently
            except Exception:  # any ordinary failure means the store is unavailable
                pass
        if stored:
            try:
                path, keys = _fallback(data_root)
                if keys.pop(provider, None) is not None:
                    write_private(path, json.dumps(keys).encode())  # leave no plain copy behind
            except (CredentialsFileError, OSError):
                # Harmless for reading, which prefers the store, but a plain copy remains.
                _log.warning("an old copy of a key could not be removed from %s", FALLBACK_FILE)
            return None
        try:
            path, keys = _fallback(data_root)
            keys[provider] = key
            write_private(path, json.dumps(keys).encode())
        except (CredentialsFileError, OSError) as error:
            raise CredentialsFileError(f"the key was not saved: {error}") from error
        if store is not None:
            try:  # an older key left in the store would be read before this one
                store.delete_password(SERVICE, provider)
            except Exception:  # nothing to delete, or the store refused
                try:
                    lingering = store.get_password(SERVICE, provider)
                except Exception:
                    lingering = True  # cannot tell
                if lingering:
                    _log.warning("the credential store could not be cleared of an older key; it may be "
                                 "read instead of the one in %s", FALLBACK_FILE)
        return FALLBACK_WARNING


def load_key(data_root, provider, backend=None):
    """Return a provider's key from the credential store, else the fallback file, else None."""
    store = _store(backend)
    with _lock:  # both reads in one step, so a concurrent save cannot fall between them
        if store is not None:
            try:
                key = store.get_password(SERVICE, provider)
            except Exception:  # any ordinary failure means the store is unavailable
                key = None
            if isinstance(key, str) and key:  # an empty value is no key; save_key never stores one
                return key
        try:
            key = _fallback(data_root)[1].get(provider)
        except CredentialsFileError as error:
            _log.warning("%s; its keys are not used", error)
            key = None
    return key if isinstance(key, str) and key else None
