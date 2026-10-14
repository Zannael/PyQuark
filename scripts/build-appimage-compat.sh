#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${PYQUARK_BUILD_IMAGE:-pyquark-appimage:ubuntu22.04}"
command -v docker >/dev/null || { echo 'Docker is required' >&2; exit 1; }
docker build -t "$IMAGE" "$ROOT_DIR/packaging/linux"
docker run --rm --user "$(id -u):$(id -g)" \
  -e PYQUARK_VERSION="${PYQUARK_VERSION:-}" \
  -e PYINSTALLER_CONFIG_DIR=/tmp/pyquark-pyinstaller \
  -v "$ROOT_DIR:/workspace" -w /workspace "$IMAGE" \
  bash scripts/build-appimage.sh --python /opt/pyquark-build/bin/python
