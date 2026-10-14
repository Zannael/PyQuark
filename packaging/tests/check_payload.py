"""Check actual bundled dependencies and RAR reads without Switch hardware."""
import ctypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import rarfile
import usb.backend.libusb1

from src.vfs.rar_stream import RarVirtualHandle

appdir = Path(sys.argv[1]).resolve()
glibc_versions = []
for binary in appdir.rglob('*'):
    if not binary.is_file():
        continue
    with binary.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            continue
    versions = subprocess.run(['readelf', '--version-info', str(binary)],
                              capture_output=True, text=True, check=True).stdout
    glibc_versions.extend(tuple(map(int, version)) for version in
                          re.findall(r'GLIBC_(\d+)\.(\d+)', versions))
assert glibc_versions and max(glibc_versions) <= (2, 35), max(glibc_versions)
print(f'PASS: maximum bundled glibc requirement: {max(glibc_versions)}')
assert shutil.which('unrar') is None, 'Run this check without a system UnRAR'
unrar = appdir / 'usr/bin/unrar'
os.environ['PYQUARK_UNRAR_CMD'] = str(unrar)
os.environ['PATH'] = f"{unrar.parent}:{os.environ['PATH']}"
rarfile.UNRAR_TOOL = str(unrar)
assert shutil.which('unrar') == str(unrar)
subprocess.run([str(unrar)], stdout=subprocess.DEVNULL, check=False)
libusb = appdir / 'usr/bin/_internal/libusb-1.0.so.0'
ctypes.CDLL(str(libusb))
assert usb.backend.libusb1.get_backend(find_library=lambda _: str(libusb)) is not None
fixture = Path(__file__).parent / 'fixtures/payload.rar'
expected = b'PyQuark packaging test payload.\n' * 1024
for mode in ('stage', 'stream'):
    with tempfile.TemporaryDirectory(prefix='pyquark-rar-test-') as cache:
        handle = RarVirtualHandle(str(fixture), 'pyquark-packaging-test.txt',
                                  mode=mode, cache_root=cache)
        try:
            assert handle.read(0, len(expected)) == expected, mode
            assert handle.read(100, 200) == expected[100:300], mode
            assert not handle._error_event.is_set(), mode
        finally:
            handle.close()
print('PASS: bundled UnRAR, libusb, RAR staging, streaming and random reads')
