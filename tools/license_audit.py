"""License audit of a frozen PyInstaller onedir bundle.

Run it with the environment that built the bundle:

    uv run python tools/license_audit.py build/dist/scholia-probe

It lists every file in the bundle and every module archived inside it, and
assigns each to a component: Scholia's own code, CPython with the third-party
code python.org's build carries, PyInstaller, or an installed Python
distribution. It fails when
- a file or module belongs to no known component,
- a component's license is not on the allowed list,
- a distribution's native code has not been reviewed for the libraries it embeds, or
- a component's license text is not shipped in the bundle's licenses/ folder.

A PyInstaller spec ships those texts with `notice_datas()`.
"""

import re
import sys
import sysconfig
import zipfile
from collections import defaultdict
from functools import cache
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENTS = "_internal"  # PyInstaller's onedir contents folder
LICENSES = "licenses"  # license texts ship in <contents>/licenses/<component>/
# The build interpreter's installation, which every bundled CPython file must come from
CPYTHON_HOME = Path(sys.base_prefix)
LIB_DYNLOAD = Path(sysconfig.get_path("platstdlib"), "lib-dynload")
# CPython's license document, which covers the third-party code it incorporates
CPYTHON_DOC = Path(sys.base_prefix, "Resources/English.lproj/Documentation/_sources",
                   "license.rst.txt")
# pyinstaller-hooks-contrib's license file puts its run-time hooks, the only part of it
# an app bundles, under Apache-2.0, and the rest of the distribution under the GPL.
CONTRIB_RTHOOKS = "pyinstaller-hooks-contrib-rthooks"

# Licenses that ask only for attribution, a notice or a disclaimer, and public-domain terms.
ALLOWED = {
    "MIT", "MIT-0", "BSD", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Apache-2.0", "ISC", "Zlib",
    "Libpng", "IJG", "FTL", "PSF-2.0", "BSL-1.0", "Unicode-3.0", "Unicode-DFS-2016",
    "blessing", "CC0-1.0", "Unlicense", "LicenseRef-Public-Domain",
}
# GPL is allowed only with these exceptions.
ALLOWED_WITH = {
    ("GPL-3.0-or-later", "GCC-exception-3.1"),
    ("GPL-2.0-or-later", "Bootloader-exception"),
    ("Apache-2.0", "LLVM-exception"),
}

# Third-party code inside python.org's macOS CPython 3.13 build, by the bundled file
# that carries it (file name up to its first dot). That build links bzip2, libffi,
# zlib and libedit from macOS, so they are not bundled. A shared library from the
# build's lib folder that is not listed here fails the audit.
CPYTHON_EMBEDDED = {
    "Python": ["mimalloc"],
    "libssl": ["OpenSSL"],
    "libcrypto": ["OpenSSL"],
    "_decimal": ["libmpdec"],
    "pyexpat": ["expat"],
    "_elementtree": ["expat"],
    "_sqlite3": ["SQLite"],
    "_lzma": ["xz"],
    "_md5": ["HACL-star"],
    "_sha1": ["HACL-star"],
    "_sha2": ["HACL-star"],
    "_sha3": ["HACL-star"],
    "_blake2": ["BLAKE2"],
}

# Third-party libraries: (license, notice). A notice is a list of license files that
# must ship in licenses/<name>/, or a name CPython's shipped license document covers.
LIBRARIES = {
    "OpenSSL": ("Apache-2.0", "OpenSSL"),
    "expat": ("MIT", "expat"),
    "libmpdec": ("BSD-2-Clause", "libmpdec"),
    "mimalloc": ("MIT", "mimalloc"),
    "SQLite": ("blessing", []),
    "xz": ("0BSD OR LicenseRef-Public-Domain", []),  # liblzma: public domain before 5.6, then 0BSD
    "BLAKE2": ("CC0-1.0 OR OpenSSL OR Apache-2.0", []),
    "HACL-star": ("MIT", [ROOT / "tools/notices/HACL-star/LICENSE"]),
}

# Distributions whose native code was reviewed, with the third-party libraries it embeds
# (names in LIBRARIES). No distribution's native code is bundled yet.
REVIEWED_NATIVE: dict[str, list[str]] = {}

CLASSIFIERS = {
    "MIT License": "MIT",
    "BSD License": "BSD",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "zlib/libpng License": "Zlib",
    "The Unlicense (Unlicense)": "Unlicense",
    "Public Domain": "LicenseRef-Public-Domain",
}

# Mach-O magic numbers in both byte orders: thin 32- and 64-bit, universal 32- and 64-bit
MACHO_MAGIC = {
    magic.to_bytes(4, order)
    for magic in (0xFEEDFACE, 0xFEEDFACF, 0xCAFEBABE, 0xCAFEBABF)
    for order in ("big", "little")
}


def allowed(expression: str) -> bool:
    """Whether an SPDX license expression is allowed. Raises ValueError if malformed."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    pos = 0

    def take():
        nonlocal pos
        if pos == len(tokens):
            raise ValueError(f"incomplete license expression {expression!r}")
        pos += 1
        return tokens[pos - 1]

    def peek(word):
        return pos < len(tokens) and tokens[pos].upper() == word

    def term():
        token = take()
        if token == "(":
            ok = either()
            if take() != ")":
                raise ValueError(f"unbalanced license expression {expression!r}")
            return ok
        if token in ("(", ")") or token.upper() in ("AND", "OR", "WITH"):
            raise ValueError(f"malformed license expression {expression!r}")
        if peek("WITH"):
            take()
            return (token, take()) in ALLOWED_WITH
        return token in ALLOWED

    def both():
        ok = term()
        while peek("AND"):
            take()
            ok = term() and ok
        return ok

    def either():
        ok = both()
        while peek("OR"):
            take()
            ok = both() or ok
        return ok

    ok = either()
    if pos != len(tokens):
        raise ValueError(f"malformed license expression {expression!r}")
    return ok


def _license_files(dist) -> list[tuple[Path, str]]:
    """A distribution's own license files, as (source, path under its licenses folder)."""
    files = []
    for f in dist.files or []:
        parts = f.parts
        if len(parts) > 1 and parts[0].endswith(".dist-info"):
            if parts[1] == "licenses" and len(parts) > 2:
                files.append((Path(dist.locate_file(f)), "/".join(parts[2:])))
            elif re.match(r"(LICEN[CS]E|COPYING|NOTICE)", f.name, re.I):
                files.append((Path(dist.locate_file(f)), "/".join(parts[1:])))
    return files


def _dist_license(dist) -> str | None:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    if re.fullmatch(r"[\w.+-]+", (meta.get("License") or "").strip()):
        return meta["License"].strip()
    classifiers = meta.get_all("Classifier") or []
    labels = [c.split(" :: ")[-1] for c in classifiers if c.startswith("License :: ")]
    # Several license classifiers do not say whether they are a choice, so all must pass.
    names = [CLASSIFIERS.get(label, "LicenseRef-" + re.sub(r"\W+", "-", label)) for label in labels]
    return " AND ".join(names) or None


def component(name: str):
    """(license, notice) for a component; notice as described at LIBRARIES."""
    if name == "Scholia":
        return "MIT", [(ROOT / "LICENSE", "LICENSE")]
    if name == "CPython":
        stdlib = Path(sysconfig.get_path("stdlib"))
        return "PSF-2.0", [(stdlib / "LICENSE.txt", "LICENSE.txt"), (CPYTHON_DOC, CPYTHON_DOC.name)]
    if name == "PyInstaller":
        # The bootloader and loader carry the bootloader exception; run-time hooks are Apache-2.0.
        return ("GPL-2.0-or-later WITH Bootloader-exception AND Apache-2.0",
                _license_files(metadata.distribution("pyinstaller")))
    if name == CONTRIB_RTHOOKS:
        return "Apache-2.0", _license_files(metadata.distribution("pyinstaller-hooks-contrib"))
    if name in LIBRARIES:
        license, notice = LIBRARIES[name]
        return license, notice if isinstance(notice, str) else [(p, p.name) for p in notice]
    dist = metadata.distribution(name)
    return _dist_license(dist), _license_files(dist)


def notice_datas(dists=()) -> list[tuple[str, str]]:
    """PyInstaller `datas` entries that ship the license files of the fixed components
    (Scholia, CPython, PyInstaller and the libraries with their own files) and of `dists`."""
    with_files = [name for name, (_, files) in LIBRARIES.items() if isinstance(files, list) and files]
    names = ["Scholia", "CPython", "PyInstaller", *with_files, *dists]
    return [
        (str(source), str(Path(LICENSES, name, dest).parent))
        for name in names
        for source, dest in component(name)[1]
    ]


@cache
def _record_owners() -> dict[str, str]:
    """Installed file path (relative to site-packages) -> distribution name."""
    return {
        f.as_posix(): dist.metadata["Name"]
        for dist in metadata.distributions()
        for f in dist.files or []
    }


def _is_macho(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(4) in MACHO_MAGIC


def _rthook_owner(name: str):
    owners = _record_owners()
    if f"PyInstaller/hooks/rthooks/{name}.py" in owners:
        return ["PyInstaller"]
    if f"_pyinstaller_hooks_contrib/rthooks/{name}.py" in owners:
        return [CONTRIB_RTHOOKS]
    return None


def assign_module(kind: str, name: str):
    """The components an archived module or script belongs to, or None."""
    top = name.split(".")[0]
    if top in sys.stdlib_module_names or top.startswith("_sysconfigdata_"):
        return ["CPython"]
    if top.startswith(("pyimod", "pyiboot")) or top == "_pyi_rth_utils":
        return ["PyInstaller"]
    if top.startswith("pyi_rth_"):
        return _rthook_owner(top)
    if top == "backend" or (kind == "script" and (ROOT / "tools" / f"{top}.py").is_file()):
        return ["Scholia"]
    owners = metadata.packages_distributions().get(top)
    return sorted(set(owners)) if owners else None


def assign_file(bundle: Path, rel: str, problems: list[str]):
    """The components a bundled file belongs to, or None."""
    parts = rel.split("/")
    if len(parts) == 1:  # the executable: PyInstaller's bootloader and an archive
        return ["PyInstaller"] if _is_macho(bundle / rel) else None
    if parts[0] != CONTENTS:
        return None
    inner = "/".join(parts[1:])
    stem = parts[-1].split(".")[0]
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    cpython = ["CPython", *CPYTHON_EMBEDDED.get(stem, [])]
    # CPython files are accepted only when the build interpreter has them, by name.
    if inner == "base_library.zip":  # PyInstaller's archive of stdlib modules, checked by member
        return ["CPython"]
    if inner.startswith(f"python{version}/lib-dynload/"):
        return cpython if len(parts) == 4 and (LIB_DYNLOAD / parts[-1]).is_file() else None
    if parts[1] == "Python.framework":  # Versions/<version>/ mirrors the installation
        mirrored = parts[2:4] == ["Versions", version] and len(parts) > 4
        return cpython if mirrored and (CPYTHON_HOME / "/".join(parts[4:])).is_file() else None
    if inner == "Python" and (CPYTHON_HOME / "Python").is_file():
        return cpython
    if len(parts) == 2 and parts[1].endswith(".dylib") and (CPYTHON_HOME / "lib" / parts[1]).is_file():
        return CPYTHON_EMBEDDED.get(stem)  # a library the build ships; None if not reviewed
    owner = _record_owners().get(inner)
    if owner is None:
        return None
    if not _is_macho(bundle / rel):
        return [owner]
    if owner not in REVIEWED_NATIVE:
        problems.append(
            f"{rel}: native code from {owner} has not been reviewed for the libraries it embeds"
        )
    return [owner, *REVIEWED_NATIVE.get(owner, [])]


def _archived(path: Path, problems: list[str]) -> list[tuple[str, str]]:
    """(kind, name) of every module and script archived in a PyInstaller executable."""
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(path))
    items = []
    for name, entry in reader.toc.items():
        typecode = entry[-1]
        if typecode == "z":
            items += [("module", module) for module in reader.open_embedded_archive(name).toc]
        elif typecode in ("m", "s"):
            items.append(("module" if typecode == "m" else "script", name))
        elif typecode != "o":  # "o" entries are run-time options, not code
            problems.append(f"{path.name}: unexpected archive entry {name!r} ({typecode})")
    return items


def audit(bundle: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Components found in the bundle (with where), and the problems that fail the audit."""
    found: dict[str, set[str]] = defaultdict(set)
    problems: list[str] = []
    if not bundle.is_dir():
        return found, [f"{bundle}: not a directory"]
    notices = Path(CONTENTS, LICENSES).as_posix() + "/"
    inventoried = 0
    for path in sorted(p for p in bundle.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(bundle).as_posix()
        if rel.startswith(notices):
            continue
        inventoried += 1
        owners = assign_file(bundle, rel, problems)
        if owners is None:
            problems.append(f"{rel}: belongs to no known component")
            continue
        for owner in owners:
            found[owner].add(rel)
        members = []
        if "/" not in rel:
            members = _archived(path, problems)
        elif rel == f"{CONTENTS}/base_library.zip":
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if member.endswith(".pyc"):
                        members.append(("module", member.removesuffix(".pyc").replace("/", ".")))
                    elif not member.endswith("/"):  # anything but compiled modules and folders
                        problems.append(f"{rel}: member {member} belongs to no known component")
        for kind, name in members:
            owners = assign_module(kind, name)
            if owners is None:
                problems.append(f"{rel}: {kind} {name} belongs to no known component")
            for owner in owners or []:
                found[owner].add(rel)
    if not inventoried:
        problems.append(f"{bundle}: the bundle contains no files")
    for name in sorted(found):
        problems += _check(bundle, name)
    return found, problems


def _check(bundle: Path, name: str) -> list[str]:
    license, notice = component(name)
    problems = []
    try:
        if license is None:
            problems.append(f"{name}: no license in its metadata")
        elif not allowed(license):
            problems.append(f"{name}: license {license} is not allowed")
    except ValueError as error:
        problems.append(f"{name}: {error}")
    shipped = bundle / CONTENTS / LICENSES
    if isinstance(notice, str):
        doc = shipped / "CPython" / CPYTHON_DOC.name
        if not doc.is_file() or notice not in doc.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{name}: not covered by CPython's shipped license document")
        return problems
    if not notice and name not in LIBRARIES:
        problems.append(f"{name}: the distribution has no license file to ship")
    for source, dest in notice:
        copy = shipped / name / dest
        if not source.is_file():
            problems.append(f"{name}: license file {source} not found in the build environment")
        elif not copy.is_file() or copy.read_bytes() != source.read_bytes():
            problems.append(f"{name}: license file {dest} is not shipped in {LICENSES}/{name}/")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: license_audit.py BUNDLE_DIR", file=sys.stderr)
        return 2
    problems = []
    if sys.version_info[:2] != (3, 13) or getattr(sys, "_framework", "") != "Python":
        problems.append("the CPython entries describe python.org's macOS CPython 3.13; "
                        "run the audit with the interpreter that built the bundle")
    found, more = audit(Path(argv[0]))
    for name in sorted(found):
        print(f"{name}: {component(name)[0]} ({len(found[name])} files)")
    for problem in problems + more:
        print(f"FAIL {problem}")
    return 1 if problems or more else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
