#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ARTIFACTS_DIR="${REPO_ROOT}/build/browser-wallet"
VERSION="$(node -p "require('./package.json').version")"
mkdir -p "${ARTIFACTS_DIR}"

echo "[1/5] Building Chrome extension..."
npm run build:chrome
rm -rf dist-chrome
mv dist dist-chrome

echo "[2/5] Building Firefox extension..."
npm run build:firefox
rm -rf dist-firefox
mv dist dist-firefox

echo "[3/5] Packaging Firefox XPI..."
./package-firefox.sh

echo "[4/5] Packaging Chrome and Firefox ZIP downloads..."
rm -f \
  "${ARTIFACTS_DIR}/chipcoin-browser-wallet-chrome.zip" \
  "${ARTIFACTS_DIR}/chipcoin-browser-wallet-chrome-${VERSION}.zip" \
  "${ARTIFACTS_DIR}/chipcoin-browser-wallet-firefox.zip" \
  "${ARTIFACTS_DIR}/chipcoin-browser-wallet-firefox-${VERSION}.zip"
(
  cd dist-chrome
  zip -qr "${ARTIFACTS_DIR}/chipcoin-browser-wallet-chrome-${VERSION}.zip" .
)
(
  cd dist-firefox
  zip -qr "${ARTIFACTS_DIR}/chipcoin-browser-wallet-firefox-${VERSION}.zip" .
)
cp "${ARTIFACTS_DIR}/chipcoin-browser-wallet-chrome-${VERSION}.zip" \
  "${ARTIFACTS_DIR}/chipcoin-browser-wallet-chrome.zip"
cp "${ARTIFACTS_DIR}/chipcoin-browser-wallet-firefox-${VERSION}.zip" \
  "${ARTIFACTS_DIR}/chipcoin-browser-wallet-firefox.zip"

echo "[5/5] Build outputs ready:"
echo "  Chrome : ${SCRIPT_DIR}/dist-chrome"
echo "  Firefox: ${SCRIPT_DIR}/dist-firefox"
echo "  Firefox unsigned XPI: ${SCRIPT_DIR}/../../build/browser-wallet/chipcoin-browser-wallet-firefox-unsigned.xpi"
echo "  Chrome ZIP: ${ARTIFACTS_DIR}/chipcoin-browser-wallet-chrome.zip"
echo "  Firefox ZIP: ${ARTIFACTS_DIR}/chipcoin-browser-wallet-firefox.zip"

echo "[5/5] Done."
