"""Download a pinned file and check its size and SHA-256 before anything uses it.

    python tools/fetch.py llama.cpp DEST_DIR
    python tools/fetch.py embedding-model DEST_DIR
    python tools/fetch.py reranker-model DEST_DIR   (ticket 70's reranker tests and timings only)

It prints the verified file's path. A file already in DEST_DIR, for example from a CI
cache, is checked the same way and downloaded again if it fails. A download that fails
the check is deleted, and the command fails. Each redirect followed is printed to stderr by its
hosts (never its query, which may be signed).
"""

import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.local_helper import EMBEDDING_MODEL, RERANKER_MODEL, mismatch  # noqa: E402

PINS = {
    # llama.cpp build b11146 for macOS arm64. The SHA-256 is the digest GitHub's release
    # API publishes for the asset.
    "llama.cpp": {
        "file": "llama-b11146-bin-macos-arm64.tar.gz",
        "size": 11_189_714,
        "sha256": "1ad3f9eff80edb9dbef4259ad564d1720612ef7eea48fa4afed0e54f5f3d5711",
        "url": "https://github.com/ggml-org/llama.cpp/releases/download/b11146/"
        "llama-b11146-bin-macos-arm64.tar.gz",
    },
    "embedding-model": EMBEDDING_MODEL,
    "reranker-model": RERANKER_MODEL,
}


class _Hops(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        print(f"redirect {code}: {urllib.parse.urlsplit(req.full_url).hostname} -> "
              f"{urllib.parse.urlsplit(newurl).hostname}", file=sys.stderr)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(pin: dict, dest: Path, url: str | None = None) -> Path:
    """The verified file `pin` names in `dest`, downloading it from `url` (default: the pin's)."""
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / pin["file"]
    if path.is_file() and mismatch(path, pin) is None:
        return path
    part = path.with_name(path.name + ".part")
    try:
        opener = urllib.request.build_opener(_Hops)
        with opener.open(url or pin["url"], timeout=60) as response, open(part, "wb") as f:
            while block := response.read(1 << 20):
                f.write(block)
        if error := mismatch(part, pin):
            raise RuntimeError(error)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path


if __name__ == "__main__":
    name, folder = sys.argv[1:]
    print(fetch(PINS[name], Path(folder)))
