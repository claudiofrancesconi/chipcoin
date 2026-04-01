#!/usr/bin/env bash
set -euo pipefail

ROLE="${1:?missing role}"

log() {
  printf 'INFO %s\n' "$*"
}

warn() {
  printf 'WARN %s\n' "$*" >&2
}

die() {
  printf 'ERROR %s\n' "$*" >&2
  exit 1
}

wallet_address() {
  local wallet_file="$1"
  WALLET_FILE="$wallet_file" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["WALLET_FILE"])
payload = json.loads(path.read_text(encoding="utf-8"))
address = payload.get("address")
if not isinstance(address, str) or not address:
    raise SystemExit("Wallet file does not contain a valid address.")
print(address)
PY
}

resolve_peer() {
  if [[ -n "${DIRECT_PEER:-}" ]]; then
    printf '%s' "$DIRECT_PEER"
    return 0
  fi

  if [[ -n "${BOOTSTRAP_URL:-}" ]]; then
    local peer=""
    if peer=$(BOOTSTRAP_URL="$BOOTSTRAP_URL" CHIPCOIN_NETWORK="$CHIPCOIN_NETWORK" python3 - <<'PY'
import os

from chipcoin.interfaces.seed_client import SeedClient

base_url = os.environ["BOOTSTRAP_URL"]
network = os.environ["CHIPCOIN_NETWORK"]
client = SeedClient(base_url)
peers = client.list_peers(network)
if not peers:
    raise SystemExit(1)
print(f"{peers[0].host}:{peers[0].port}")
PY
); then
      printf '%s' "$peer"
      return 0
    fi
    warn "Bootstrap discovery failed or returned no peers. Starting isolated."
  fi

  return 1
}

ensure_file_parent() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
}

start_http_api() {
  chipcoin-http \
    --data /runtime/node.sqlite3 \
    --network "${CHIPCOIN_NETWORK}" \
    --log-level "${NODE_LOG_LEVEL}" \
    --host 0.0.0.0 \
    --port "${NODE_HTTP_BIND_PORT}"
}

run_node() {
  : "${CHIPCOIN_NETWORK:?missing CHIPCOIN_NETWORK}"
  : "${NODE_LOG_LEVEL:?missing NODE_LOG_LEVEL}"
  : "${NODE_P2P_BIND_PORT:?missing NODE_P2P_BIND_PORT}"
  : "${NODE_HTTP_BIND_PORT:?missing NODE_HTTP_BIND_PORT}"

  ensure_file_parent /runtime/node.sqlite3
  touch /runtime/node.sqlite3
  log "Starting node network=${CHIPCOIN_NETWORK} p2p_port=${NODE_P2P_BIND_PORT} http_port=${NODE_HTTP_BIND_PORT} node_wallet_runtime=not_used_in_phase_1"

  local -a peer_args=()
  if peer="$(resolve_peer)"; then
    log "Node discovery target=${peer}"
    peer_args=(--peer "$peer")
  else
    log "Node discovery target=isolated"
  fi

  start_http_api &
  http_api_pid=$!
  trap 'kill "${http_api_pid}" >/dev/null 2>&1 || true' EXIT

  exec chipcoin \
    --network "${CHIPCOIN_NETWORK}" \
    --log-level "${NODE_LOG_LEVEL}" \
    --data /runtime/node.sqlite3 \
    run \
    --listen-host 0.0.0.0 \
    --listen-port "${NODE_P2P_BIND_PORT}" \
    "${peer_args[@]}"
}

run_miner() {
  : "${CHIPCOIN_NETWORK:?missing CHIPCOIN_NETWORK}"
  : "${MINER_LOG_LEVEL:?missing MINER_LOG_LEVEL}"
  : "${MINER_P2P_BIND_PORT:?missing MINER_P2P_BIND_PORT}"
  : "${MINING_MIN_INTERVAL_SECONDS:?missing MINING_MIN_INTERVAL_SECONDS}"

  [[ -f /runtime/miner-wallet.json ]] || die "Miner wallet file is missing at /runtime/miner-wallet.json."
  ensure_file_parent /runtime/miner.sqlite3
  touch /runtime/miner.sqlite3

  local miner_address
  miner_address="$(wallet_address /runtime/miner-wallet.json)"
  log "Starting miner network=${CHIPCOIN_NETWORK} p2p_port=${MINER_P2P_BIND_PORT} wallet_address=${miner_address}"

  local -a peer_args=()
  if peer="$(resolve_peer)"; then
    log "Miner discovery target=${peer}"
    peer_args=(--peer "$peer")
  else
    log "Miner discovery target=isolated"
  fi

  exec chipcoin \
    --network "${CHIPCOIN_NETWORK}" \
    --log-level "${MINER_LOG_LEVEL}" \
    --data /runtime/miner.sqlite3 \
    mine \
    --listen-host 0.0.0.0 \
    --listen-port "${MINER_P2P_BIND_PORT}" \
    --miner-address "${miner_address}" \
    --mining-min-interval-seconds "${MINING_MIN_INTERVAL_SECONDS}" \
    "${peer_args[@]}"
}

case "$ROLE" in
  node)
    run_node
    ;;
  miner)
    run_miner
    ;;
  *)
    die "Unsupported role: ${ROLE}"
    ;;
esac
