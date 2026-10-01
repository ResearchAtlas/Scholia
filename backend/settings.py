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
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit


def _int(low, high=None):
    return lambda v: type(v) is int and v >= low and (high is None or v <= high)


def _number(above, below=None):
    return lambda v: (type(v) in (int, float) and math.isfinite(v)
                      and v > above and (below is None or v < below))


def _choice(*options):
    return lambda v: type(v) is str and v in options


def _text(v):
    return type(v) is str


def _flag(v):
    return type(v) is bool


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
    "output_tokens": (8000, _int(1024)),
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
    ("providers", "*", "base_url"): (None, _text),
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
    _digest: str | None = None
    _broken: bool = False

    def save(self, updates):
        """Write updates ({"section.key": value}) to the file, keeping comments and unknown keys.

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
                for leaf, leaf_value in list(_leaves({path[-1]: value}, path[:-1])) or [(path, value)]:
                    pattern, problem = _check(self.schema, leaf, leaf_value)
                    if problem:
                        raise ValueError(f"{'.'.join(leaf[:len(pattern)])} {problem}")
                node = doc
                for part in path[:-1]:
                    if part not in node:
                        node[part] = tomlkit.table()
                    node = node[part]
                    if not isinstance(node, dict):
                        raise ValueError(f"{dotted}: {part} is not a table in {self.label}")
                node[path[-1]] = value
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
    """Yield (key path, value) for every non-table value under node."""
    for key, value in node.items():
        if isinstance(value, dict):
            yield from _leaves(value, prefix + (key,))
        else:
            yield prefix + (key,), value


def _match(schema, path):
    """The schema entry for path, or a problem when the file's shape disagrees with the schema."""
    for pattern, spec in schema.items():
        n = min(len(pattern), len(path))
        if all(p in ("*", k) for p, k in zip(pattern, path)):
            if len(pattern) == len(path):
                return pattern, spec
            # A table where a value belongs, or a value where a table belongs.
            return pattern[:n], (None, lambda v: False)
    return None, None


# Names that hold secrets; keys live in the credential store, never in config.toml.
_SECRET = re.compile(r"(^|[_-])(api_?key|key|token|secret|password)$", re.IGNORECASE)


def _check(schema, path, value):
    """Return (pattern, problem) for one key path and value.

    pattern is the schema entry that path falls under (None for unknown keys),
    and problem says why the value cannot be used (None when it can).
    """
    if schema is PROJECT and path[0] in ("providers", "subagents"):
        return path, "is a personal setting and cannot be set in a project file"
    if any(_SECRET.search(part) for part in path):
        return path, "looks like a secret; keys belong in the credential store"
    pattern, spec = _match(schema, path)
    if spec is not None and not spec[1](value):
        return pattern, "is not valid"
    return pattern, None


_KEY_PART = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([^.\s]+)')
_HEADER = re.compile(r"""\[\[?\s*((?:"[^"]*"|'[^']*'|[^\]"'])*)\]""")


def _split_key(key):
    return tuple(a or b or c for a, b, c in _KEY_PART.findall(key))


def _scan_value(text, open_quote, depth):
    """Track whether a multi-line string or array is still open at the end of a line."""
    i = 0
    while i < len(text):
        if open_quote:
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
    for number, line in enumerate(text.splitlines(), 1):
        if open_quote or depth > 0:
            open_quote, depth = _scan_value(line, open_quote, depth)
            continue
        stripped = line.strip()
        header = _HEADER.match(stripped)
        if header:
            table = _split_key(header.group(1))
            lines.setdefault(table, number)
        elif "=" in stripped and not stripped.startswith("#"):
            key, _, rest = stripped.partition("=")
            lines.setdefault(table + _split_key(key), number)
            open_quote, depth = _scan_value(rest, None, 0)
    return lines


def _line(lines, path):
    for i in range(len(path), 0, -1):
        if path[:i] in lines:
            return lines[path[:i]]
    return "?"


def _parse(settings, raw):
    """Fill settings.values and settings.warnings from the file's bytes."""
    settings._digest = _digest(raw)
    settings.values = _defaults(settings.schema)
    settings.warnings = []
    settings._broken = False
    if raw is None:
        return
    try:
        text = raw.decode("utf-8")
        data = tomlkit.parse(text).unwrap()
    except UnicodeDecodeError:
        settings._broken = True
        settings.warnings.append(f"{settings.label}: not UTF-8 text; using the defaults")
        return
    except tomlkit.exceptions.ParseError as error:
        settings._broken = True
        settings.warnings.append(f"{settings.label} line {error.line}: not valid TOML; using the defaults")
        return
    lines = _key_lines(text)
    for path, value in _leaves(data):
        pattern, problem = _check(settings.schema, path, value)
        if problem:
            key = ".".join(path[:len(pattern)])
            default = settings.schema.get(pattern, (None,))[0]
            fallback = "ignored" if default is None else f"using the default {default!r}"
            settings.warnings.append(f"{settings.label} line {_line(lines, path)}: {key} {problem}; {fallback}")
        elif pattern:
            node = settings.values
            for part in path[:-1]:
                node = node.setdefault(part, {})
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
    return settings


INSTRUCTIONS_CAP = 32 * 1024  # bytes of UTF-8, personal and project combined


def load_instructions(data_root, project_id=None):
    """Return (text, warnings): the personal AGENTS.md, then the project's, capped at 32 KiB."""
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
    data = "\n\n".join(parts).encode("utf-8")
    if len(data) > INSTRUCTIONS_CAP:
        warnings.append("Instructions exceed the 32 KiB combined cap; only the first 32 KiB are used")
        data = data[:INSTRUCTIONS_CAP]
    return data.decode("utf-8", errors="ignore"), warnings  # ignore drops a character cut in half
