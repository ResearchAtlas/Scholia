# PyInstaller spec for the runtime probe: onedir, no UPX, shipping the license texts
# that tools/license_audit.py requires.
#     uv run pyinstaller --noconfirm --clean --distpath build/dist --workpath build/work tools/runtime_probe.spec
import sys

sys.path.insert(0, SPECPATH)
from license_audit import notice_datas  # noqa: E402

a = Analysis([f"{SPECPATH}/runtime_probe.py"], datas=notice_datas())
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="scholia-probe", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="scholia-probe", upx=False)
