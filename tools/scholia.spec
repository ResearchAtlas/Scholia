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
from license_audit import notice_datas  # noqa: E402

# Distributions the app bundles, whose license files ship with it; the audit fails if one
# is bundled and missing here.
DISTRIBUTIONS = [
    "apsw", "sqlite-vec", "pyobjc-core", "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz",
    "pyobjc-framework-CoreML", "pyobjc-framework-Vision",
]

a = Analysis(
    [str(ROOT / "tools/app.py")],
    pathex=[str(ROOT)],
    binaries=collect_dynamic_libs("sqlite_vec"),  # sqlite-vec has no PyInstaller hook
    datas=notice_datas(DISTRIBUTIONS),
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
