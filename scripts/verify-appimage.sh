#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="$(readlink -f "${1:?Usage: verify-appimage.sh /path/to/AppImage}")"
PYTHON_BIN="${PYQUARK_TEST_PYTHON:-/opt/pyquark-build/bin/python}"
SMOKE_DIR="$ROOT_DIR/build/appimage-smoke"
mkdir -p "$SMOKE_DIR"
cd "$SMOKE_DIR"
"$IMAGE" --appimage-extract >/dev/null
APPDIR="$SMOKE_DIR/squashfs-root"
EXPECTED='gh-releases-zsync|Zannael|PyQuark|latest|PyQuark-*-x86_64.AppImage.zsync'
[[ "$("$IMAGE" --appimage-updateinformation)" == "$EXPECTED" ]]
test -s "$IMAGE.zsync"
file "$IMAGE" | tee runtime.txt
grep -q 'static' runtime.txt
PYTHONPATH="$ROOT_DIR" timeout 30 "$PYTHON_BIN" "$ROOT_DIR/packaging/tests/check_payload.py" "$APPDIR"
# Exercise the real frozen executable through its AppImage entry point.
# This container has no system UnRAR and no libfuse2.
xvfb-run -a -s '-screen 0 1280x900x24' bash -c '
  set -euo pipefail
  APPIMAGE_EXTRACT_AND_RUN=1 "$1" >gui.log 2>&1 &
  gui_pid=$!
  trap "kill $gui_pid 2>/dev/null || true; wait $gui_pid 2>/dev/null || true" EXIT
  sleep 5
  kill -0 "$gui_pid"
  xwininfo -root -tree > windows.txt
  grep -q PyQuark windows.txt
  import -window root screenshot.png
' bash "$IMAGE"
if grep -Ei 'Traceback|not found|could not load|error while loading' gui.log; then
  exit 1
fi
echo "PASS: update metadata, static runtime, dependencies and frozen GUI"
echo "Screenshot: $SMOKE_DIR/screenshot.png"
