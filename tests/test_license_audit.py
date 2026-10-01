import sys
import zipfile
from importlib import metadata

import pytest

from tools import license_audit as la

DYNLOAD = "_internal/python3.13/lib-dynload"
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
    assert la.assign_module("script", "runtime_probe") == ["Scholia"]
    assert la.assign_module("module", "scholia_unknown_module") is None


def _put(root, rel, data=b""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _ship(root, name):
    for source, dest in la.component(name)[1]:
        _put(root, f"_internal/licenses/{name}/{dest}", source.read_bytes())


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
    _put(bundle, "_internal/Python.framework/Versions/3.13/Python", MACHO)  # genuine
    found, problems = la.audit(bundle)
    assert problems == [] and "_internal/Python.framework/Versions/3.13/Python" in found["CPython"]
    _put(bundle, f"{DYNLOAD}/_evil.cpython-313-darwin.so", MACHO)
    _put(bundle, f"{DYNLOAD}/libavcodec.61.dylib", MACHO)
    _put(bundle, "_internal/Python.framework/Versions/3.13/lib/libgmp.10.dylib", MACHO)
    assert set(la.audit(bundle)[1]) == {
        f"{DYNLOAD}/_evil.cpython-313-darwin.so: belongs to no known component",
        f"{DYNLOAD}/libavcodec.61.dylib: belongs to no known component",
        "_internal/Python.framework/Versions/3.13/lib/libgmp.10.dylib: belongs to no known component",
    }


def test_base_library_members_must_all_be_known(bundle):
    archive = bundle / "_internal/base_library.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("json/__init__.pyc", b"")
    assert la.audit(bundle)[1] == []
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("unknownpkg/__init__.pyc", b"")
        zf.writestr("libfoo.dylib", MACHO)
    assert la.audit(bundle)[1] == [
        "_internal/base_library.zip: member libfoo.dylib belongs to no known component",
        "_internal/base_library.zip: module unknownpkg.__init__ belongs to no known component",
    ]


def test_missing_or_empty_bundle_fails(tmp_path, capsys):
    missing = tmp_path / "missing"
    assert la.audit(missing)[1] == [f"{missing}: not a directory"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert la.audit(empty)[1] == [f"{empty}: the bundle contains no files"]
    assert la.main([str(missing)]) == 1
    assert f"FAIL {missing}: not a directory" in capsys.readouterr().out


def test_file_of_no_known_component_fails(bundle):
    _put(bundle, "_internal/libavcodec.61.dylib", MACHO)
    _put(bundle, "README.txt")
    problems = la.audit(bundle)[1]
    assert any(p.startswith("_internal/libavcodec.61.dylib: belongs to no known") for p in problems)
    assert any(p.startswith("README.txt: belongs to no known") for p in problems)


def test_disallowed_license_fails(bundle):
    _put(bundle, "_internal/certifi/cacert.pem")
    _ship(bundle, "certifi")
    assert la.audit(bundle)[1] == ["certifi: license MPL-2.0 is not allowed"]


def test_missing_or_changed_license_text_fails(bundle):
    shipped = bundle / "_internal/licenses/CPython/LICENSE.txt"
    shipped.write_bytes(shipped.read_bytes() + b"changed")
    (bundle / "_internal/licenses/CPython/license.rst.txt").unlink()
    problems = la.audit(bundle)[1]
    assert "CPython: license file LICENSE.txt is not shipped in licenses/CPython/" in problems
    assert "CPython: license file license.rst.txt is not shipped in licenses/CPython/" in problems


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
    _put(bundle, "_internal/pydantic_core/_pydantic_core.cpython-313-darwin.so", magic + bytes(28))
    _ship(bundle, "pydantic_core")
    problems = la.audit(bundle)[1]
    assert len(problems) == 1 and "has not been reviewed" in problems[0]


def test_data_file_from_a_distribution_is_not_native_code(bundle):
    _put(bundle, "_internal/pydantic_core/__init__.py", b"# Python source")
    _ship(bundle, "pydantic_core")
    assert la.audit(bundle)[1] == []


def test_runtime_hooks_are_assigned_to_their_own_licenses():
    assert la.assign_module("script", "pyi_rth_inspect") == ["PyInstaller"]
    assert la.assign_module("script", "pyi_rth_enchant") == [la.CONTRIB_RTHOOKS]
    assert la.assign_module("script", "pyi_rth_unknown_hook") is None


def test_community_runtime_hooks_are_apache_but_the_rest_of_their_distribution_is_not(bundle):
    _ship(bundle, la.CONTRIB_RTHOOKS)
    assert la._check(bundle, la.CONTRIB_RTHOOKS) == []
    (bundle / f"_internal/licenses/{la.CONTRIB_RTHOOKS}/LICENSE").unlink()
    assert la._check(bundle, la.CONTRIB_RTHOOKS) == [
        f"{la.CONTRIB_RTHOOKS}: license file LICENSE is not shipped in licenses/{la.CONTRIB_RTHOOKS}/"
    ]
    _put(bundle, "_internal/_pyinstaller_hooks_contrib/__init__.py")
    _ship(bundle, "pyinstaller-hooks-contrib")
    problems = la.audit(bundle)[1]
    assert len(problems) == 1 and problems[0].startswith("pyinstaller-hooks-contrib: license ")
    assert "GPL" in problems[0] and "is not allowed" in problems[0]


def test_libraries_in_the_framework_need_the_same_review_as_at_top_level(bundle):
    framework_lib = "_internal/Python.framework/Versions/3.13/lib"
    _put(bundle, f"{framework_lib}/libssl.3.dylib", MACHO)  # OpenSSL, reviewed
    found, problems = la.audit(bundle)
    assert problems == [] and f"{framework_lib}/libssl.3.dylib" in found["OpenSSL"]
    _put(bundle, f"{framework_lib}/libgmp.10.dylib", MACHO)  # in the interpreter, not reviewed
    _put(bundle, "_internal/libgmp.10.dylib", MACHO)
    assert set(la.audit(bundle)[1]) == {
        f"{framework_lib}/libgmp.10.dylib: belongs to no known component",
        "_internal/libgmp.10.dylib: belongs to no known component",
    }


def test_symlinks_inside_the_bundle_share_their_targets_component(bundle):
    _put(bundle, "_internal/Python.framework/Versions/3.13/Python", MACHO)
    (bundle / "_internal/Python.framework/Versions/Current").symlink_to("3.13")
    (bundle / "_internal/Python").symlink_to("Python.framework/Versions/Current/Python")
    found, problems = la.audit(bundle)
    assert problems == []
    assert "_internal/Python" in found["CPython"] and "_internal/Python" in found["mimalloc"]


def test_dangling_or_escaping_symlink_fails(bundle, tmp_path):
    outside = tmp_path / "libavcodec.61.dylib"
    outside.write_bytes(MACHO)
    (bundle / "_internal/libavcodec.61.dylib").symlink_to(outside)
    (bundle / "_internal/libgone.dylib").symlink_to("missing.dylib")
    assert la.audit(bundle)[1] == [
        f"_internal/libavcodec.61.dylib: symlink to {outside.resolve()}, outside the bundle",
        "_internal/libgone.dylib: symlink to nothing",
    ]
