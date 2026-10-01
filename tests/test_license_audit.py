import shutil
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
    """A bundle holding CPython modules that embed SQLite and libmpdec, with CPython's texts."""
    doc = tmp_path / "license.rst.txt"
    doc.write_text("... libmpdec ...", encoding="utf-8")
    monkeypatch.setattr(la, "CPYTHON_DOC", doc)
    root = tmp_path / "bundle"
    for module in ("_json", "_sqlite3", "_decimal"):
        _put(root, f"{DYNLOAD}/{module}.cpython-313-darwin.so", MACHO)
    _ship(root, "CPython")
    return root


def test_clean_bundle_passes(bundle):
    found, problems = la.audit(bundle)
    assert problems == []
    assert set(found) == {"CPython", "SQLite", "libmpdec"}


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


def test_unreviewed_native_code_from_a_distribution_fails(bundle):
    _put(bundle, "_internal/pydantic_core/_pydantic_core.cpython-313-darwin.so", MACHO)
    _ship(bundle, "pydantic_core")
    problems = la.audit(bundle)[1]
    assert len(problems) == 1 and "has not been reviewed" in problems[0]
