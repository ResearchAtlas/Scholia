import json
import sys
import zipfile
from importlib import metadata

import pytest

from tools import license_audit as la

DYNLOAD = "Contents/Frameworks/python3__dot__13/lib-dynload"  # as PyInstaller lays it out
MACHO = b"\xcf\xfa\xed\xfe" + bytes(28)


@pytest.mark.parametrize(
    "expression, ok",
    [
        ("MIT", True),
        ("BSD-3-Clause", True),
        ("(MIT OR Apache-2.0) AND Unicode-3.0", True),
        ("MIT OR GPL-3.0-only", True),
        ("MIT AND GPL-3.0-only", False),
        ("GPL-2.0-or-later WITH Bootloader-exception", True),
        ("GPL-2.0-or-later", False),
        ("GPL-3.0-or-later WITH GCC-exception-3.1", True),
        ("Apache-2.0 WITH LLVM-exception", True),
        ("GPL-3.0-or-later WITH LLVM-exception", False),
        ("LGPL-2.1-or-later", False),
        ("MPL-2.0", False),
    ],
)
def test_license_expressions(expression, ok):
    assert la.allowed(expression) is ok


@pytest.mark.parametrize("expression", ["", "MIT OR", "(MIT", "MIT)", "AND MIT", "MIT WITH"])
def test_malformed_license_expression_is_an_error(expression):
    with pytest.raises(ValueError):
        la.allowed(expression)


def test_license_comes_from_metadata_then_classifiers():
    assert la._dist_license(metadata.distribution("certifi")) == "MPL-2.0"
    assert la._dist_license(metadata.distribution("pywebview")) == "BSD"  # from its classifier


def test_archived_modules_are_assigned():
    assert la.assign_module("module", "json.decoder") == ["CPython"]
    assert la.assign_module("module", "pyimod02_importers") == ["PyInstaller"]
    assert la.assign_module("module", "httpx._client") == ["httpx"]
    assert la.assign_module("script", "app") == ["Scholia"]
    assert la.assign_module("module", "scholia_unknown_module") is None


def _put(root, rel, data=b""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _ship(root, name):
    for source, dest in la.component(name)[1]:
        _put(root, f"Contents/Resources/licenses/{name}/{dest}", source.read_bytes())


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A bundle holding CPython modules that embed SQLite and libmpdec, with CPython's texts,
    audited against a stand-in for the build interpreter's installation."""
    home = tmp_path / "python"
    for module in ("_json", "_sqlite3", "_decimal", "pyexpat"):
        _put(home, f"lib/python3.13/lib-dynload/{module}.cpython-313-darwin.so")
    _put(home, "Python")
    for library in ("libssl.3.dylib", "libgmp.10.dylib"):  # libgmp: one CPython never ships
        _put(home, f"lib/{library}")
    doc = home / "license.rst.txt"
    doc.write_text("... OpenSSL ... libmpdec ... mimalloc ...", encoding="utf-8")
    monkeypatch.setattr(la, "CPYTHON_HOME", home)
    monkeypatch.setattr(la, "LIB_DYNLOAD", home / "lib/python3.13/lib-dynload")
    monkeypatch.setattr(la, "CPYTHON_DOC", doc)
    root = tmp_path / "bundle"
    for module in ("_json", "_sqlite3", "_decimal"):
        _put(root, f"{DYNLOAD}/{module}.cpython-313-darwin.so", MACHO)
    _ship(root, "CPython")
    return root


def test_cpython_files_are_looked_up_in_the_build_interpreter_not_the_venv():
    for path in (la.CPYTHON_HOME, la.LIB_DYNLOAD, la.CPYTHON_DOC):
        assert path.is_relative_to(sys.base_prefix), path


def test_clean_bundle_passes(bundle):
    found, problems = la.audit(bundle)
    assert problems == []
    assert set(found) == {"CPython", "SQLite", "libmpdec"}


def test_cpython_files_must_come_from_the_build_interpreter(bundle):
    _put(bundle, "Contents/Frameworks/Python.framework/Versions/3.13/Python", MACHO)  # genuine
    found, problems = la.audit(bundle)
    assert problems == [] and "Contents/Frameworks/Python.framework/Versions/3.13/Python" in found["CPython"]
    _put(bundle, f"{DYNLOAD}/_evil.cpython-313-darwin.so", MACHO)
    _put(bundle, f"{DYNLOAD}/libavcodec.61.dylib", MACHO)
    _put(bundle, "Contents/Frameworks/Python.framework/Versions/3.13/lib/libgmp.10.dylib", MACHO)
    assert set(la.audit(bundle)[1]) == {
        f"{DYNLOAD}/_evil.cpython-313-darwin.so: belongs to no known component",
        f"{DYNLOAD}/libavcodec.61.dylib: belongs to no known component",
        "Contents/Frameworks/Python.framework/Versions/3.13/lib/libgmp.10.dylib: belongs to no known component",
    }


def test_base_library_members_must_all_be_known(bundle):
    archive = bundle / "Contents/Resources/base_library.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("json/__init__.pyc", b"")
    assert la.audit(bundle)[1] == []
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("unknownpkg/__init__.pyc", b"")
        zf.writestr("libfoo.dylib", MACHO)
    assert la.audit(bundle)[1] == [
        "Contents/Resources/base_library.zip: member libfoo.dylib belongs to no known component",
        "Contents/Resources/base_library.zip: module unknownpkg.__init__ belongs to no known component",
    ]


@pytest.fixture
def interface(tmp_path, monkeypatch):
    """A built interface bundling react, as frontend/licenses.mjs records a build."""
    front = tmp_path / "frontend"
    _put(front, "dist/index.html", b'<div id="root"></div>')
    _put(front, "dist/assets/app.js", b"react's code")
    _put(front, "dist-licenses/react@19.2.0/LICENSE", b"MIT License (react)")
    (front / "dist-licenses/packages.json").write_text(json.dumps([_npm("react", "19.2.0")]))
    lock = {"": {}, "node_modules/react": {"version": "19.2.0"},
            "node_modules/vite": {"version": "1.0.0", "dev": True},
            "node_modules/tailwindcss": {"version": "1.0.0", "dev": True},
            "node_modules/x": {"version": "2.0.0"}, "node_modules/y/node_modules/x": {"version": "1.0.0", "dev": True}}
    (front / "package-lock.json").write_text(json.dumps({"packages": lock}))
    monkeypatch.setattr(la, "FRONTEND", front)
    la.npm_packages.cache_clear()
    la._npm_lock.cache_clear()
    yield front
    la.npm_packages.cache_clear()
    la._npm_lock.cache_clear()


def _npm(name, version, path=None, files=("LICENSE",)):
    return {"name": name, "version": version, "license": "MIT", "files": list(files),
            "path": path or f"node_modules/{name}"}


def _ship_interface(bundle, front):
    for rel in ("index.html", "assets/app.js"):
        _put(bundle, f"Contents/Resources/frontend/{rel}", (front / "dist" / rel).read_bytes())
    _ship(bundle, "Scholia")
    _ship(bundle, "npm/react@19.2.0")


def test_the_interface_belongs_to_scholia_and_the_npm_packages_it_bundles(bundle, interface):
    _ship_interface(bundle, interface)
    found, problems = la.audit(bundle)
    assert problems == []
    assert found["npm/react@19.2.0"] == {"Contents/Resources/frontend/index.html", "Contents/Resources/frontend/assets/app.js"}
    assert "Contents/Resources/frontend/assets/app.js" in found["Scholia"]


def test_the_interface_must_be_the_builds_and_ship_its_packages_licenses(bundle, interface):
    _ship_interface(bundle, interface)
    (bundle / "Contents/Resources/licenses/npm/react@19.2.0/LICENSE").unlink()
    _put(bundle, "Contents/Resources/frontend/assets/extra.js", b"not built")
    _put(bundle, "Contents/Resources/frontend/assets/app.js", b"changed after the build")
    assert set(la.audit(bundle)[1]) == {
        "Contents/Resources/frontend/assets/extra.js: belongs to no known component",
        "Contents/Resources/frontend/assets/app.js: belongs to no known component",
        "npm/react@19.2.0: license file LICENSE is not shipped in Contents/Resources/licenses/npm/react@19.2.0/",
    }


def test_a_development_package_in_the_interface_fails_apart_from_tailwinds_styles(bundle, interface):
    packages = [_npm("react", "19.2.0"), _npm("vite", "1.0.0"), _npm("tailwindcss", "1.0.0"),
                _npm("x", "2.0.0"), _npm("x", "1.0.0", "node_modules/y/node_modules/x"), _npm("z", "1.0.0")]
    (interface / "dist-licenses/packages.json").write_text(json.dumps(packages))
    for key in ("vite@1.0.0", "tailwindcss@1.0.0", "x@2.0.0", "x@1.0.0", "z@1.0.0"):
        _put(interface, f"dist-licenses/{key}/LICENSE", b"MIT License")
    _ship_interface(bundle, interface)
    for key in ("vite@1.0.0", "tailwindcss@1.0.0", "x@2.0.0", "x@1.0.0", "z@1.0.0"):
        _ship(bundle, f"npm/{key}")
    found, problems = la.audit(bundle)
    assert {"npm/x@2.0.0", "npm/x@1.0.0"} <= set(found)  # each bundled version is its own component
    assert problems == [  # the dev flag is read at each version's own install path
        "npm/vite@1.0.0: a development package ships in the interface",
        "npm/x@1.0.0: a development package ships in the interface",
        "npm/z@1.0.0: not in the interface's lockfile at node_modules/z",
    ]


def test_an_interface_without_the_builds_record_fails(bundle, interface):
    _ship_interface(bundle, interface)
    (interface / "dist-licenses/packages.json").unlink()
    la.npm_packages.cache_clear()
    problems = la.audit(bundle)[1]
    assert "Contents/Resources/frontend/index.html: the build recorded no npm packages" \
        " (frontend/dist-licenses/packages.json)" in problems
    assert "Contents/Resources/frontend/index.html: belongs to no known component" in problems


def test_npm_packages_without_license_files_get_upstreams(interface):
    packages = [_npm(name, "1.0.0", files=()) for name in la.NPM_SUPPLIED]
    (interface / "dist-licenses/packages.json").write_text(json.dumps(packages))
    for name in la.NPM_SUPPLIED:
        files = la.component(f"{la.NPM}{name}@1.0.0")[1]
        assert files and all(source.is_file() for source, _ in files), name
    # The supplied Radix text is the one the monorepo's other packages ship.
    radix = la.ROOT / "frontend/node_modules/@radix-ui/react-dialog/LICENSE"
    if radix.is_file():
        assert radix.read_bytes() == (la.ROOT / "tools/notices/npm/radix-ui-primitives/LICENSE").read_bytes()


def test_missing_or_empty_bundle_fails(tmp_path, capsys):
    missing = tmp_path / "missing"
    assert la.audit(missing)[1] == [f"{missing}: not a directory"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert la.audit(empty)[1] == [f"{empty}: the bundle contains no files"]
    assert la.main([str(missing)]) == 1
    assert f"FAIL {missing}: not a directory" in capsys.readouterr().out


def test_file_of_no_known_component_fails(bundle):
    _put(bundle, "Contents/Frameworks/libavcodec.61.dylib", MACHO)
    _put(bundle, "README.txt")
    problems = la.audit(bundle)[1]
    assert any(p.startswith("Contents/Frameworks/libavcodec.61.dylib: belongs to no known") for p in problems)
    assert any(p.startswith("README.txt: belongs to no known") for p in problems)


def test_an_mpl_package_passes_only_by_its_named_entry_at_its_version(bundle, monkeypatch):
    assert not la.allowed("MPL-2.0")  # never by the license alone
    _put(bundle, "Contents/Resources/certifi/cacert.pem")
    _ship(bundle, "certifi")
    assert la.audit(bundle)[1] == []
    shipped = sorted(p.name for p in (bundle / "Contents/Resources/licenses/certifi").iterdir())
    assert shipped == ["LICENSE", "SOURCE.txt"]
    source = (bundle / "Contents/Resources/licenses/certifi/SOURCE.txt").read_text()
    version = metadata.version("certifi")
    assert f"https://github.com/certifi/python-certifi/tree/{version}" in source
    monkeypatch.setitem(la.MPL_PACKAGES, "certifi", "2020.1.1")
    assert la.audit(bundle)[1] == [
        f"certifi: version {version} is not the reviewed MPL-2.0 version 2020.1.1",
        "certifi: SOURCE.txt does not name version 2020.1.1",
    ]
    monkeypatch.delitem(la.MPL_PACKAGES, "certifi")
    assert "certifi: license MPL-2.0 is not allowed" in la.audit(bundle)[1]


def test_missing_or_changed_license_text_fails(bundle):
    shipped = bundle / "Contents/Resources/licenses/CPython/LICENSE.txt"
    shipped.write_bytes(shipped.read_bytes() + b"changed")
    (bundle / "Contents/Resources/licenses/CPython/license.rst.txt").unlink()
    problems = la.audit(bundle)[1]
    assert "CPython: license file LICENSE.txt is not shipped in Contents/Resources/licenses/CPython/" in problems
    assert "CPython: license file license.rst.txt is not shipped in Contents/Resources/licenses/CPython/" in problems


def test_embedded_library_needs_cpythons_license_document(bundle):
    _put(bundle, f"{DYNLOAD}/pyexpat.cpython-313-darwin.so", MACHO)
    assert la.audit(bundle)[1] == ["expat: not covered by CPython's shipped license document"]


# Mach-O magic numbers as stored on disk: thin 32- and 64-bit and universal 32- and
# 64-bit, each in both byte orders.
MACHO_MAGICS = [
    bytes.fromhex(h) for h in (
        "feedface", "cefaedfe", "feedfacf", "cffaedfe",
        "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
    )
]


@pytest.mark.parametrize("magic", MACHO_MAGICS, ids=lambda m: m.hex())
def test_unreviewed_native_code_from_a_distribution_fails(bundle, magic):
    _put(bundle, "Contents/Frameworks/watchfiles/_rust_notify.cpython-313-darwin.so", magic + bytes(28))
    _ship(bundle, "watchfiles")
    problems = la.audit(bundle)[1]
    assert len(problems) == 1 and "has not been reviewed" in problems[0]


def test_data_file_from_a_distribution_is_not_native_code(bundle):
    _put(bundle, "Contents/Frameworks/watchfiles/__init__.py", b"# Python source")
    _ship(bundle, "watchfiles")
    assert la.audit(bundle)[1] == []


def test_a_rust_extension_needs_its_crates_notices_for_its_exact_version(bundle, monkeypatch):
    _put(bundle, "Contents/Frameworks/pydantic_core/_pydantic_core.cpython-313-darwin.so", MACHO)
    _ship(bundle, "pydantic_core")
    assert la.audit(bundle)[1] == [
        "pydantic-core-crates: license file RUST-NOTICES.txt is not shipped in "
        "Contents/Resources/licenses/pydantic-core-crates/"]
    _ship(bundle, "pydantic-core-crates")
    found, problems = la.audit(bundle)
    assert problems == [] and "pydantic-core-crates" in found
    monkeypatch.setitem(la.RUST_NOTICES, "pydantic_core", "1.0.0")
    assert any("has no Rust notices" in p for p in la.audit(bundle)[1])


def test_the_rust_notices_are_for_the_installed_version():
    version = metadata.version("pydantic_core")
    assert la.RUST_NOTICES == {"pydantic_core": version}
    header = (la.ROOT / "tools/notices/pydantic-core/RUST-NOTICES.txt").read_text().splitlines()[0]
    assert header == f"Third-party notices for the Rust code compiled into pydantic-core {version}."
    assert la.allowed(la.component(la.PYDANTIC_CORE_CRATES)[0])


def test_scholias_own_backend_data_files_are_scholias(bundle):
    _put(bundle, "Contents/Resources/backend/reasoning_capabilities.json", b"{}")
    _ship(bundle, "Scholia")
    found, problems = la.audit(bundle)
    assert problems == [] and "Contents/Resources/backend/reasoning_capabilities.json" in found["Scholia"]
    _put(bundle, "Contents/Resources/backend/not_in_the_source.json", b"{}")
    assert la.audit(bundle)[1] == ["Contents/Resources/backend/not_in_the_source.json: belongs to no known component"]


def test_runtime_hooks_are_assigned_to_their_own_licenses():
    assert la.assign_module("script", "pyi_rth_inspect") == ["PyInstaller"]
    assert la.assign_module("script", "pyi_rth_enchant") == [la.CONTRIB_RTHOOKS]
    assert la.assign_module("script", "pyi_rth_unknown_hook") is None


def test_community_runtime_hooks_are_apache_but_the_rest_of_their_distribution_is_not(bundle):
    _ship(bundle, la.CONTRIB_RTHOOKS)
    assert la._check(bundle, la.CONTRIB_RTHOOKS) == []
    (bundle / f"Contents/Resources/licenses/{la.CONTRIB_RTHOOKS}/LICENSE").unlink()
    assert la._check(bundle, la.CONTRIB_RTHOOKS) == [
        f"{la.CONTRIB_RTHOOKS}: license file LICENSE is not shipped in Contents/Resources/licenses/{la.CONTRIB_RTHOOKS}/"
    ]
    _put(bundle, "Contents/Frameworks/_pyinstaller_hooks_contrib/__init__.py")
    _ship(bundle, "pyinstaller-hooks-contrib")
    problems = la.audit(bundle)[1]
    assert len(problems) == 1 and problems[0].startswith("pyinstaller-hooks-contrib: license ")
    assert "GPL" in problems[0] and "is not allowed" in problems[0]


def test_libraries_in_the_framework_need_the_same_review_as_at_top_level(bundle):
    framework_lib = "Contents/Frameworks/Python.framework/Versions/3.13/lib"
    _put(bundle, f"{framework_lib}/libssl.3.dylib", MACHO)  # OpenSSL, reviewed
    found, problems = la.audit(bundle)
    assert problems == [] and f"{framework_lib}/libssl.3.dylib" in found["OpenSSL"]
    _put(bundle, f"{framework_lib}/libgmp.10.dylib", MACHO)  # in the interpreter, not reviewed
    _put(bundle, "Contents/Frameworks/libgmp.10.dylib", MACHO)
    assert set(la.audit(bundle)[1]) == {
        f"{framework_lib}/libgmp.10.dylib: belongs to no known component",
        "Contents/Frameworks/libgmp.10.dylib: belongs to no known component",
    }


def test_symlinks_inside_the_bundle_share_their_targets_component(bundle):
    _put(bundle, "Contents/Frameworks/Python.framework/Versions/3.13/Python", MACHO)
    (bundle / "Contents/Frameworks/Python.framework/Versions/Current").symlink_to("3.13")
    (bundle / "Contents/Frameworks/Python").symlink_to("Python.framework/Versions/Current/Python")
    found, problems = la.audit(bundle)
    assert problems == []
    assert "Contents/Frameworks/Python" in found["CPython"] and "Contents/Frameworks/Python" in found["mimalloc"]


def test_dangling_or_escaping_symlink_fails(bundle, tmp_path):
    outside = tmp_path / "libavcodec.61.dylib"
    outside.write_bytes(MACHO)
    (bundle / "Contents/Frameworks/libavcodec.61.dylib").symlink_to(outside)
    (bundle / "Contents/Frameworks/libgone.dylib").symlink_to("missing.dylib")
    assert la.audit(bundle)[1] == [
        f"Contents/Frameworks/libavcodec.61.dylib: symlink to {outside.resolve()}, outside the bundle",
        "Contents/Frameworks/libgone.dylib: symlink to nothing",
    ]


def test_only_expected_license_files_may_sit_in_the_licenses_folder(bundle):
    _put(bundle, "Contents/Resources/licenses/CPython/libevil.dylib", MACHO)
    _put(bundle, "Contents/Resources/licenses/FFmpeg/libavcodec.61.dylib", MACHO)
    assert la.audit(bundle)[1] == [
        "Contents/Resources/licenses/CPython/libevil.dylib: not an expected license file",
        "Contents/Resources/licenses/FFmpeg/libavcodec.61.dylib: not an expected license file",
    ]


@pytest.mark.parametrize("where", ["outside the bundle", "inside the bundle"])
def test_license_file_must_be_a_regular_file_in_the_bundle(bundle, tmp_path, where):
    shipped = bundle / "Contents/Resources/licenses/CPython/LICENSE.txt"
    copy = (tmp_path if where == "outside the bundle" else bundle / "Contents/Frameworks") / "LICENSE.txt"
    copy.write_bytes(shipped.read_bytes())  # the right text, but reached through a symlink
    shipped.unlink()
    shipped.symlink_to(copy)
    problems = la.audit(bundle)[1]
    assert "CPython: license file LICENSE.txt is not shipped in Contents/Resources/licenses/CPython/" in problems
    assert "Contents/Resources/licenses/CPython/LICENSE.txt: not an expected license file" not in problems


def test_helper_files_belong_to_llama_cpp_and_need_its_notices(bundle):
    _put(bundle, la.HELPER_SERVER, MACHO)
    _put(bundle, "Contents/Frameworks/llama-cpp/libggml-metal.0.dylib", MACHO)
    found, problems = la.audit(bundle)
    assert problems == [f"{la.HELPER}: license file LICENSES.txt is not shipped in "
                        f"Contents/Resources/licenses/{la.HELPER}/"]
    assert found[la.HELPER] == {la.HELPER_SERVER, "Contents/Frameworks/llama-cpp/libggml-metal.0.dylib"}
    _ship(bundle, la.HELPER)
    assert la.audit(bundle)[1] == []


def test_only_the_helpers_own_files_are_accepted_as_llama_cpp(bundle):
    _ship(bundle, la.HELPER)
    _put(bundle, "Contents/Frameworks/llama-cpp/libavcodec.61.dylib", MACHO)
    _put(bundle, "Contents/Frameworks/llama-cpp/libllama.0.dylib", b"not Mach-O")
    _put(bundle, "Contents/MacOS/llama-server", b"#!/bin/sh")
    assert set(la.audit(bundle)[1]) == {
        "Contents/Frameworks/llama-cpp/libavcodec.61.dylib: belongs to no known component",
        "Contents/Frameworks/llama-cpp/libllama.0.dylib: belongs to no known component",
        "Contents/MacOS/llama-server: belongs to no known component",
    }


def test_llama_cpp_notices_cover_what_the_release_embeds():
    text = (la.ROOT / "tools/notices/llama.cpp/LICENSES.txt").read_text()
    for name in ("llama.cpp", "cpp-httplib", "BoringSSL", "jsonhpp"):
        assert f"License for {name}\n" in text


def test_app_metadata_and_pyinstallers_icon_are_known(bundle):
    _put(bundle, "Contents/Info.plist")
    _put(bundle, "Contents/_CodeSignature/CodeResources")
    _put(bundle, "Contents/Resources/llama-server.sha256.json")  # the helper's hashes, as the build wrote them
    _put(bundle, "Contents/Resources/icon-windowed.icns", la._pyinstaller_icon().read_bytes())
    _ship(bundle, "PyInstaller")
    _ship(bundle, "Scholia")
    found, problems = la.audit(bundle)
    assert problems == []
    assert "Contents/Resources/icon-windowed.icns" in found["PyInstaller"]
    _put(bundle, "Contents/Resources/icon-windowed.icns", b"another icon")
    _put(bundle, "Contents/PlugIns/evil.dylib", MACHO)
    assert set(la.audit(bundle)[1]) == {
        "Contents/Resources/icon-windowed.icns: belongs to no known component",
        "Contents/PlugIns/evil.dylib: belongs to no known component",
    }


def test_native_code_is_reviewed_file_by_file(bundle):
    _put(bundle, "Contents/Frameworks/apsw/__init__.cpython-313-darwin.so", MACHO)
    _ship(bundle, "apsw")
    found, problems = la.audit(bundle)
    assert problems == [] and "SQLite" in found
    # apsw's Unicode tables are a separate native file, not reviewed
    _put(bundle, "Contents/Frameworks/apsw/_unicode.cpython-313-darwin.so", MACHO)
    assert la.audit(bundle)[1] == [
        "Contents/Frameworks/apsw/_unicode.cpython-313-darwin.so: native code from apsw "
        "has not been reviewed for the libraries it embeds"
    ]


def test_reviewed_licenses_and_supplied_notices():
    assert la.component("apsw")[0] == "Zlib"
    assert la.allowed(la.component("sqlite-vec")[0])
    for name in la.SUPPLIED_NOTICES:
        files = la.component(name)[1]
        assert files and all(source.is_file() for source, _ in files), name
    # The supplied PyObjC text is the one the Cocoa wheel ships for the same release.
    cocoa = [source for source, _ in la.component("pyobjc-framework-Cocoa")[1]]
    assert [p.read_bytes() for p in cocoa] == [
        (la.ROOT / "tools/notices/pyobjc/License.txt").read_bytes()
    ]


def test_pyinstallers_stand_in_for_dots_in_folder_names():
    from PyInstaller.building.osx import DOT_REPLACEMENT

    assert la.DOT == DOT_REPLACEMENT
    assert la._inner("Contents/Frameworks/python3__dot__13/lib-dynload/_json.so") == (
        "python3.13/lib-dynload/_json.so"
    )
    assert la._inner("Contents/Frameworks/a/b__dot__c.dylib") == "a/b__dot__c.dylib"


def test_the_zip_encryption_packages_are_reviewed_and_ship_their_licenses(bundle):
    assert la.component("pyzipper")[0] == "MIT AND PSF-2.0" and la.allowed("MIT AND PSF-2.0")
    assert la.component("pycryptodomex")[0] == "BSD-2-Clause AND Unlicense" and la.allowed("BSD-2-Clause AND Unlicense")
    for name, expected in (("pyzipper", {"LICENSE", "LICENSE.python"}), ("pycryptodomex", {"LICENSE.rst"})):
        assert expected <= {dest for _, dest in la.component(name)[1]}, name
    # Every native file of the installed pycryptodomex is covered by its review, and no other.
    # The review is of this version: another one is reviewed again before it ships.
    assert metadata.version("pycryptodomex") == "3.23.0"
    natives = [f.as_posix() for f in metadata.distribution("pycryptodomex").files if f.suffix == ".so"]
    assert len(natives) == 40
    for native in natives:
        _put(bundle, f"Contents/Frameworks/{native}", MACHO)
    _ship(bundle, "pycryptodomex")
    found, problems = la.audit(bundle)
    assert problems == [] and len(found["pycryptodomex"]) == 40


def test_pdfium_is_reviewed_with_the_libraries_built_into_it_and_their_notices_ship(bundle):
    assert la.component("pypdfium2")[0] == "Apache-2.0 OR BSD-3-Clause" and la.allowed("Apache-2.0 OR BSD-3-Clause")
    assert la.allowed(la.component(la.PDFIUM)[0])
    # The build's license list ships with pypdfium2's own license files, one per library built in.
    shipped = {dest for _, dest in la.component("pypdfium2")[1]}
    for library in ("pdfium", "freetype", "libjpeg_turbo.ijg", "libpng", "zlib", "icu", "lcms", "libopenjpeg",
                    "abseil", "agg23", "fast_float", "llvm-libc", "simdutf", "pdfium-binaries"):
        assert any(dest.rsplit("/", 1)[-1].startswith(library) for dest in shipped), library
    # Its one native file is covered by the review, as PDFium, at the reviewed version.
    assert metadata.version("pypdfium2") == "5.14.0"
    natives = [f.as_posix() for f in metadata.distribution("pypdfium2").files if f.suffix in (".dylib", ".so")]
    assert natives == ["pypdfium2_raw/libpdfium.dylib"]
    _put(bundle, f"Contents/Frameworks/{natives[0]}", MACHO)
    _ship(bundle, "pypdfium2")
    found, problems = la.audit(bundle)
    assert problems == [] and la.PDFIUM in found and "pypdfium2" in found


def test_pylatexenc_is_mit_and_ships_its_license():
    assert la.component("pylatexenc")[0] == "MIT"
    assert {dest for _, dest in la.component("pylatexenc")[1]} == {"LICENSE.txt"}
