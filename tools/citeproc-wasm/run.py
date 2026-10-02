"""Spike: run citeproc.wasm through wasmtime with a small English and Chinese input.

    python tools/citeproc-wasm/run.py build/citeproc-wasm/citeproc.wasm

Prints JSON with the module's size, SHA-256, how many GMP symbol names appear in it, the
compile and run times, and citeproc's output; exits non-zero if the output is wrong or
the module shows GMP code.
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


def formatted(code: int, out: str) -> tuple[list | None, str]:
    """citeproc's bibliography and citation if the run succeeded with the expected output."""
    try:
        result = json.loads(out)
        citation = result["citations"][0]
        bibliography = " ".join(entry for _, entry in result["bibliography"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None, ""
    expected = "Garcia 2024" in citation and "张" in citation and "学术研究" in bibliography
    return (result["bibliography"], citation) if code == 0 and expected else (None, "")


def main(path: str) -> int:
    data = Path(path).read_bytes()
    engine = Engine()
    started = time.perf_counter()
    module = Module(engine, data)
    compiled = time.perf_counter() - started
    runs = []
    for _ in range(3):  # every run must succeed, not just the last; each in a fresh folder
        with tempfile.TemporaryDirectory() as folder:
            started = time.perf_counter()
            code, out, err = run(engine, module, Path(folder))
            seconds = round(time.perf_counter() - started, 3)
        bibliography, citation = formatted(code, out)
        runs.append({"seconds": seconds, "exit_code": code, "ok": bibliography is not None,
                     "citation": citation, "bibliography": bibliography,
                     "stdout": "" if bibliography else out[-2000:], "stderr": err[-2000:]})
    gmp, rts = data.count(b"__gmp"), data.count(b"stg_")
    # Success needs every run right and no GMP code, judged by symbol names, which are
    # there unless stripped (the GHC runtime's are the control).
    ok = all(r["ok"] for r in runs) and gmp == 0 and rts > 0
    print(json.dumps({
        "ok": ok,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "gmp_symbols": gmp,
        "rts_symbols": rts,
        "compile_seconds": round(compiled, 3),
        "runs": runs,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
