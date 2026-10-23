# PyInstaller spec: one folder holding Python, every native library and the web GUI.
# Build with packaging/build.py, which also builds the GUI and zips the result.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
datas = [(str(ROOT / "src" / "imagelib" / "ui"), "imagelib/ui")]
binaries = []
hiddenimports = collect_submodules("uvicorn") + collect_submodules("imagelib") + ["multipart", "python_multipart"]
# Packages that ship native libraries (LibRaw, ffmpeg, exiv2, libheif, libjpeg-turbo) or data files.
for package in ("rawpy", "pillow_heif", "av", "exiv2", "simplejpeg", "blake3", "psd_tools"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

icon = str(ROOT / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.icns"))

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller", "IPython", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="imagelib",
    console=True,   # the window shows the address and is how the app is stopped
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="imagelib")
