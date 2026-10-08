"""License audit of the app: a PyInstaller onedir .app with the llama.cpp helper.

Run it with the environment that built the app:

    uv run python tools/license_audit.py "build/dist/Scholia.app"

It lists every file in the app and every module archived inside it, and
assigns each to a component: Scholia's own code, CPython with the third-party
code python.org's build carries, PyInstaller, the llama.cpp helper, an
installed Python distribution, or an npm package the interface bundles. It fails when
- a file or module belongs to no known component,
- a component's license is not on the allowed list,
- a distribution's native code has not been reviewed for the libraries it embeds, or
- a component's license text is not shipped in the app's Contents/Resources/licenses/ folder, or
- an npm package that only builds or tests the interface ships in it.

A PyInstaller spec ships those texts with `notice_datas()`.
"""

import fnmatch
import json
import re
import sys
import sysconfig
import zipfile
from collections import defaultdict
from functools import cache
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# In a .app, PyInstaller puts binaries in Contents/Frameworks and data files in
# Contents/Resources, and links each folder's entries from the other.
CONTENTS = ("Contents/Frameworks", "Contents/Resources")
# PyInstaller's stand-in for a dot in a folder name under Contents/Frameworks, which codesign
# allows only for frameworks (PyInstaller.building.osx.DOT_REPLACEMENT)
DOT = "__dot__"
LICENSES = "Contents/Resources/licenses"  # license texts ship in <LICENSES>/<component>/
# The llama.cpp helper: build_app.sh puts the server beside the app's executable and the
# libraries it links in their own Frameworks folder.
HELPER = "llama.cpp"
HELPER_SERVER = "Contents/MacOS/llama-server"
HELPER_LIBRARY = re.compile(r"Contents/Frameworks/llama-cpp/lib(llama|ggml|mtmd)[\w.-]*\.dylib")
PYDANTIC_CORE_CRATES = "pydantic-core-crates"
PDFIUM = "PDFium"  # the library pypdfium2 ships, with the third-party code built into it
# The build interpreter's installation, which every bundled CPython file must come from
CPYTHON_HOME = Path(sys.base_prefix)
# "stdlib", not "platstdlib": inside a venv, "platstdlib" names the venv
LIB_DYNLOAD = Path(sysconfig.get_path("stdlib"), "lib-dynload")
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
    # Anti-Grain Geometry 2.3, inside PDFium: copy, use, modify, sell and distribute "provided this
    # copyright notice appears in all copies", as is. A notice only; no SPDX identifier names it.
    "LicenseRef-AGG-2.3",
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
    # llama.cpp b11146's server and libraries. Reviewed: ggml and llama.cpp (MIT) with
    # cpp-httplib (MIT), nlohmann/json (MIT) and BoringSSL (Apache-2.0), whose notices the
    # release prints with `llama licenses` (build_app.sh checks the file matches), and
    # stb_image (MIT or Unlicense) and miniaudio (Unlicense or MIT-0) in libmtmd, taken
    # under the Unlicense, which needs no notice.
    HELPER: ("MIT AND Apache-2.0 AND (MIT OR Unlicense) AND (Unlicense OR MIT-0)",
             [ROOT / "tools/notices/llama.cpp/LICENSES.txt"]),
    # PDFium in pypdfium2 5.14.0's libpdfium.dylib (bblanchon/pdfium-binaries' build), reviewed from
    # the build's own license list, which ships with pypdfium2's license files (BUILD_LICENSES):
    # PDFium (BSD-3-Clause) and the build's scripts (MIT); abseil (Apache-2.0); Anti-Grain Geometry
    # 2.3; fast_float (MIT); FreeType (FTL, taken over its GPL choice); ICU (Unicode-3.0); Little CMS
    # (MIT); libjpeg-turbo (IJG, BSD-3-Clause and Zlib); OpenJPEG (BSD-2-Clause); libpng (its
    # license, Libpng); LLVM's libc (Apache-2.0 with the LLVM exception); simdutf (Apache-2.0 or
    # MIT); zlib (Zlib).
    PDFIUM: ("BSD-3-Clause AND MIT AND Apache-2.0 AND LicenseRef-AGG-2.3 AND FTL AND Unicode-3.0 AND IJG AND Zlib"
             " AND BSD-2-Clause AND Libpng AND (Apache-2.0 WITH LLVM-exception) AND (Apache-2.0 OR MIT)", []),
    # The Rust crates and standard library compiled into pydantic-core's extension, with the
    # licenses tools/rust_notices.py found in its Cargo.lock (it prints this expression).
    PYDANTIC_CORE_CRATES: (
        "((MIT OR Apache-2.0) AND Unicode-DFS-2016) AND (Apache-2.0 OR BSL-1.0) AND (Apache-2.0 OR MIT)"
        " AND (Apache-2.0 WITH LLVM-exception) AND (Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT)"
        " AND (BSD-2-Clause OR Apache-2.0 OR MIT) AND MIT AND (MIT OR Apache-2.0)"
        " AND (MIT OR Apache-2.0 OR LGPL-2.1-or-later) AND Unicode-3.0 AND (Unlicense OR MIT)",
        [ROOT / "tools/notices/pydantic-core/RUST-NOTICES.txt"]),
}
# Rust extensions whose compiled-in crates' notices tools/rust_notices.py generated for exactly
# this version; another version fails the audit until they are generated again.
RUST_NOTICES = {"pydantic_core": "2.41.5"}

# Unmodified MPL-2.0 packages (decided 2026-10-02). Each passes only by its entry here, at
# exactly this version, never by its license alone, and ships with its license text and a
# link to that version's source. Its files keep their own names in the bundle, apart from
# Scholia's code.
MPL_PACKAGES = {
    "certifi": "2025.11.12",
}

# Distributions whose native code was reviewed: for each native file (a pattern on its path
# in the installed distribution), the third-party libraries it embeds (names in LIBRARIES).
REVIEWED_NATIVE: dict[str, dict[str, list[str]]] = {
    "apsw": {"apsw/__init__.*": ["SQLite"]},  # the SQLite amalgamation, linked statically
    "sqlite-vec": {"sqlite_vec/vec0.dylib": []},  # one C source file, no dependencies
    # Bindings to Apple's frameworks; libffi comes from the system, not the wheel.
    "pyobjc-core": {"objc/*": []},
    "pyobjc-framework-Cocoa": {"*": []},
    "pyobjc-framework-Quartz": {"Quartz/*": []},
    "pyobjc-framework-CoreML": {"CoreML/*": []},
    "pyobjc-framework-Vision": {"Vision/*": []},
    "pyobjc-framework-WebKit": {"WebKit/*": []},
    # A Rust extension: the crates it uses, and Rust's standard library, are linked in.
    "pydantic_core": {"pydantic_core/_pydantic_core.*": [PYDANTIC_CORE_CRATES]},
    # PyCryptodome's own C code and PyCrypto's (its LICENSE.rst: BSD-2-Clause and public domain).
    # Reviewed for 3.23.0: each of its 40 extensions links only libSystem, and none carries
    # another project's copyright or license notice.
    "pycryptodomex": {"Cryptodome/*": []},
    # Its one native file, PDFium as one library (see LIBRARIES); reviewed for 5.14.0, on macOS arm64.
    "pypdfium2": {"pypdfium2_raw/libpdfium.dylib": [PDFIUM]},
}

# Licenses read from a distribution's own license text where its metadata is not a usable
# SPDX expression.
REVIEWED_LICENSES = {
    "apsw": "Zlib",  # metadata "any-OSI"; its LICENSE is the zlib license, or any OSI license
    "sqlite-vec": "MIT OR Apache-2.0",  # metadata names both licenses in free text
    # Its metadata says MIT, but the only license text upstream, which ships, is BSD-style
    # (Armin Ronacher, Jonathan Tushman).
    "proxy_tools": "BSD-2-Clause",
    # Metadata "BSD, Public Domain"; its LICENSE.rst puts PyCrypto's code in the public domain by
    # the Unlicense and later contributions under BSD-2-Clause.
    "pycryptodomex": "BSD-2-Clause AND Unlicense",
    # Metadata MIT; it is a fork of CPython's zipfile, and ships the PSF license for that part.
    "pyzipper": "MIT AND PSF-2.0",
    # Metadata "BSD-3-Clause, Apache-2.0, dependency licenses": its own code under either; what it
    # builds in is the PDFium library's entry. Its CC-BY-4.0 text covers its documentation, which
    # does not ship.
    "pypdfium2": "Apache-2.0 OR BSD-3-Clause",
}
# License texts for distributions whose wheels ship none, from their upstream repositories
# at the bundled versions.
SUPPLIED_NOTICES = {
    "sqlite-vec": ["sqlite-vec/LICENSE-MIT", "sqlite-vec/LICENSE-APACHE"],
    "pyobjc-core": ["pyobjc/License.txt"],
    "pyobjc-framework-CoreML": ["pyobjc/License.txt"],
    "pyobjc-framework-Vision": ["pyobjc/License.txt"],
    "pyobjc-framework-UniformTypeIdentifiers": ["pyobjc/License.txt"],
    # Upstream's LICENSE.txt at commit db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6; 0.1.0 is untagged.
    "proxy_tools": ["proxy_tools/LICENSE.txt"],
}

# The interface. Vite builds frontend/dist, and its licenses plugin (frontend/licenses.mjs)
# records the npm packages the build bundles in frontend/dist-licenses/packages.json, with
# copies of their license files. The app ships frontend/dist as its frontend folder; each
# file there must be the build's, byte for byte, and belongs to Scholia and to every bundled
# package. Each version of a package is a component, "npm/<name>@<version>".
FRONTEND = ROOT / "frontend"
NPM = "npm/"
# Development packages whose code the build copies into what ships: Tailwind's base styles
# (preflight) are in the built CSS. Any other development package that ships fails the audit.
NPM_BUILD_CODE = {"tailwindcss"}
# License texts for npm packages that ship none, from their upstream repositories.
NPM_SUPPLIED = {
    # The radix-ui/primitives monorepo's LICENSE, which its other packages ship
    **{f"@radix-ui/{name}": ["npm/radix-ui-primitives/LICENSE"] for name in (
        "react-compose-refs", "react-context", "react-direction", "react-id", "react-use-callback-ref",
        "react-use-escape-keydown", "react-use-layout-effect", "react-use-size")},
    # Upstream's LICENSE at commit 8ca9ba5ea52de03308fe8ced94f7b159a44d28ff; 2.3.8 is untagged.
    "react-remove-scroll-bar": ["npm/react-remove-scroll-bar/LICENSE"],
}

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


@cache
def npm_packages() -> dict[str, dict]:
    """The npm packages the interface's build bundles, by "<name>@<version>", as the build
    recorded them."""
    path = FRONTEND / "dist-licenses/packages.json"
    packages = json.loads(path.read_text()) if path.is_file() else []
    return {f"{p['name']}@{p['version']}": p for p in packages}


@cache
def _npm_lock() -> dict[str, dict]:
    """The interface's lockfile entries, by install path."""
    return json.loads((FRONTEND / "package-lock.json").read_text())["packages"]


def component(name: str):
    """(license, notice) for a component; notice as described at LIBRARIES."""
    if name.startswith(NPM):
        key = name.removeprefix(NPM)
        package = npm_packages()[key]
        own = [(FRONTEND / "dist-licenses" / key / file, file) for file in package["files"]]
        supplied = [(ROOT / "tools/notices" / path, Path(path).name) for path in NPM_SUPPLIED.get(package["name"], [])]
        return package["license"], own or supplied
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
    supplied = [ROOT / "tools/notices" / path for path in SUPPLIED_NOTICES.get(name, [])]
    files = _license_files(dist) or [(path, path.name) for path in supplied]
    if name in MPL_PACKAGES:  # its source link ships beside its license
        files.append((ROOT / "tools/notices" / name / "SOURCE.txt", "SOURCE.txt"))
    return REVIEWED_LICENSES.get(name) or _dist_license(dist), files


def _shipped_by_default() -> list[str]:
    """The fixed components whose license files every bundle ships."""
    with_files = [name for name, (_, files) in LIBRARIES.items() if isinstance(files, list) and files]
    return ["Scholia", "CPython", "PyInstaller", *with_files]


def notice_datas(dists=()) -> list[tuple[str, str]]:
    """PyInstaller `datas` entries that ship the license files of the fixed components
    (Scholia, CPython, PyInstaller and the libraries with their own files) and of `dists`."""
    names = [*_shipped_by_default(), *dists]
    return [
        (str(source), str(Path("licenses", name, dest).parent))
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


def _inner(rel: str) -> str | None:
    """A file's path inside PyInstaller's contents folders, or None if outside them."""
    for folder in CONTENTS:
        if rel.startswith(folder + "/"):
            *folders, name = rel[len(folder) + 1:].split("/")
            return "/".join([*(f.replace(DOT, ".") for f in folders), name])
    return None


def _pyinstaller_icon() -> Path:
    return Path(metadata.distribution("pyinstaller").locate_file(
        "PyInstaller/bootloader/images/icon-windowed.icns"))


def assign_file(bundle: Path, rel: str, problems: list[str]):
    """The components a file in the app belongs to, or None."""
    path = bundle / rel
    if rel == HELPER_SERVER or HELPER_LIBRARY.fullmatch(rel):
        return [HELPER] if _is_macho(path) else None
    if rel.startswith("Contents/MacOS/") and rel.count("/") == 2:
        # the app's executable: PyInstaller's bootloader and an archive
        return ["PyInstaller"] if _is_macho(path) else None
    if rel in ("Contents/Info.plist", "Contents/_CodeSignature/CodeResources"):
        return ["Scholia"]  # the app's metadata and its signature seal
    if rel == "Contents/Resources/icon-windowed.icns":  # PyInstaller's default app icon
        return ["PyInstaller"] if path.read_bytes() == _pyinstaller_icon().read_bytes() else None
    inner = _inner(rel)
    if inner is None:
        return None
    parts = inner.split("/")
    stem = parts[-1].split(".")[0]
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    cpython = ["CPython", *CPYTHON_EMBEDDED.get(stem, [])]
    # CPython files are accepted only when the build interpreter has them, by name.
    if inner == "base_library.zip":  # PyInstaller's archive of stdlib modules, checked by member
        return ["CPython"]
    if inner.startswith(f"python{version}/lib-dynload/"):
        return cpython if len(parts) == 3 and (LIB_DYNLOAD / parts[-1]).is_file() else None
    if parts[0] == "Python.framework":  # Versions/<version>/ mirrors the installation
        source = "/".join(parts[3:])
        if parts[1:3] != ["Versions", version] or not source or not (CPYTHON_HOME / source).is_file():
            return None
        if source == "Python" or not _is_macho(path):  # the interpreter, or a resource
            return cpython
        if len(parts) == 5 and parts[3] == "lib":  # a library the build ships, as at top level
            return CPYTHON_EMBEDDED.get(stem)
        return None
    if inner == "Python" and (CPYTHON_HOME / "Python").is_file():
        return cpython
    if len(parts) == 1 and inner.endswith(".dylib") and (CPYTHON_HOME / "lib" / inner).is_file():
        return CPYTHON_EMBEDDED.get(stem)  # a library the build ships; None if not reviewed
    if parts[0] == "frontend":  # the built interface: the build's own files only
        built = FRONTEND / "dist" / "/".join(parts[1:])
        if not npm_packages():
            problems.append(f"{rel}: the build recorded no npm packages (frontend/dist-licenses/packages.json)")
            return None
        if len(parts) == 1 or not built.is_file() or built.read_bytes() != path.read_bytes():
            return None
        return ["Scholia", *(NPM + name for name in npm_packages())]
    if parts[0] == "backend" and not _is_macho(path) and (ROOT / inner).is_file():
        return ["Scholia"]  # a data file of Scholia's own backend, such as its reasoning record
    owner = _record_owners().get(inner)
    if owner is None:
        return None
    if not _is_macho(path):
        return [owner]
    reviewed = REVIEWED_NATIVE.get(owner, {})
    embedded = next((libs for pattern, libs in reviewed.items() if fnmatch.fnmatch(inner, pattern)), None)
    if embedded is None:
        problems.append(
            f"{rel}: native code from {owner} has not been reviewed for the libraries it embeds"
        )
    return [owner, *(embedded or [])]


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
    notices = LICENSES + "/"
    inventoried = 0
    owners_of: dict[str, list[str]] = {}
    links = []
    notice_entries = set()  # everything in the licenses folder but its real subfolders
    for path in sorted(bundle.rglob("*")):
        rel = path.relative_to(bundle).as_posix()
        if rel.startswith(notices):
            if path.is_symlink() or not path.is_dir():
                notice_entries.add(rel)
            continue
        if path.is_symlink():
            links.append((rel, path))
            continue
        if not path.is_file():
            continue
        inventoried += 1
        owners = assign_file(bundle, rel, problems)
        if owners is None:
            problems.append(f"{rel}: belongs to no known component")
            continue
        owners_of[rel] = owners
        for owner in owners:
            found[owner].add(rel)
        members = []
        if owners == ["PyInstaller"] and rel.startswith("Contents/MacOS/"):
            members = _archived(path, problems)
        elif _inner(rel) == "base_library.zip":
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
    # A symlink must resolve inside the bundle; one to a file shares that file's components.
    root = bundle.resolve()
    for rel, path in links:
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError):  # missing target, or a loop
            problems.append(f"{rel}: symlink to nothing")
            continue
        if not target.is_relative_to(root):
            problems.append(f"{rel}: symlink to {target}, outside the bundle")
            continue
        for owner in owners_of.get(target.relative_to(root).as_posix(), []):
            found[owner].add(rel)
    if not inventoried:
        problems.append(f"{bundle}: the bundle contains no files")
    # Only the expected license files may sit in the licenses folder; _check requires
    # each to be a regular file inside the bundle.
    expected = {
        f"{notices}{name}/{dest}"
        for name in {*found, *_shipped_by_default()}
        if isinstance(notice := component(name)[1], list)
        for _, dest in notice
    }
    for rel in sorted(notice_entries - expected):
        problems.append(f"{rel}: not an expected license file")
    for name in sorted(found):
        problems += _check(bundle, name)
    return found, problems


def _check(bundle: Path, name: str) -> list[str]:
    license, notice = component(name)
    problems = []
    try:
        if license is None:
            problems.append(f"{name}: no license in its metadata")
        elif name in MPL_PACKAGES and license == "MPL-2.0":
            version = metadata.version(name)
            if version != MPL_PACKAGES[name]:
                problems.append(f"{name}: version {version} is not the reviewed MPL-2.0 version {MPL_PACKAGES[name]}")
            source = ROOT / "tools/notices" / name / "SOURCE.txt"
            if not source.is_file() or f"{name} {MPL_PACKAGES[name]} " not in source.read_text(encoding="utf-8"):
                problems.append(f"{name}: SOURCE.txt does not name version {MPL_PACKAGES[name]}")
        elif not allowed(license):
            problems.append(f"{name}: license {license} is not allowed")
    except ValueError as error:
        problems.append(f"{name}: {error}")
    if name.startswith(NPM):  # checked at the path the build took it from
        package = npm_packages()[name.removeprefix(NPM)]
        entry = _npm_lock().get(package.get("path"))
        if entry is None or entry.get("version") != package["version"]:
            problems.append(f"{name}: not in the interface's lockfile at {package.get('path')}")
        elif entry.get("dev") and package["name"] not in NPM_BUILD_CODE:
            problems.append(f"{name}: a development package ships in the interface")
    if name in RUST_NOTICES and metadata.version(name) != RUST_NOTICES[name]:
        problems.append(f"{name}: version {metadata.version(name)} has no Rust notices; generate them with "
                        "tools/rust_notices.py")
    shipped = LICENSES
    if isinstance(notice, str):
        doc = _regular_file(bundle, f"{shipped}/CPython/{CPYTHON_DOC.name}")
        if not doc or notice not in doc.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{name}: not covered by CPython's shipped license document")
        return problems
    if not notice and name not in LIBRARIES:
        problems.append(f"{name}: the distribution has no license file to ship")
    for source, dest in notice:
        copy = _regular_file(bundle, f"{shipped}/{name}/{dest}")
        if not source.is_file():
            problems.append(f"{name}: license file {source} not found in the build environment")
        elif not copy or copy.read_bytes() != source.read_bytes():
            problems.append(f"{name}: license file {dest} is not shipped in {LICENSES}/{name}/")
    return problems


def _regular_file(bundle: Path, rel: str) -> Path | None:
    """The file at `rel`, if it is a regular file with no symlink anywhere on its path
    inside the bundle; otherwise None."""
    path = bundle / rel
    inside = path.is_file() and path.resolve() == bundle.resolve() / rel
    return path if inside else None


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
