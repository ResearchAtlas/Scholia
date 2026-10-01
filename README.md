# Scholia

Scholia is a local-first research platform for researchers working in English and Chinese.

It is in early development and not ready to use yet.

- **Platform:** macOS on Apple silicon first.
- **Name:** Scholia is the repository. The app keeps the product name AI Advisory Board for now, with the working name "AAB Research".
- **License:** MIT, in [LICENSE](LICENSE). Credits are in [CREDITS.md](CREDITS.md).

## Repository layout

| Folder | Holds |
|---|---|
| `backend/` | The Python backend |
| `frontend/` | The web interface |
| `tests/` | Automated tests. They run offline: every outbound connection is refused, except to mock servers a test starts and registers |
| `evals/` | Evaluation sets and their runners |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Node.js 24. uv installs CPython 3.13.

```bash
uv sync --locked
uv run pytest
cd frontend && npm ci && node --test
```

CI (`.github/workflows/ci.yml`) runs on every pull request on macOS on Apple silicon, with python.org's CPython 3.13: the backend and frontend tests, a PyInstaller build of a small frozen program (`tools/runtime_probe.spec`), and a license audit of that build (`tools/license_audit.py`). The audit expects python.org's interpreter, the one the app ships.
