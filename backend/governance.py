"""What a project's sensitivity level allows (slice-1 spec sections 6.4 and 10; tickets 18 and 64).

- Normal may use any model route. Private may use OpenRouter models that the Private
  allowlist covers and that OpenRouter lists with a zero-retention endpoint, sent with
  provider.zdr = true and a key whose data settings the researcher confirmed; and declared
  local servers. Local only may use declared local servers. A review-locked project may use
  none: in M1 the lock refuses every generative function, until the venue rules relax it.
- The Private allowlist ships as a dated data file, private_routes.json. Route keys are
  "openrouter:<model id>", or "openrouter:*" for every model OpenRouter serves with zero
  retention; an exact key comes before "*", so a disabled exact entry excludes its model.
  Researcher edits are rows in private_routes: a shipped entry turned off or on, or an
  OpenRouter route added. A shipped entry's flags always come from the file. Re-checks are
  rows in list_checks, and an entry last checked more than six months ago is flagged.
- A key confirmation holds a salted fingerprint of the key (never the key), the version of
  the statement shown and an expiry six months on. It is current only for that key, that
  statement and until then; the salt is an owner-only file in the data folder.
- A declaration that a local server runs its models on this machine covers one exact origin.

The checks read the database through the caller's connection, so the outbound gate makes
them inside its decision transaction.
"""

import calendar
import functools
import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.db import new_id, utc_now
from backend.db.deletion import revoking_write  # noqa: F401  (tightening and locking write through it)
from backend.openrouter_client import get_model_metadata
from backend.outbound_gate import is_openrouter, local_origin
from backend.settings import write_private

LEVELS = ("normal", "private", "local_only")  # from least to most strict
ALLOWLIST = Path(__file__).resolve().parent / "private_routes.json"
# The versions of the statements the interface shows (frontend catalogs, keys privacy.key.* and
# privacy.local.*), recorded with each confirmation and declaration. Changing a statement's text
# needs a new version (tests/test_private_attestation.py holds each version's digest); a key
# confirmation of an earlier version is out of date, so the key is asked about again (section
# 6.4), while a declaration keeps the version it was made under, for the record.
KEY_STATEMENT = "2026-10-03"
LOCAL_STATEMENT = "2026-10-03"
CONFIRMATION_MONTHS = 6  # a key confirmation's life, and the age at which a list entry is flagged
SALT_FILE = "key-fingerprint.salt"
_salts: dict = {}
_salt_lock = threading.Lock()


def record(conn, event, project_id=None, **data):
    """One audit row: what happened, never content."""
    conn.execute("INSERT INTO audit_log (event, project_id, data) VALUES (?, ?, ?)",
                 (event, project_id, json.dumps(data)))


def months_later(stamp, months):
    """A time in the schema's form, months later (or earlier), on the same day or the month's last."""
    time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    index = time.month - 1 + months
    year, month = time.year + index // 12, index % 12 + 1
    time = time.replace(year=year, month=month, day=min(time.day, calendar.monthrange(year, month)[1]))
    return time.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The Private allowlist


@functools.cache
def shipped():
    """The shipped allowlist file, read once."""
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))


def allowlist(conn, now=None):
    """The allowlist's entries as they apply now: the shipped ones with the researcher's edits,
    each with its source, enabled, checked_on (the later of its own and a re-check) and stale."""
    entries = {e["route_key"]: {**e, "source": "shipped", "enabled": True} for e in shipped()["entries"]}
    for key, source, flags, features, terms, checked, exceptions, enabled in conn.execute(
            "SELECT route_key, source, required_flags, allowed_features, terms_url, checked_on, exceptions, enabled"
            " FROM private_routes ORDER BY route_key"):
        if source == "shipped" and key in entries:  # turned off or on; its flags stay the file's
            entries[key]["enabled"] = bool(enabled)
        elif source == "researcher" and key not in entries:
            entries[key] = {"route_key": key, "required_flags": json.loads(flags),
                            "allowed_features": json.loads(features), "terms_url": terms, "checked_on": checked,
                            "exceptions": json.loads(exceptions), "source": source, "enabled": bool(enabled)}
    rechecked = dict(conn.execute("SELECT entry_id, checked_on FROM list_checks WHERE list = 'private_routes'"))
    flagged_before = months_later(now or utc_now(), -CONFIRMATION_MONTHS)
    for key, entry in entries.items():
        entry["checked_on"] = max(entry.get("checked_on") or "", rechecked.get(key) or "") or None
        entry["stale"] = entry["checked_on"] is None or entry["checked_on"] < flagged_before
    return [entries[key] for key in sorted(entries)]


def covering_entry(entries, model):
    """The enabled allowlist entry covering an OpenRouter model, or None."""
    by_key = {entry["route_key"]: entry for entry in entries}
    entry = by_key.get(f"openrouter:{model}") or by_key.get("openrouter:*")
    return entry if entry is not None and entry["enabled"] else None


def private_flags(entries, model):
    """The request flags of the enabled entry covering an OpenRouter model, or None."""
    entry = covering_entry(entries, model)
    return entry["required_flags"] if entry is not None else None


def zero_retention(provider, model) -> bool:
    """Whether the provider's catalog, as read, lists a zero-retention endpoint for the model."""
    from backend.providers import Route  # providers reads this module's checks for the gate
    return (get_model_metadata(Route(provider, model)) or {}).get("supports_zdr") is True


# Declared local servers


def declared_origins(conn):
    """{origin: when it was declared} for every declared server."""
    found = {}
    for url, at in conn.execute("SELECT base_url, declared_at FROM local_declarations ORDER BY declared_at"):
        if origin := local_origin(url):
            found[origin] = at
    return found


def declare(conn, provider, base_url):
    """Record the researcher's declaration that the server at base_url runs its models on this
    machine, replacing any earlier one for its origin. Returns the origin, or None when the
    address is not on this machine (nothing is written)."""
    origin = local_origin(base_url)
    if origin is None:
        return None
    _withdraw(conn, origin)
    conn.execute("INSERT INTO local_declarations (id, provider, base_url, statement) VALUES (?, ?, ?, ?)",
                 (new_id(), provider, base_url, LOCAL_STATEMENT))
    record(conn, "local_declared", provider=provider, origin=origin, statement=LOCAL_STATEMENT)
    return origin


def withdraw(conn, provider, base_url):
    """Withdraw the declaration covering base_url's origin. Returns whether there was one."""
    origin = local_origin(base_url)
    if origin is None or not _withdraw(conn, origin):
        return False
    record(conn, "local_declaration_withdrawn", provider=provider, origin=origin)
    return True


def _withdraw(conn, origin):
    gone = [row_id for row_id, url in conn.execute("SELECT id, base_url FROM local_declarations")
            if local_origin(url) == origin]
    conn.executemany("DELETE FROM local_declarations WHERE id = ?", [(row_id,) for row_id in gone])
    return bool(gone)


def declaration(conn, base_url):
    """When the server at base_url was declared, or None."""
    origin = local_origin(base_url)
    found = [at for url, at in conn.execute("SELECT base_url, declared_at FROM local_declarations")
             if origin is not None and local_origin(url) == origin]
    return max(found) if found else None


# Key confirmations


def _salt(data_dir):
    path = Path(data_dir) / SALT_FILE
    with _salt_lock:
        salt = _salts.get(path)
        if salt is None:
            try:
                salt = path.read_bytes()
            except FileNotFoundError:
                salt = b""
            if len(salt) != 32:  # missing or damaged: a new one, so earlier confirmations no longer match
                salt = secrets.token_bytes(32)
                write_private(path, salt)
            _salts[path] = salt
        return salt


def fingerprint(data_dir, key):
    """A salted fingerprint of a key; the key itself is never stored."""
    return hmac.new(_salt(data_dir), key.encode(), hashlib.sha256).hexdigest()


def confirmation(conn, data_dir, key, now=None):
    """The key's data-settings confirmation: status is current, missing (none at all),
    other_key (none for this key), outdated (of an earlier statement) or expired; with the
    latest confirmation's times."""
    now = now or utc_now()
    mark = fingerprint(data_dir, key)
    rows = conn.execute("SELECT key_fingerprint, statement, confirmed_at, expires_at FROM key_attestations"
                        " ORDER BY confirmed_at DESC").fetchall()
    mine = [row for row in rows if hmac.compare_digest(row[0], mark)]
    current = [row for row in mine if row[1] == KEY_STATEMENT and row[3] > now]
    if current or not mine:
        status = "current" if current else "other_key" if rows else "missing"
        latest = (current or [None])[0]
    else:
        latest = mine[0]
        status = "outdated" if latest[1] != KEY_STATEMENT else "expired"
    return {"status": status, "statement": KEY_STATEMENT, "key": key_reference(mark),
            "confirmed_at": latest[2] if latest else None, "expires_at": latest[3] if latest else None}


def key_reference(mark):
    """A short reference to the key a confirmation is shown for, from its fingerprint: a
    confirmation names it, so one made from a card that showed another key is refused."""
    return mark[:16]


def key_attested(conn, data_dir, key) -> bool:
    return confirmation(conn, data_dir, key)["status"] == "current"


def confirm_key(conn, data_dir, provider, key):
    """Record the researcher's confirmation of the key's data settings, for six months."""
    now = utc_now()
    expires = months_later(now, CONFIRMATION_MONTHS)
    conn.execute(
        "INSERT INTO key_attestations (id, provider, key_fingerprint, statement, confirmed_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)", (new_id(), provider, fingerprint(data_dir, key), KEY_STATEMENT, now, expires))
    record(conn, "key_attested", provider=provider, statement=KEY_STATEMENT, expires_at=expires)


# What a project allows


@dataclass(frozen=True)
class Policy:
    """What one project allows, as read at one moment."""
    level: str
    locked: bool
    declared: dict  # declared origin -> when
    entries: tuple

    @property
    def zero_retention(self) -> bool:
        """Whether its OpenRouter requests carry provider.zdr = true."""
        return self.level == "private"

    def problem(self, provider, model):
        """Why the project may not send a step to this provider's model, or None."""
        if self.locked:
            return "review_locked"
        if self.level == "normal":
            return None
        origin = local_origin(provider.base_url)
        if origin is not None:  # loopback is only transport until the researcher declares it
            return None if origin in self.declared else "not_declared"
        if self.level == "private" and provider.is_openrouter and is_openrouter(provider.base_url):
            if private_flags(self.entries, model) is None or not zero_retention(provider, model):
                return "private_route_not_allowed"
            return None
        return "route_not_allowed"

    def terms(self, provider, model):
        """The retention terms a step sent to this provider's model under this policy is covered
        by, for its record (ticket 18's provenance): the level, and OpenRouter's zero retention
        with its allowlist entry and the date its terms were checked, or the declaration of a
        server on this Mac."""
        origin = local_origin(provider.base_url)
        if self.level != "normal" and origin in self.declared:
            return {"level": self.level, "declared_origin": origin, "declared_at": self.declared[origin]}
        entry = covering_entry(self.entries, model) if self.zero_retention and provider.is_openrouter else None
        if entry is not None:
            return {"level": self.level, "zero_retention": True, "allowlist_entry": entry["route_key"],
                    "terms_url": entry["terms_url"], "checked_on": entry["checked_on"]}
        return {"level": self.level}

    def allows_provider(self, provider) -> bool:
        """Whether some model of the provider may be allowed: the defaults pass over the rest."""
        if self.locked:
            return False
        if self.level == "normal":
            return True
        origin = local_origin(provider.base_url)
        if origin is not None:
            return origin in self.declared
        return self.level == "private" and provider.is_openrouter and is_openrouter(provider.base_url)


def policy(conn, project_id):
    """The project's Policy, or None when there is no such project."""
    row = conn.execute("SELECT sensitivity, review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    return Policy(row[0], bool(row[1]), declared_origins(conn), tuple(allowlist(conn)))


def revoke_running(conn, project_id):
    """Revoke every running run of the project, in the caller's transaction (a tightening, or the
    review lock): its next dispatch is refused. Returns their ids, for the harness to stop them."""
    ids = [run_id for (run_id,) in conn.execute(
        "SELECT id FROM runs WHERE project_id = ? AND status = 'running' ORDER BY id", (project_id,))]
    conn.execute("UPDATE runs SET cancel_reason = 'revoked' WHERE project_id = ? AND status = 'running'", (project_id,))
    return ids
