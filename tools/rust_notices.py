"""Collect the license notices of the Rust crates compiled into a bundled extension.

    python tools/rust_notices.py <name> <version> <Cargo.lock URL> <output file>

A Rust extension (such as pydantic-core's) links every crate it uses into its
binary, and their licenses ask for their notices to ship with it. This reads the
extension's Cargo.lock at the shipped version, downloads each crate from
crates.io, checks the download against the lock file's checksum, and copies the
crate's own license, notice, copyright and authors files into one file, with the crate's
declared license. Every crate in the lock file is included, not only those the
binary links, so nothing is missed. It prints the licenses found, as one SPDX
expression for the license audit. The Rust standard library's notices follow.

The output is committed; tools/license_audit.py ships it with the app and checks
that its header names the version the app bundles. Run it again when that
version changes. Standard library only.
"""

import hashlib
import io
import re
import sys
import tarfile
import tomllib
import urllib.request

CRATES = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
# The Rust standard library, linked into every Rust binary, at a pinned release.
RUST_STD = "https://raw.githubusercontent.com/rust-lang/rust/1.91.0/{file}"
RUST_STD_FILES = ("COPYRIGHT", "LICENSE-MIT", "LICENSE-APACHE")
NOTICE = re.compile(r"(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|UNLICENSE|AUTHORS)([-._].*)?$", re.IGNORECASE)


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def crate_notices(name, version, checksum):
    """(declared license, [(file name, text)]) from the crate's verified source package."""
    data = fetch(CRATES.format(name=name, version=version))
    if hashlib.sha256(data).hexdigest() != checksum:
        raise SystemExit(f"{name} {version}: the download does not match Cargo.lock's checksum")
    files, license = [], None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = member.name.split("/")
            if not member.isfile() or len(parts) != 2:  # files at the crate's root only
                continue
            if parts[1] == "Cargo.toml":
                manifest = tomllib.loads(archive.extractfile(member).read().decode("utf-8"))
                license = manifest.get("package", {}).get("license")
            elif NOTICE.match(parts[1]):
                files.append((parts[1], archive.extractfile(member).read().decode("utf-8", "replace")))
    return license, sorted(files)


def main(argv):
    name, version, lock_url, output = argv
    lock_bytes = fetch(lock_url)
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    sections, licenses, seen = [], set(), {}
    for package in sorted(lock["package"], key=lambda p: (p["name"], p["version"])):
        if package.get("source") != REGISTRY:
            continue  # the extension itself
        license, files = crate_notices(package["name"], package["version"], package["checksum"])
        if not license:
            raise SystemExit(f"{package['name']} {package['version']}: no declared license")
        license = license.replace("/", " OR ")  # older crates write "MIT/Apache-2.0"
        licenses.add(license)
        if files:
            parts = []
            for file, text in files:
                text = text.rstrip()
                if text in seen:  # a license text many crates share is written out once
                    parts.append(f"---- {file}: the same text as {seen[text]} ----\n")
                else:
                    seen[text] = f"{package['name']} {package['version']}'s {file}"
                    parts.append(f"---- {file} ----\n{text}\n")
            body = "\n".join(parts)
        else:  # some crates ship no license file; their manifest's declaration is all there is
            body = f"(No license file in its source package; its manifest declares {license}.)\n"
        sections.append(f"==== {package['name']} {package['version']} ({license}) ====\n{body}")
    std = "\n".join(f"---- {file} ----\n{fetch(RUST_STD.format(file=file)).decode('utf-8').rstrip()}\n"
                    for file in RUST_STD_FILES)
    header = (
        f"Third-party notices for the Rust code compiled into {name} {version}.\n"
        f"Crates from its Cargo.lock ({lock_url}, sha256 {hashlib.sha256(lock_bytes).hexdigest()}),\n"
        "each with the license files of its crates.io source package, checked against the lock\n"
        "file's checksum; then the Rust standard library's notices.\n"
    )
    with open(output, "w", encoding="utf-8") as file:
        file.write(header + "\n" + "\n".join(sections) + f"\n==== Rust standard library ====\n{std}")
    print(" AND ".join(f"({expression})" if " " in expression else expression for expression in sorted(licenses)))


if __name__ == "__main__":
    main(sys.argv[1:])
