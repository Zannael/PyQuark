#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ $# -ge 2 && "$1" == "--python" ]]; then
  PYTHON_BIN="$2"
  shift 2
fi
if [[ $# -ne 0 || ! -x "$PYTHON_BIN" ]]; then
  echo "Usage: $0 [--python /path/to/python]" >&2
  exit 1
fi
for tool in curl sha256sum make g++ zsyncmake; do
  command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done

BUILD_DIR="${ROOT_DIR}/build"
APPDIR="${BUILD_DIR}/AppDir"
DIST_DIR="${ROOT_DIR}/dist"
VERSION="${PYQUARK_VERSION:-$(git -C "$ROOT_DIR" describe --tags --always --dirty)}"
VERSION="${VERSION#v}"
[[ "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid version" >&2; exit 1; }
OUTPUT_IMAGE="${DIST_DIR}/PyQuark-${VERSION}-x86_64.AppImage"
UPDATE_INFO="gh-releases-zsync|Zannael|PyQuark|latest|PyQuark-*-x86_64.AppImage.zsync"

# Upstream continuous assets are checksum-pinned. When updating tools, update
# the matching checksum deliberately; never silently accept a changed binary.
APPIMAGETOOL_SHA=95cbe7cce9717fce90c484e34052ee7c7f1d7635b33c12525b4776826a7d29b6
RUNTIME_SHA=156f4bdbde9c52d01814600013e0a273f0118dc2de98975f3c8c63427ec79074
UNRAR_SHA=634900842a3737d9cc15bbcc71d4c74cc713437e0bca296a573424fe5f2660ab
mkdir -p "$BUILD_DIR/downloads" "$DIST_DIR"
download() {
  local url="$1" destination="$2" checksum="$3"
  if ! printf '%s  %s\n' "$checksum" "$destination" | sha256sum -c --status 2>/dev/null; then
    curl --fail --location --retry 3 "$url" -o "$destination"
  fi
  printf '%s  %s\n' "$checksum" "$destination" | sha256sum -c -
}
APPIMAGETOOL_BIN="$BUILD_DIR/downloads/appimagetool-${APPIMAGETOOL_SHA}.AppImage"
RUNTIME_BIN="$BUILD_DIR/downloads/runtime-${RUNTIME_SHA}"
UNRAR_SOURCE="$BUILD_DIR/downloads/unrarsrc-7.3.1.tar.gz"
download https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage "$APPIMAGETOOL_BIN" "$APPIMAGETOOL_SHA"
download https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64 "$RUNTIME_BIN" "$RUNTIME_SHA"
download https://www.rarlab.com/rar/unrarsrc-7.3.1.tar.gz "$UNRAR_SOURCE" "$UNRAR_SHA"
chmod +x "$APPIMAGETOOL_BIN" "$RUNTIME_BIN"

"$PYTHON_BIN" -m PyInstaller --noconfirm --clean --distpath "$DIST_DIR" \
  --workpath "$BUILD_DIR/pyinstaller" "$ROOT_DIR/packaging/pyinstaller/pyquark_gui.spec"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$APPDIR/usr/share/licenses/UnRAR"
cp -a "$DIST_DIR/pyquark-gui/." "$APPDIR/usr/bin/"
mkdir -p "$BUILD_DIR/unrar-source"
tar -xzf "$UNRAR_SOURCE" -C "$BUILD_DIR/unrar-source"
make -C "$BUILD_DIR/unrar-source/unrar" -j2 LDFLAGS='-pthread -static-libstdc++ -static-libgcc'
install -m755 "$BUILD_DIR/unrar-source/unrar/unrar" "$APPDIR/usr/bin/unrar"
cp "$BUILD_DIR/unrar-source/unrar/license.txt" "$APPDIR/usr/share/licenses/UnRAR/license.txt"
cp "$ROOT_DIR/packaging/linux/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$ROOT_DIR/packaging/linux/pyquark.desktop" "$APPDIR/usr/share/applications/pyquark.desktop"
cp "$ROOT_DIR/packaging/linux/pyquark.desktop" "$APPDIR/pyquark.desktop"
cp "$ROOT_DIR/packaging/linux/pyquark.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/pyquark.svg"
cp "$ROOT_DIR/packaging/linux/pyquark.svg" "$APPDIR/pyquark.svg"
ln -sf usr/share/icons/hicolor/scalable/apps/pyquark.svg "$APPDIR/.DirIcon"
(cd "$DIST_DIR" && ARCH=x86_64 "$APPIMAGETOOL_BIN" --appimage-extract-and-run \
  --runtime-file "$RUNTIME_BIN" -u "$UPDATE_INFO" "$APPDIR" "$OUTPUT_IMAGE")
test -s "$OUTPUT_IMAGE.zsync"
(cd "$DIST_DIR" && sha256sum "$(basename "$OUTPUT_IMAGE")" "$(basename "$OUTPUT_IMAGE").zsync" > SHA256SUMS)
echo "Built AppImage and update file: $OUTPUT_IMAGE{,.zsync}"
