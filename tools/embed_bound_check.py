"""Embedding inputs past the helper's slot, against the real pinned helper (S1-18's finding; slice 1
sections 7.1 and 13). Outside the test suite, like tools/helper_timings.py.

    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python tools/embed_bound_check.py \\
        --helper <app>/Contents/MacOS/llama-server --model <Qwen3-Embedding-0.6B-Q8_0.gguf> [--out FILE]

The inputs are synthetic and named here (INPUTS), shaped as S1-18 found LitSearch's: titles of
private-use-area characters (the glyphs PDF text extraction leaves unmapped) of about 1,500 and 900
characters, and Han passages of about 2,169 characters (S1-16's synthetic sentences, and characters
seldom side by side, which take more tokens), each made an index text (title, section path, passage)
as search_index.index_text makes it; and a question of 1,000 private-use characters. The
backend runs in this process on a new temporary data folder, with an in-memory credential store; the
model is imported through the API and the helper the app starts is driven through the outbound gate.
For each input it records the helper's own token count (/tokenize, without and with the special
tokens), whether the helper refuses the whole input (HTTP 400, as before the fix), and the prefix
Helper.embed sends now: its token count and that its embedding came back.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402

from backend import local_helper, search  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.local_helper import EMBEDDING, InputRefused  # noqa: E402
from backend.search_index import index_text  # noqa: E402
from backend.self_test import _Keys  # noqa: E402
from tools.helper_timings import ZH_SENTENCES, ZH_WORDS, _paragraph, helpers_of, sandboxed, sockets  # noqa: E402

ORIGIN = "http://127.0.0.1:1"
GLYPHS = "".join(chr(0xE000 + (i * 37) % 0x1900) for i in range(1500))  # private-use characters, varied
_RNG = random.Random(20261015)
HAN = _paragraph(_RNG, ZH_WORDS, ZH_SENTENCES, 2169, "")  # S1-16's synthetic Chinese sentences, to about 2,169
RARE = "".join(chr(0x4E00 + (i * 7919) % 0x5100) for i in range(2169))  # Han characters seldom side by side
INPUTS = {
    "private-use title, 1,500 characters": index_text(GLYPHS, ["Introduction"], "Minimum wages and employment."),
    "private-use title, 900 characters": index_text(GLYPHS[:900], ["Methods"], "A synthetic panel of regions."),
    "Han passage, 2,169 characters (S1-16's sentences)": index_text("最低工资研究", ["研究发现"], HAN),
    "Han passage, 2,169 characters seldom side by side": index_text("最低工资研究", ["研究发现"], RARE),
}
QUESTION = search.QUERY_INSTRUCTION + GLYPHS[:1000]


async def count(helper, text, special):
    reply = await helper._post("/tokenize", {"content": text, "add_special": special}, None)
    return len(reply["tokens"])


async def check(helper, state, text, query):
    found = {"characters": len(text), "utf8_bytes": len(text.encode()),
             "tokens": await count(helper, text, False), "tokens_with_special": await count(helper, text, True)}
    try:  # the whole input, as it was sent before the fix
        await helper._post("/v1/embeddings", {"input": [text]}, None)
        found["whole_input"] = "embedded"
    except InputRefused:
        found["whole_input"] = "refused (HTTP 400)"
    bounded = await helper._bounded(text, None, None)
    [vector] = await local_helper.embed(state, [text], query=query)
    found["sent"] = {"characters": len(bounded), "is_prefix": text.startswith(bounded),
                     "tokens": await count(helper, bounded, False),
                     "tokens_with_special": await count(helper, bounded, True), "dimensions": len(vector)}
    return found


async def run(args):
    with tempfile.TemporaryDirectory(prefix="scholia-embed-bound-") as folder:
        data = Path(folder)
        app = create_app(data, origin=ORIGIN, keyring_backend=_Keys(), helper=local_helper.Config(binary=Path(args.helper)))
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                         headers={"X-Scholia-Client": "local"}, timeout=600) as client:
                response = await client.post("/api/helper/models/import",
                                             json={"model": EMBEDDING, "path": str(Path(args.model).resolve())})
                assert response.status_code == 200, response.text
                state = app.app.state.scholia
                helper = state["local_helper"].helpers[EMBEDDING]
                await local_helper.embed(state, ["start"], query=True)
                results = {"sandboxed": sandboxed(), "slot_tokens": local_helper.CONTEXT // local_helper.SLOTS,
                           "input_tokens": local_helper.INPUT_TOKENS, "inputs": {},
                           "runtime_sha256": search.identity(local_helper.Config(binary=Path(args.helper)))["runtime"]}
                for name, text in INPUTS.items():
                    results["inputs"][name] = await check(helper, state, text, query=False)
                results["inputs"]["question of 1,000 private-use characters"] = await check(helper, state, QUESTION,
                                                                                            query=True)
                results["helper_sockets"] = [name for pid in helpers_of(os.getpid()) for name in sockets(pid)]
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--helper", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    text = json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
