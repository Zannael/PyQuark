# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
LIBUSB = next(Path("/usr/lib").glob("*/libusb-1.0.so.0"), None)
if LIBUSB is None:
    raise RuntimeError("Install libusb-1.0-0 before building the AppImage")

hiddenimports = [
    "src",
    "src.protocol",
    "src.transport",
    "src.dbi_protocol",
    "src.session",
    "src.vfs",
    "src.vfs.core",
    "src.vfs.rar_stream",
    "src.xci_virtualizer",
    "usb",
    "usb.core",
    "usb.util",
    "rarfile",
    "multivolumefile",
]


a = Analysis(
    [str(ROOT / "quark_gui.py")],
    pathex=[str(ROOT)],
    # PyUSB loads libusb with ctypes; automatic import analysis cannot discover it.
    binaries=[(str(LIBUSB), ".")],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pyquark-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pyquark-gui",
)
