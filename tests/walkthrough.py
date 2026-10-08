"""Serves the app for interface work and rendered walkthroughs, never with real data.

    uv run python tests/walkthrough.py [--port N] [--dev] [--request-log FILE]

The backend runs on a new temporary data folder, with an in-memory credential store
(never the Keychain) and a test-owned provider that answers with synthetic text, behind
the test network block (network_guard.py), so this process reaches nothing but its own
listener. It serves the built interface (frontend/dist) and prints the window's address
with this launch's session. --dev serves no interface and admits the Vite server on
127.0.0.1:5173 instead (`npm run dev` in frontend/), with no session, on port 8765, where
that server sends API requests. Without --port, a free port is used. --request-log appends
each request the test-owned provider receives to FILE, one JSON line each.

tests/walkthrough_driver.mjs drives the rendered walkthrough against this server.
"""

import argparse
import asyncio
import json
import secrets
import socket
import sys
import tempfile
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parents[1])]
import network_guard  # noqa: E402

# pycryptodomex learns the CPU architecture once, through platform.architecture(), which runs
# the local `file` command; the network block refuses every subprocess, so encrypted backups
# and exports (pyzipper) would fail under it. Loading it first runs that one local command
# before the block starts.
from Cryptodome.Hash import SHA1  # noqa: E402, F401

network_guard.start()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from scholia_app import FakeKeyring, MockProvider  # noqa: E402

from backend.app import create_app  # noqa: E402
from backend.budget_router import MODEL_TIERS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ANSWER = """A cohort study follows a group of people over time to see who develops an outcome.

1. **Prospective** cohorts enrol people now and follow them forward.
2. **Retrospective** cohorts use records that already exist.

> Exposure is measured before the outcome occurs.

A figure from the source would appear as a link: ![Survival curve](https://example.org/figure.png)

队列研究的关键在于暴露先于结局被测量。"""


# A synthetic model listing: the router's preferred models, with made-up windows and prices,
# and two others (one with no reported window).
CATALOG = [{"id": model, "name": model.split("/", 1)[1].replace("-", " ").title(), "context_length": 131072 * (i % 3 + 1),
            "pricing": {"prompt": f"{0.0000002 * (i + 1):.10f}", "completion": f"{0.0000008 * (i + 1):.10f}"},
            "supported_parameters": ["tools", "reasoning"]}
           for i, model in enumerate(dict.fromkeys(m for tier in MODEL_TIERS.values() for m in tier))]
CATALOG += [{"id": "example/long-context-mini", "name": "Long Context Mini", "context_length": 1000000,
             "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
            {"id": "example/unlisted-window", "name": "Unlisted Window", "pricing": {"prompt": "0", "completion": "0"}}]


class SyntheticProvider(MockProvider):
    log = None  # the --request-log file

    async def __call__(self, request):
        if self.log is not None:
            body = json.loads(request.content) if request.content else {}
            with open(self.log, "a", encoding="utf-8") as out:
                out.write(json.dumps({"method": request.method, "host": request.url.host, "path": request.url.path,
                                      "model": body.get("model") if isinstance(body, dict) else None}) + "\n")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOG})
        return await super().__call__(request)


def synthetic(body):
    first = ((body or {}).get("messages") or [{}])[0].get("content", "")
    text = "Designing a cohort study" if first.startswith("Write a title") else ANSWER
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                 "usage": {"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300, "cost": 0.0042}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int)
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--request-log", type=Path)
    args = parser.parse_args(argv)

    provider = SyntheticProvider(zero_retention=[model["id"] for model in CATALOG[::2]])  # half have zero retention
    provider.replies = [synthetic] * 1000
    provider.title_replies = [synthetic] * 1000
    provider.log = args.request_log
    data_dir = Path(tempfile.mkdtemp(prefix="scholia-walkthrough-"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port if args.port is not None else 8765 if args.dev else 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    session = None if args.dev else secrets.token_urlsafe(32)
    app = create_app(data_dir, origin=origin, session=session,
                     dev_origins=("http://127.0.0.1:5173",) if args.dev else (),
                     frontend_dir=None if args.dev else ROOT / "frontend" / "dist",
                     keyring_backend=FakeKeyring(), transport=httpx.MockTransport(provider))
    print(f"data folder: {data_dir}", flush=True)
    print(f"open: {origin}/" + ("" if args.dev else f"#session={session}"), flush=True)
    server = uvicorn.Server(uvicorn.Config(app, loop="asyncio", http="h11", ws="none", log_level="warning"))
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
