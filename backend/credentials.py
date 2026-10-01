"""Provider keys: the OS credential store through keyring, or an owner-only file.

Keys never go into config.toml. When the credential store is unavailable, they
go to credentials.json in the data folder (0600), and the caller shows a warning.
"""

import json
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
    backend = backend or keyring.get_keyring()
    try:
        backend.set_password(SERVICE, provider, key)
        stored = backend.get_password(SERVICE, provider) == key  # some stores drop keys silently
    except KeyringError:
        stored = False
    path, keys = _fallback(data_root)
    if stored:
        if keys.pop(provider, None) is not None:
            write_private(path, json.dumps(keys).encode())  # leave no plain copy behind
        return None
    keys[provider] = key
    write_private(path, json.dumps(keys).encode())
    return FALLBACK_WARNING


def load_key(data_root, provider, backend=None):
    """Return a provider's key from the credential store, else the fallback file, else None."""
    backend = backend or keyring.get_keyring()
    try:
        key = backend.get_password(SERVICE, provider)
    except KeyringError:
        key = None
    if key is None:
        key = _fallback(data_root)[1].get(provider)
    return key if isinstance(key, str) else None
