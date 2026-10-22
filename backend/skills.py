"""The first-party skills, `scholia.skills`, bundled read-only in the app (slice-1 spec sections 6.2 and 11).

Each skill is a folder in the Agent Skills format under backend/extensions/scholia.skills: `SKILL.md` (front
matter `name` and `description`, then the body), optional `references/` and `assets/` files, and for the
interface its Chinese text, `SKILL.zh-CN.md` (the model is given the English body: system-side text is
English, section 12). `manifest.json` pins every file of every skill by SHA-256. Every load checks them
all: a skill with a changed, missing, extra, oversized or symlinked file is unavailable, and reported
(`problems`), until the manifest is pinned again with the change reviewed. Nothing is written to the data
folder: S1-22 installs `scholia.skills` as the pre-approved first-party extension (scope notes, reading 9).

A skill's `allowed-tools` field is never honored, and no script in a skill ever runs: none is shipped,
and only text files are read. A skill's files are offered to the agent by file id (`<skill>#<n>`, in the
manifest's order), never by path (`read_skill_file`, final review F06).
"""

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "extensions" / "scholia.skills"
MAX_FILE_BYTES = 1_000_000  # section 11: each file at most 1 MB
MAX_SKILL_BYTES = 20_000_000  # and each skill at most 20 MB
OWN = ("SKILL.md", "SKILL.zh-CN.md")  # the skill's own text; its other files are offered by id
OFFERED = ("references/", "assets/")  # the folders whose files the agent may read


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str  # SKILL.md without its front matter, as the agent is given it
    files: tuple  # ((file id, relative path, sha256), ...) the agent may read
    zh: dict  # {"name", "description", "body"} from SKILL.zh-CN.md, for the interface
    sha256: str  # SKILL.md's, recorded with each activation


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def front_matter(text):
    """({key: value} for the simple `key: value` lines of the front matter, the body after it)."""
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text
    fields = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if colon and not line.startswith((" ", "\t", "-")):
            fields[key.strip()] = value.strip()
    return fields, body.lstrip("\n")


def _files(folder):
    """{relative path: bytes} of every file under folder; raises ValueError naming a symlink or a non-file."""
    found = {}
    for here, dirs, names in os.walk(folder, followlinks=False):
        for entry in [*dirs, *names]:
            path = Path(here) / entry
            if path.is_symlink():
                raise ValueError("symlink")
        for name in names:
            path = Path(here) / name
            if not path.is_file():
                raise ValueError("not_a_file")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("too_large")
            found[path.relative_to(folder).as_posix()] = path.read_bytes()
    return found


def _skill(root, name, pinned):
    folder = root / name
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("missing")
    found = _files(folder)
    if set(found) - set(pinned):
        raise ValueError("extra_file")
    if set(pinned) - set(found):
        raise ValueError("missing_file")
    if any(_sha(found[path]) != sha for path, sha in pinned.items()):
        raise ValueError("changed")
    if sum(map(len, found.values())) > MAX_SKILL_BYTES:
        raise ValueError("too_large")
    fields, body = front_matter(found["SKILL.md"].decode("utf-8"))
    if fields.get("name") != name or not fields.get("description"):
        raise ValueError("front_matter")
    zh_fields, zh_body = front_matter(found.get("SKILL.zh-CN.md", b"").decode("utf-8"))
    offered = [path for path in pinned if path.startswith(OFFERED)]
    return Skill(name, fields["description"], body,
                 tuple((f"{name}#{i}", path, pinned[path]) for i, path in enumerate(offered, start=1)),
                 {"name": zh_fields.get("name", name), "description": zh_fields.get("description", fields["description"]),
                  "body": zh_body}, pinned["SKILL.md"])


def load(root=None):
    """(the available skills by name, {name: why it is unavailable}), every file checked against the manifest."""
    root = root or ROOT
    skills, problems = {}, {}
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {"scholia.skills": "manifest"}
    for name, entry in (manifest.get("skills") or {}).items():
        try:
            skills[name] = _skill(root, name, entry["files"])
        except (OSError, ValueError, UnicodeDecodeError, KeyError) as error:
            problems[name] = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeDecodeError) \
                else "unreadable"
    return skills, problems


def read_file(file_id, root=None):
    """(text, sha256) of a skill file by its pinned id, its hash checked as it is read; ValueError otherwise."""
    root = root or ROOT
    name, _, _ = str(file_id).partition("#")
    skills, _ = load(root)
    skill = skills.get(name)
    entry = next((f for f in skill.files if f[0] == file_id), None) if skill else None
    if entry is None:
        raise ValueError("unknown_file")
    path = root / name / entry[1]
    data = path.read_bytes() if not path.is_symlink() else b""
    if _sha(data) != entry[2]:
        raise ValueError("changed")
    return data.decode("utf-8"), entry[2]


def pin(root=None):
    """Write the manifest from the files as they are: run after a reviewed change to a skill."""
    root = root or ROOT
    skills = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        files = _files(folder)
        skills[folder.name] = {"files": {path: _sha(files[path]) for path in sorted(files)}}
    (root / "manifest.json").write_text(json.dumps({"extension": "scholia.skills", "skills": skills}, indent=2) + "\n",
                                        encoding="utf-8")


if __name__ == "__main__" and sys.argv[1:] == ["pin"]:
    pin()
