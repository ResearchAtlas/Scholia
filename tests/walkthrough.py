"""Serves the app for interface work and rendered walkthroughs, never with real data.

    uv run python tests/walkthrough.py [--port N] [--dev]

The backend runs on a new temporary data folder, with an in-memory credential store
(never the Keychain) and a test-owned provider that answers with synthetic text, behind
the test network block (network_guard.py), so this process reaches nothing but its own
listener. It serves the built interface (frontend/dist) and prints the window's address
with this launch's session. --dev serves no interface and admits the Vite server on
127.0.0.1:5173 instead (`npm run dev` in frontend/), with no session, on port 8765, where
that server sends API requests. Without --port, a free port is used.
"""

import argparse
import asyncio
import secrets
import socket
import sys
import tempfile
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parents[1])]
import network_guard  # noqa: E402

network_guard.start()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from scholia_app import FakeKeyring, MockProvider  # noqa: E402

from backend.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ANSWER = """A cohort study follows a group of people over time to see who develops an outcome.

1. **Prospective** cohorts enrol people now and follow them forward.
2. **Retrospective** cohorts use records that already exist.

> Exposure is measured before the outcome occurs.

A figure from the source would appear as a link: ![Survival curve](https://example.org/figure.png)

队列研究的关键在于暴露先于结局被测量。"""


def synthetic(body):
    first = ((body or {}).get("messages") or [{}])[0].get("content", "")
    text = "Designing a cohort study" if first.startswith("Write a title") else ANSWER
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                 "usage": {"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300, "cost": 0.0042}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int)
    parser.add_argument("--dev", action="store_true")
    args = parser.parse_args(argv)

    provider = MockProvider()
    provider.replies = [synthetic] * 1000
    provider.title_replies = [synthetic] * 1000
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
