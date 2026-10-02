"""Carried-over modules: headers naming their source, no legacy configuration, no import side effects.

A child process with its own home folder imports every carried module and runs
the app beside synthetic canaries: the current app's data folder ("AI Advisory
Board") and a `.env` file, in the working folder and in that data folder, plus a
provider key in the environment. An audit hook in the child records every file
opened or listed; none of the canaries may be touched, the environment's key
may not be used, and importing may create no file. The child opens no socket:
its model calls are answered in process.
"""

import json
import subprocess
import sys
from pathlib import Path

from network_guard import allow_subprocess

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "b5d687820e88c10de25a9a2343d3cc478e497524"
CARRIED = ["openrouter.py", "openrouter_client.py", "reasoning_capability.py", "reasoning_control.py",
           "endpoint_pricing.py", "budget_router.py"]

CHILD = r"""
import asyncio, json, os, sys

home, work = sys.argv[1], sys.argv[2]
watched = (os.path.join(home, "Library", "Application Support", "AI Advisory Board"), os.path.join(work, ".env"))
touched = []
recording = [True]


def files():  # the test's own listing, not recorded
    recording[0] = False
    try:
        return sorted(os.path.relpath(os.path.join(d, f), home) for d, _, fs in os.walk(home) for f in fs)
    finally:
        recording[0] = True


def hook(event, args):
    if recording[0] and event in ("open", "os.listdir", "os.scandir", "os.stat") and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        if any(path == w or path.startswith(w + os.sep) for w in watched):
            touched.append((event, path))


sys.addaudithook(hook)
before = files()
import backend.openrouter, backend.openrouter_client, backend.reasoning_capability, backend.reasoning_control
import backend.endpoint_pricing, backend.budget_router, backend.app, backend.desktop
after_import = files()

import httpx
from backend.app import create_app
from backend.desktop import data_folder

sent_keys = []


async def provider(request):
    sent_keys.append(request.headers.get("authorization"))
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.001}})


class Keyring:
    keys = {}

    def get_password(self, service, name):
        return self.keys.get((service, name))

    def set_password(self, service, name, value):
        self.keys[(service, name)] = value

    def delete_password(self, service, name):
        self.keys.pop((service, name), None)


async def main():
    origin = "http://127.0.0.1:8765"
    app = create_app(data_folder(), origin=origin, keyring_backend=Keyring(), transport=httpx.MockTransport(provider))
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                     headers={"X-Scholia-Client": "local"}) as client:
            await client.post("/api/setup", json={"openrouter_key": "sk-or-keyring-key"})
            conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
            response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
            return response.text


stream = asyncio.run(main())
print(json.dumps({"touched": touched, "before": before, "after_import": after_import, "sent_keys": sent_keys,
                  "data_folder": str(data_folder()), "succeeded": '"succeeded"' in stream}))
"""


def test_each_carried_module_names_its_source_and_commit():
    for name in CARRIED:
        head = (ROOT / "backend" / name).read_text().splitlines()[:2]
        assert head[0] == f"# Carried over from AI Advisory Board, backend/{name} at commit", name
        assert head[1].startswith(f"# {COMMIT}"), name


def test_carried_modules_read_no_legacy_configuration_and_import_without_side_effects(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    legacy = home / "Library" / "Application Support" / "AI Advisory Board"
    legacy.mkdir(parents=True)
    work.mkdir()
    (legacy / ".env").write_text("OPENROUTER_API_KEY=sk-or-legacy-canary\n")
    (legacy / "conversations").mkdir()
    (legacy / "conversations" / "canary.json").write_text('{"messages": ["legacy canary"]}')
    (legacy / "config.json").write_text('{"chairman_model": "canary/model"}')
    (work / ".env").write_text("OPENROUTER_API_KEY=sk-or-dotenv-canary\nOPENROUTER_BASE_URL=https://canary.example\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT),
           "OPENROUTER_API_KEY": "sk-or-environment-canary", "OPENROUTER_BASE_URL": "https://canary.example/v1"}

    with allow_subprocess(sys.executable):  # the child's model calls are answered in process; it opens no socket
        child = subprocess.run([sys.executable, "-c", CHILD, str(home), str(work)], cwd=work, env=env,
                               capture_output=True, timeout=120)
    assert child.returncode == 0, child.stderr.decode()[-3000:]
    result = json.loads(child.stdout.decode().strip().splitlines()[-1])

    assert result["touched"] == []
    assert result["before"] == result["after_import"]  # importing created no file
    assert result["succeeded"]
    assert result["sent_keys"] == ["Bearer sk-or-keyring-key"]  # the keyring's, never the environment's or a .env file's
    assert result["data_folder"] == str(home / "Library" / "Application Support" / "Scholia")
