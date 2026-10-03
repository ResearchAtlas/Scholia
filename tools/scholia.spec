# PyInstaller spec for the app: an onedir .app built through BUNDLE, without UPX, shipping
# the license texts that tools/license_audit.py requires. tools/build_app.sh runs it, then
# adds the llama.cpp helper and makes the DMG.
#     uv run pyinstaller --noconfirm --clean --distpath build/dist --workpath build/work tools/scholia.spec
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "tools"))
from license_audit import NPM, notice_datas, npm_packages  # noqa: E402

# Distributions the app bundles, whose license files ship with it; the audit fails if one
# is bundled and missing here.
DISTRIBUTIONS = [
    "apsw", "sqlite-vec", "pyobjc-core", "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz",
    "pyobjc-framework-CoreML", "pyobjc-framework-Vision",
    # The backend: its server stack, settings and keys
    "fastapi", "starlette", "pydantic", "pydantic_core", "annotated-types", "annotated-doc",
    "typing-inspection", "typing_extensions", "anyio", "sniffio", "idna", "uvicorn", "h11", "click",
    "python-multipart", "httpx", "httpcore", "certifi", "tomlkit", "keyring", "jaraco.classes",
    "jaraco.context", "jaraco.functools", "more-itertools", "platformdirs",
    # The window
    "pywebview", "bottle", "proxy_tools", "pyobjc-framework-WebKit", "pyobjc-framework-UniformTypeIdentifiers",
]

# Optional pieces the app never uses, which hooks or optional imports would otherwise collect:
# uvicorn's faster loop and parser, its websockets, reload and config-file support (the app
# runs it with loop="asyncio", http="h11", ws="none" and no config file), and test and
# build tooling.
EXCLUDES = [
    "uvloop", "httptools", "websockets", "wsproto", "watchfiles", "yaml", "dotenv",
    "pytest", "_pytest", "pluggy", "iniconfig", "pygments", "setuptools", "pkg_resources", "_distutils_hack",
]

a = Analysis(
    [str(ROOT / "tools/app.py")],
    pathex=[str(ROOT)],
    binaries=collect_dynamic_libs("sqlite_vec"),  # sqlite-vec has no PyInstaller hook
    excludes=EXCLUDES,
    # the license texts, of the npm packages the interface bundles too (build_app.sh builds it first)
    datas=notice_datas(DISTRIBUTIONS + [NPM + name for name in npm_packages()])
    # the reasoning-capability record and the Private allowlist, which their modules read beside them
    + [(str(ROOT / "backend/reasoning_capabilities.json"), "backend"), (str(ROOT / "backend/private_routes.json"), "backend")]
    # the built interface, which the desktop entry serves from here (backend/desktop.py)
    + [(str(ROOT / "frontend/dist"), "frontend")],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="Scholia", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Scholia", upx=False)
app = BUNDLE(
    coll,
    name="Scholia.app",
    bundle_identifier="io.github.researchatlas.scholia",
    version=tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
    # The highest minimum among the bundled binaries (sqlite-vec's library); build_app.sh
    # checks that none needs a later macOS.
    info_plist={"LSMinimumSystemVersion": "14.0"},
)
