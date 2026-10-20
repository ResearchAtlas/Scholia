"""Convert LitSearch's parquet files to JSONL the tier 1 runner reads with the standard library (S1-18).

    uv run --no-project --with pyarrow==25.0.1 python -I evals/litsearch_to_jsonl.py <archives folder> <out folder>

A preparation step, run once outside any test, never part of a run: LitSearch ships only as parquet,
and the project has no parquet reader. pyarrow 25.0.1 (released 2026-08-10) is installed by uv for
this command alone; the project gains no dependency. The input files are checked against the SHA-256
pinned here (those of the dataset repository at commit 9573fb284a1026c998df47024b888a163f0f0e25)
before anything is read; the outputs are corpus.jsonl (corpusid, title, abstract) and queries.jsonl
(query, corpusids, query_set, specificity, quality), in the parquet files' order, each written whole
to a temporary name and then renamed. Nothing is downloaded. The runner pins the outputs' SHA-256 in
evals/tier1_manifest.json.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

INPUTS = {
    "query/full-00000-of-00001.parquet": "38cdbe4a6b7a7f5776d08055bfbf6e4511000aa2a523866592b4bb273388b914",
    "corpus_clean/full-00000-of-00006.parquet": "f0477a38be6f83a87ac76e76c5c6fa9dd590c7e388bd0c9cf3a602b909e52d86",
    "corpus_clean/full-00001-of-00006.parquet": "a82e002f8423e4c150b2e07bc983bc25425baa90af4dd2b9877e54617ace5beb",
    "corpus_clean/full-00002-of-00006.parquet": "30ee83c73f547e9dc841e74f9f88e3f132018599bd3c04c2e06ad69750a54cd0",
    "corpus_clean/full-00003-of-00006.parquet": "bbc5b74d533e6d978a7621724864c24aa8a2e9c1ec7d1ace51f6c4ad7e62d9f6",
    "corpus_clean/full-00004-of-00006.parquet": "c9c110a457efe2553f90ff1a4419c1f4a6a50a8b49bce47bf89ff01b7871d6ad",
    "corpus_clean/full-00005-of-00006.parquet": "38d333622bd085d623c306c31a871b9d19c80230b28eeb9f0aaa555acc6e674a",
}
OUTPUTS = {"queries.jsonl": ("query/", ["query", "corpusids", "query_set", "specificity", "quality"]),
           "corpus.jsonl": ("corpus_clean/", ["corpusid", "title", "abstract"])}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def main(source, out):
    source, out = Path(source), Path(out)
    for name, pinned in INPUTS.items():  # every input checked before any is read
        found = sha256(source / name)
        if found != pinned:
            sys.exit(f"{name}: SHA-256 {found}, expected {pinned}; nothing converted")
    import pyarrow.parquet as pq

    out.mkdir(parents=True, exist_ok=True)
    for target, (prefix, columns) in OUTPUTS.items():
        part = out / (target + ".part")
        rows = 0
        with open(part, "w", encoding="utf-8") as f:
            for name in sorted(n for n in INPUTS if n.startswith(prefix)):
                for row in pq.read_table(source / name, columns=columns).to_pylist():
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    rows += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, out / target)
        print(json.dumps({"file": target, "rows": rows, "bytes": (out / target).stat().st_size,
                          "sha256": sha256(out / target)}))


if __name__ == "__main__":
    main(*sys.argv[1:])
