#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "[1/4] Building Chrome extension..."
npm run build:chrome
rm -rf dist-chrome
mv dist dist-chrome

echo "[2/4] Building Firefox extension..."
npm run build:firefox
rm -rf dist-firefox
mv dist dist-firefox

echo "[3/4] Build outputs ready:"
echo "  Chrome : ${SCRIPT_DIR}/dist-chrome"
echo "  Firefox: ${SCRIPT_DIR}/dist-firefox"

echo "[4/4] Done."
