"""Settings files: the personal config.toml and one per project.

<data folder>/config.toml is personal; <data folder>/projects/<id>/config.toml
belongs to a project. Both are read and written with tomlkit, so comments,
formatting and unknown keys survive a save.
"""

import copy
import hashlib
import math
import os
import re
import stat
import tempfile
import threading
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit


_INT64 = range(-2**63, 2**63)  # TOML integers are 64-bit; tomlkit reads larger ones too


def _int(low, high=None):
    return lambda v: type(v) is int and v in _INT64 and v >= low and (high is None or v <= high)


def _number(above, below=None):
    return lambda v: (((type(v) is int and v in _INT64) or (type(v) is float and math.isfinite(v)))
                      and v > above and (below is None or v < below))


def _choice(*options):
    return lambda v: type(v) is str and v in options


def _text(v):
    return type(v) is str


def _flag(v):
    return type(v) is bool


def _base_url(v):
    """An https URL, or plain http only to this machine (a local model server): keys go to
    this address, so they never travel in clear text over a network. No user info,
    query or fragment."""
    if type(v) is not str:
        return False
    try:
        parts = urllib.parse.urlsplit(v)
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment or not parts.hostname:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"))


def _texts(max_items=None):
    return lambda v: (type(v) is list and all(type(x) is str for x in v)
                      and (max_items is None or len(v) <= max_items))


_WINDOW = _int(4096)  # the smallest supported model window

# ponytail: key names for limits, context and retrieval follow the starting-values
# table; later PRs add keys here as they start reading them.
_LIMITS = {
    "agent_steps": (12, _int(1)),
    "tool_calls": (40, _int(1)),
    "turn_minutes": (15, _int(1)),
    "requests_in_flight": (8, _int(1)),
    "retries": (2, _int(0)),
}
_CONTEXT = {  # token budgets for a 128K window
    "system_rules": (1500, _int(0)),
    "skill_catalog": (1000, _int(0)),
    "active_skills": (6000, _int(0)),
    "agents_md": (8000, _int(0)),
    "project_state": (1500, _int(0)),
    "user_memory": (1000, _int(0)),
    "research_evidence": (12000, _int(0)),
    "inference": (1000, _int(0)),
    "history": (16000, _int(0)),
    "current_request": (8000, _int(0)),
    "conflicts": (500, _int(0)),
    "output_limit": (8000, _int(1024)),  # the step's output limit, in tokens
    "margin_percent": (5, _int(0, 100)),
    "compact_at_percent": (90, _int(1, 100)),
}
_RETRIEVAL = {
    "passage_chars": (2000, _int(1)),
    "bm25_candidates": (50, _int(1)),
    "dense_candidates": (50, _int(1)),
    "rrf_k": (60, _int(1)),
    "rerank_top": (24, _int(1)),
    "keep": (8, _int(1)),
    "embedding_batch": (32, _int(1)),
    "judge_pairs": (40, _int(1)),
    "rules_ms": (100, _int(1)),
    "router_ms": (2000, _int(1)),
    "memory_ms": (200, _int(1)),
    "hybrid_ms": (500, _int(1)),
    "rerank_ms": (500, _int(1)),
    "assembly_ms": (100, _int(1)),
}

# Key path -> (default, check). "*" matches any one name (a provider or model id).
# A default of None means unset.
PERSONAL = {
    ("ui", "language"): ("system", _choice("system", "en", "zh-CN")),
    ("ui", "follow_up"): ("steer", _choice("steer", "queue")),
    ("ui", "layout", "sidebar_width"): (248, _int(180, 360)),
    ("ui", "layout", "sidebar_open"): (True, _flag),
    ("ui", "layout", "panel_share"): (0.5, _number(0, 1)),
    ("providers", "*", "kind"): (None, _choice("openrouter", "openai-compatible")),
    ("providers", "*", "base_url"): (None, _base_url),
    # Which of a provider's models are offered: "recommended", "all", or a list of model ids
    # (the stage walk's Recommended, All or Pick); unset is Recommended on OpenRouter, else All.
    ("providers", "*", "models"): (None, lambda v: v in ("recommended", "all")
                                   or (type(v) is list and all(type(x) is str and visible(x) for x in v))),
    ("providers", "*", "enabled"): (None, _flag),  # false is Off: kept, but never called
    ("providers", "*", "default_window"): (None, _WINDOW),
    ("providers", "*", "windows", "*"): (None, _WINDOW),
    ("models", "default"): ("auto", _text),
    ("models", "efforts", "*"): (None, _text),
    ("models", "council"): (None, lambda v: _text(v) or _texts()(v)),  # one model or the members
    **{("models", role): (None, _text) for role in ("chairman", "router", "judge", "extractor")},
    ("subagents", "models"): ([], _texts(max_items=5)),
    ("subagents", "at_once"): (3, _int(1)),
    ("subagents", "tool_calls"): (30, _int(1)),
    ("subagents", "effort_cap"): (None, _text),
    ("budget", "conversation_usd"): (10, _number(0)),
    ("budget", "notice_percents"): ([50, 85, 90], lambda v: type(v) is list and all(_int(1, 100)(x) for x in v)),
    ("privacy", "trim_bodies_after_days"): (0, _int(0)),
    ("helper", "idle_stop_minutes"): (10, _int(1)),
    ("helper", "model_source"): (None, _text),
    **{("limits", k): spec for k, spec in _LIMITS.items()},
    **{("context", k): spec for k, spec in _CONTEXT.items()},
    **{("retrieval", k): spec for k, spec in _RETRIEVAL.items()},
    # No defined shape yet: a table at the root, returned as given. "**" matches below it.
    ("zotero", "**"): (None, None),
    ("discovery", "**"): (None, None),
    ("extensions", "*", "**"): (None, None),  # each extension's schema validates its own table
}

PROJECT = {
    **{("project", k): (None, _text) for k in ("target_venue", "citation_style", "citation_style_zh", "template")},
    ("project", "budget_usd"): (50, _number(0)),
    ("ui", "panel"): (None, _choice("none", "library", "manuscript")),
    ("models", "default"): (None, _text),
    **{("limits", k): (None, check) for k, (_, check) in _LIMITS.items()},  # overrides
    ("memory", "enabled"): (None, _flag),
    **{("tools", k): (None, _texts()) for k in ("allow", "ask", "deny")},
    ("extensions", "enabled"): (None, _texts()),
    ("mcp", "**"): (None, None),
    ("mcp_server", "**"): (None, None),
}


class SettingsChanged(Exception):
    """The file changed on disk since it was loaded: reload, ask, then save again."""


_save_lock = threading.Lock()  # ponytail: one lock for every settings file; saves are rare


def _make_private_dirs(folder):
    """Create folder and any missing parents owner-only (0700)."""
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for path in reversed(missing):
        path.mkdir(mode=0o700, exist_ok=True)


def write_private(path, data):
    """Replace path atomically with data, owner-only (0600).

    An existing file keeps its permissions, narrowed to at most 0600, never broadened.
    """
    path = Path(path)
    _make_private_dirs(path.parent)
    try:
        mode = stat.S_IMODE(path.stat().st_mode) & 0o600
    except FileNotFoundError:
        mode = 0o600
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        os.unlink(temp)
        raise
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)  # makes the rename itself durable
    finally:
        os.close(folder)


@dataclass
class Settings:
    path: Path
    schema: dict
    label: str
    values: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    # The same problems for an interface to state in its own language: {"key": ..., "line": ...};
    # key is None when the whole file could not be used.
    problems: list = field(default_factory=list)
    _digest: str | None = None
    _broken: bool = False

    def save(self, updates):
        """Write updates ({"section.key": value}) to the file, keeping comments and unknown keys.

        A value of None removes the key, so the setting returns to its default.

        Re-reads the file first and raises SettingsChanged if it changed since it
        was loaded. Raises ValueError, writing nothing, for an invalid value, a
        secret, a personal setting in a project file, or a file that is not valid TOML.
        """
        with _save_lock:
            raw = _read(self.path)
            if _digest(raw) != self._digest:
                raise SettingsChanged(f"{self.label} changed on disk since it was loaded")
            if self._broken:
                raise ValueError(f"{self.label} is not valid TOML; fix it before saving")
            doc = tomlkit.parse(raw.decode("utf-8")) if raw else tomlkit.document()
            for dotted, value in updates.items():
                path = _split_key(dotted)
                if value is None:  # clear the key, back to its default; nothing else changes
                    node = doc
                    for part in path[:-1]:
                        node = node.get(part) if isinstance(node, dict) else None
                    if isinstance(node, dict) and path[-1] in node:
                        del node[path[-1]]
                    continue
                # Checked as tomlkit will write it: tuples become arrays, tomlkit items plain values,
                # and anything TOML cannot hold raises ValueError here.
                value = tomlkit.item(value).unwrap()
                # A dict is applied leaf by leaf, so the rest of an existing table and its comments stay.
                for leaf, leaf_value in _leaves({path[-1]: value}, path[:-1]):
                    pattern, problem = _check(self.schema, leaf, leaf_value)
                    if problem:
                        raise ValueError(f"{'.'.join(leaf[:len(pattern)])} {problem}")
                    node = doc
                    for part in leaf if isinstance(leaf_value, dict) else leaf[:-1]:
                        if part not in node:
                            node[part] = tomlkit.table()
                        node = node[part]
                        if not isinstance(node, dict):
                            raise ValueError(f"{dotted}: {part} is not a table in {self.label}")
                    if not isinstance(leaf_value, dict):
                        node[leaf[-1]] = leaf_value
            data = doc.as_string().encode("utf-8")
            write_private(self.path, data)
            _parse(self, data)


def _defaults(schema):
    values = {}
    for path, (default, _) in schema.items():
        if default is not None:
            node = values
            for part in path[:-1]:
                node = node.setdefault(part, {})
            node[path[-1]] = copy.deepcopy(default)
    return values


def _digest(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _read(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _leaves(node, prefix=()):
    """Yield (key path, value) for every non-table value and every empty table under node, in order."""
    stack = [(prefix, iter(node.items()))]  # a loop, not recursion: nesting depth is the file's choice
    while stack:
        path, items = stack[-1]
        for key, value in items:
            if isinstance(value, dict) and value:
                stack.append((path + (key,), iter(value.items())))
                break
            yield path + (key,), value
        else:
            stack.pop()


_BLANK_LETTERS = set("\u115f\u1160\u3164\uffa0\u2800")  # Hangul fillers and the blank Braille pattern


def visible(text):
    """text trimmed, or None when it holds no visible character: only separators, format and
    control characters (a zero-width space), marks with nothing to attach to (a variation
    selector), or the few letters that draw nothing.
    ponytail: by Unicode category plus a short list; a font can still draw a character blank."""
    if not isinstance(text, str) or not any(
            unicodedata.category(c)[0] not in "CMZ" and c not in _BLANK_LETTERS for c in text):
        return None
    return text.strip()


def _match(schema, path):
    """Return (pattern, spec) for path; the spec's check also catches a wrong shape."""
    for pattern, spec in schema.items():
        if pattern[-1] == "**":
            head = pattern[:-1]
            if all(p in ("*", k) for p, k in zip(head, path)):
                if len(path) > len(head):
                    return path, (None, lambda v: True)  # anything below the section's root
                return path, (None, lambda v: isinstance(v, dict))  # the root and above are tables
        elif all(p in ("*", k) for p, k in zip(pattern, path)):
            if len(pattern) == len(path):
                return pattern, spec
            if len(pattern) > len(path):  # an empty table where a table belongs is fine
                return path, (None, lambda v: isinstance(v, dict))
            return pattern, (None, lambda v: False)  # a table where a value belongs
    return None, None


# Field names that hold secrets; keys live in the credential store, never in config.toml.
# ponytail: detection by name is a safeguard, not a guarantee; a secret under an
# ordinary name passes.
_SECRET = re.compile(
    r"(^|[\s._-])(api_?key|key|token|secret|password|passwd|passphrase|auth|authorization"
    r"|bearer|cookie|credential)s?$",  # each name, singular or plural
    re.IGNORECASE,
)


def _secret_name(name):
    return _SECRET.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))) is not None  # apiKey -> api_Key


def _hides_secret(value):
    """True if a list or table value holds a secret-like field name at any depth."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(_secret_name(k) for k in item):
                return True
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def _identifiers(schema, path):
    """The positions in path that are identifiers: the schema's "*" positions (provider
    names, model ids and extension ids)."""
    identifiers = set()
    for pattern in schema:
        for i, (p, k) in enumerate(zip(pattern, path)):  # the part of path that follows this pattern
            if p not in ("*", k):
                break
            if p == "*":
                identifiers.add(i)
    return identifiers


def _field_names(schema, path):
    """The parts of path that are field names, leaving out identifiers. Every part that is
    not an identifier, tables inside open sections included, is a field name."""
    identifiers = _identifiers(schema, path)
    return [part for i, part in enumerate(path) if i not in identifiers]


def _check(schema, path, value):
    """Return (pattern, problem) for one key path and value.

    pattern is the schema entry that path falls under (None for unknown keys),
    and problem says why the value cannot be used (None when it can).
    """
    # Secrets a researcher wrote by hand are ignored, with a warning, but kept in the
    # file on save like any other content of theirs; only the app's own writes are refused.
    if schema is PROJECT and (path[0] in ("providers", "keys", "subagents")
                              or (_match(PROJECT, path)[0] is None and _match(PERSONAL, path)[0] is not None)):
        return path, "is a personal setting and cannot be set in a project file"
    if any(_secret_name(name) for name in _field_names(schema, path)) or _hides_secret(value):
        return path, "looks like a secret; keys belong in the credential store"
    if any(visible(path[i]) is None for i in _identifiers(schema, path) if i < len(path)):
        return path, "needs a name with a visible character"
    pattern, spec = _match(schema, path)
    if spec is not None and not spec[1](value):
        return pattern, "is not valid"
    return pattern, None


_QUOTED = r"""(?:"(?:[^"\\]|\\.)*"|'[^']*')"""
_HEADER = re.compile(rf"\[\[?((?:{_QUOTED}|[^\]\"'])*)\]")
_KEY = re.compile(rf"((?:{_QUOTED}|[^=#\"'])+)=")


def _split_key(key):
    """Decode a dotted key, quoted parts and escapes included, exactly as tomlkit does.

    Raises ValueError (tomlkit's ParseError) for text that is not a TOML key.
    """
    node, path = tomlkit.parse(f"{key} = 0").unwrap(), ()
    while isinstance(node, dict):  # one name per level
        (part, node), = node.items()
        path += (part,)
    return path


def _scan_value(text, open_quote, depth):
    """Track whether a multi-line string or array is still open at the end of a line."""
    i = 0
    while i < len(text):
        if open_quote == '"""':  # a basic string: a backslash escapes the next character
            while i < len(text) and not text.startswith('"""', i):
                i += 2 if text[i] == "\\" else 1
            if i >= len(text):
                return open_quote, depth
            i, open_quote = i + 3, None
            continue
        if open_quote:  # a literal string has no escapes
            end = text.find(open_quote, i)
            if end < 0:
                return open_quote, depth
            i, open_quote = end + 3, None
            continue
        if text.startswith(('"""', "'''"), i):
            open_quote, i = text[i:i + 3], i + 3
            continue
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "'":
            end = text.find("'", i + 1)
            i = len(text) if end < 0 else end
        elif c == "#":
            break
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        i += 1
    return open_quote, depth


def _key_lines(text):
    """Map each table header and key path in a TOML text to its line number.

    ponytail: a line scanner, not a parser, used only to name lines in warnings.
    Keys inside inline tables take the line of their enclosing key.
    """
    lines, table, open_quote, depth = {}, (), None, 0
    for number, line in enumerate(text.split("\n"), 1):  # TOML ends lines with \n or \r\n only
        if open_quote or depth > 0:
            open_quote, depth = _scan_value(line, open_quote, depth)
            continue
        stripped = line.strip()
        header, key = _HEADER.match(stripped), _KEY.match(stripped)
        try:
            if header:
                table = _split_key(header.group(1))
                lines.setdefault(table, number)
            elif key:
                lines.setdefault(table + _split_key(key.group(1)), number)
                open_quote, depth = _scan_value(stripped[key.end():], None, 0)
        except ValueError:
            continue  # not a key after all; its warnings fall back to the enclosing table's line
    return lines


def _line(lines, path):
    for i in range(len(path), 0, -1):
        if path[:i] in lines:
            return lines[path[:i]]
    return "?"


def _parse(settings, raw):
    """Fill settings.values and settings.warnings from the file's bytes; never raises for content."""
    try:
        _fill(settings, raw)
    except RecursionError:  # valid TOML can nest deeper than tomlkit's recursive parse and unwrap follow
        _fill(settings, None)
        settings._digest, settings._broken = _digest(raw), True
        settings.warnings.append(f"{settings.label}: nested too deeply to read; using the defaults")
        settings.problems.append({"key": None, "line": None})


def _fill(settings, raw):
    settings._digest = _digest(raw)
    settings.values = _defaults(settings.schema)
    settings.warnings = []
    settings.problems = []
    settings._broken = False
    if raw is None:
        return
    try:
        text = raw.decode("utf-8")
        data = tomlkit.parse(text).unwrap()
    except UnicodeDecodeError:
        settings._broken = True
        settings.warnings.append(f"{settings.label}: not UTF-8 text; using the defaults")
        settings.problems.append({"key": None, "line": None})
        return
    except tomlkit.exceptions.ParseError as error:
        settings._broken = True
        settings.warnings.append(f"{settings.label} line {error.line}: not valid TOML; using the defaults")
        settings.problems.append({"key": None, "line": error.line})
        return
    lines = _key_lines(text)
    for path, value in _leaves(data):
        pattern, problem = _check(settings.schema, path, value)
        if problem:
            key = ".".join(path[:len(pattern)])
            default = settings.schema.get(pattern, (None,))[0]
            fallback = "ignored" if default is None else f"using the default {default!r}"
            settings.warnings.append(f"{settings.label} line {_line(lines, path)}: {key} {problem}; {fallback}")
            settings.problems.append({"key": key, "line": _line(lines, path)})
        elif pattern:
            node = settings.values
            for part in path[:-1]:
                node = node.setdefault(part, {})
            if isinstance(value, dict):  # an empty table: keep any defaults under it
                node.setdefault(path[-1], {})
            else:
                node[path[-1]] = value


def _project_folder(data_root, project_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
        raise ValueError("invalid project id")
    return Path(data_root) / "projects" / project_id


def load_settings(data_root, project_id=None):
    """Load the personal settings, or a project's when project_id is given.

    Never raises for the file's content: problems become warnings and defaults.
    """
    if project_id is None:
        settings = Settings(Path(data_root) / "config.toml", PERSONAL, "config.toml")
    else:
        settings = Settings(_project_folder(data_root, project_id) / "config.toml", PROJECT, "project config.toml")
    try:
        _parse(settings, _read(settings.path))
    except OSError:
        _parse(settings, None)
        settings._broken = True
        settings.warnings.append(f"{settings.label} could not be read; using the defaults")
        settings.problems.append({"key": None, "line": None})
    return settings


INSTRUCTIONS_CAP = 32 * 1024  # bytes of UTF-8, personal and project combined


def load_instructions(data_root, project_id=None):
    """Return (text, warnings): the personal AGENTS.md, then the project's, capped at 32 KiB."""
    data, warnings = _joined_instructions(data_root, project_id)
    if len(data) > INSTRUCTIONS_CAP:
        warnings.append("Instructions exceed the 32 KiB combined cap; only the first 32 KiB are used")
        data = data[:INSTRUCTIONS_CAP]
    return data.decode("utf-8", errors="ignore"), warnings  # ignore drops a character cut in half


def instructions_size(data_root, project_id=None) -> int:
    """The combined instructions' size in UTF-8 bytes before the cap cuts them, as the cap counts it."""
    return len(_joined_instructions(data_root, project_id)[0])


def _joined_instructions(data_root, project_id):
    """The personal and the project's AGENTS.md as one UTF-8 text, uncut, with warnings."""
    paths = [Path(data_root) / "AGENTS.md"]
    if project_id is not None:
        paths.append(_project_folder(data_root, project_id) / "AGENTS.md")
    parts, warnings = [], []
    for path in paths:
        try:
            raw = _read(path)
        except OSError:
            warnings.append(f"{path.name} could not be read; it was left out")
            continue
        if raw is None:
            continue
        try:
            parts.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            parts.append(raw.decode("utf-8", errors="replace"))
            warnings.append(f"{path.name} is not UTF-8 text; unreadable characters were replaced")
    return "\n\n".join(parts).encode("utf-8"), warnings
