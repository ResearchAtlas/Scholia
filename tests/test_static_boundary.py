"""Static files are served only from inside the built interface's folder.

Canary files outside the build root stand in for credentials and records; no
path form may reach them.
"""

import os

import pytest

from backend.app import static_file
from scholia_app import started


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "build"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Scholia</title>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    (root / ".env").write_text("SECRET=canary")
    (root / "assets" / ".hidden").write_text("canary")
    (tmp_path / "credentials.json").write_text('{"openrouter": "canary-key"}')
    os.symlink(tmp_path / "credentials.json", root / "assets" / "escape.json")
    os.symlink(root / "assets" / "app.js", root / "assets" / "inside.js")
    return root


@pytest.mark.parametrize("path, served", [
    ("", "index.html"),
    ("index.html", "index.html"),
    ("assets/app.js", "assets/app.js"),
    ("assets/inside.js", "assets/app.js"),  # a link that stays inside
    ("projects/123/conversations", "index.html"),  # an app route
    ("assets", "index.html"),  # a folder is not a file
])
def test_built_files_and_app_routes_are_served(layout, path, served):
    assert static_file(layout.resolve(), path) == (layout / served).resolve()


@pytest.mark.parametrize("path", [
    "../credentials.json", "assets/../../credentials.json", "assets/../index.html", "./index.html",
    "/etc/passwd", "//etc/passwd", "assets//app.js", "assets\\..\\..\\credentials.json", "..\\credentials.json",
    "C:/Windows/win.ini", "assets/app.js:stream", "index.html\x00.js", "assets/app.js\n",
    ".env", "assets/.hidden", "assets/escape.json", "assets/missing.js", "favicon.ico",
])
def test_nothing_outside_or_hidden_or_missing_is_served(layout, path):
    assert static_file(layout.resolve(), path) is None


def test_without_a_build_nothing_is_served(tmp_path):
    assert static_file(None, "index.html") is None
    assert static_file(tmp_path / "nowhere", "") is None


@pytest.mark.asyncio
async def test_over_http_traversal_is_refused_and_unknown_api_paths_never_fall_back(tmp_path, layout):
    async with started(tmp_path / "data", frontend_dir=layout) as client:
        page = await client.get("/")
        assert page.text.startswith("<!doctype html>")
        policy = dict(part.strip().split(" ", 1) for part in page.headers["content-security-policy"].split(";"))
        assert policy["img-src"] == "'self' data:" and policy["default-src"] == "'self'"  # PR02A: no remote images
        assert "script-src" not in policy and "'unsafe-inline'" not in policy["default-src"]
        assert (await client.get("/projects/x")).text.startswith("<!doctype html>")
        for path in ("/%2e%2e/credentials.json", "/assets/%2e%2e/%2e%2e/credentials.json", "/.env",
                     "/assets/escape.json", "/assets/..%2f..%2fcredentials.json"):
            response = await client.get(path)
            assert response.status_code == 404 and b"canary" not in response.content, path
        response = await client.get("/api/no-such-endpoint")
        assert (response.status_code, response.json()["code"]) == (404, "not_found")
        response = await client.post("/api/no-such-endpoint", json={})
        assert response.status_code == 404 and "doctype" not in response.text
