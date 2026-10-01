"""Spike: run citeproc.wasm through wasmtime with a small English and Chinese input.

    python tools/citeproc-wasm/run.py build/citeproc-wasm/citeproc.wasm

Prints JSON with the module's size, SHA-256, whether GMP symbols appear in it, the
compile and run times, and citeproc's output; exits non-zero if the output is wrong.
"""

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

from wasmtime import Engine, ExitTrap, Linker, Module, Store, WasiConfig

HERE = Path(__file__).parent
INPUT = {
    "style": (HERE / "style.csl").read_text(encoding="utf-8"),
    "references": [
        {"id": "a", "type": "article-journal", "title": "Local search",
         "author": [{"family": "Garcia", "given": "Ana"}], "issued": {"date-parts": [[2024]]}},
        {"id": "b", "type": "book", "title": "学术研究",
         "author": [{"family": "张", "given": "伟"}], "issued": {"date-parts": [[2023]]}},
    ],
    "citations": [{"citationID": "c1", "citationItems": [{"id": "a"}, {"id": "b"}]}],
}


def run(engine: Engine, module: Module, folder: Path) -> tuple[int, str, str]:
    (folder / "in.json").write_text(json.dumps(INPUT, ensure_ascii=False), encoding="utf-8")
    wasi = WasiConfig()
    wasi.argv = ["citeproc"]  # HTML output: each citation and entry is a string
    wasi.stdin_file = str(folder / "in.json")
    wasi.stdout_file = str(folder / "out.json")
    wasi.stderr_file = str(folder / "err.txt")
    store = Store(engine)
    store.set_wasi(wasi)
    linker = Linker(engine)
    linker.define_wasi()
    instance = linker.instantiate(store, module)
    try:
        instance.exports(store)["_start"](store)
        code = 0
    except ExitTrap as trap:
        code = trap.code
    return code, (folder / "out.json").read_text(encoding="utf-8"), (folder / "err.txt").read_text()


def main(path: str) -> int:
    data = Path(path).read_bytes()
    engine = Engine()
    started = time.perf_counter()
    module = Module(engine, data)
    compiled = time.perf_counter() - started
    timings = []
    with tempfile.TemporaryDirectory() as folder:
        for _ in range(3):
            started = time.perf_counter()
            code, out, err = run(engine, module, Path(folder))
            timings.append(round(time.perf_counter() - started, 3))
    try:
        result = json.loads(out)
        citation = result["citations"][0]
        bibliography = " ".join(entry for _, entry in result["bibliography"])
    except (ValueError, KeyError, IndexError, TypeError):
        result, citation, bibliography = None, "", ""
    ok = code == 0 and "Garcia 2024" in citation and "张" in citation and "学术研究" in bibliography
    print(json.dumps({
        "ok": ok,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "gmp_symbols": data.count(b"__gmp"),
        "compile_seconds": round(compiled, 3),
        "run_seconds": timings,
        "exit_code": code,
        "citation": citation,
        "bibliography": result["bibliography"] if result else None,
        "stdout": "" if result else out[-2000:],
        "stderr": err[-2000:],
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
