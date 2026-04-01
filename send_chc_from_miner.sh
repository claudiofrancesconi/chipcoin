#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="${ROOT_DIR}/.venv/bin/chipcoin"
NETWORK="devnet"
DATA_FILE="${ROOT_DIR}/run/node/node-devnet.sqlite3"
WALLET_FILE="${ROOT_DIR}/run/wallets/chipcoin-wallet.json"
PEER_ENDPOINT="tiltmediaconsulting.com:18444"
DEFAULT_FEE_CHIPBITS=1000

if [[ ! -x "${CLI}" ]]; then
  echo "CLI not found at ${CLI}" >&2
  exit 1
fi

if [[ ! -f "${WALLET_FILE}" ]]; then
  echo "Miner wallet file not found at ${WALLET_FILE}" >&2
  exit 1
fi

read -r -p "Recipient CHC address: " RECIPIENT
read -r -p "Amount in CHC: " AMOUNT_CHC

if [[ -z "${RECIPIENT}" ]]; then
  echo "Recipient address is required." >&2
  exit 1
fi

if [[ -z "${AMOUNT_CHC}" ]]; then
  echo "Amount is required." >&2
  exit 1
fi

AMOUNT_CHIPBITS="$(python3 - <<'PY' "${AMOUNT_CHC}"
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import sys

value = sys.argv[1].strip()
try:
    amount = Decimal(value)
except InvalidOperation:
    raise SystemExit("Invalid CHC amount.")

if amount <= 0:
    raise SystemExit("Amount must be greater than zero.")

chipbits = (amount * Decimal("100000000")).quantize(Decimal("1"), rounding=ROUND_DOWN)
if chipbits <= 0:
    raise SystemExit("Amount is too small after conversion to chipbits.")

print(int(chipbits))
PY
)"

echo
echo "Sending payment"
echo "  Wallet     : ${WALLET_FILE}"
echo "  Recipient  : ${RECIPIENT}"
echo "  Amount CHC : ${AMOUNT_CHC}"
echo "  Chipbits   : ${AMOUNT_CHIPBITS}"
echo "  Fee        : ${DEFAULT_FEE_CHIPBITS}"
echo "  Peer       : ${PEER_ENDPOINT}"
echo

"${CLI}" \
  --network "${NETWORK}" \
  --data "${DATA_FILE}" \
  wallet-send \
  --wallet-file "${WALLET_FILE}" \
  --to "${RECIPIENT}" \
  --amount "${AMOUNT_CHIPBITS}" \
  --fee "${DEFAULT_FEE_CHIPBITS}" \
  --connect "${PEER_ENDPOINT}"
