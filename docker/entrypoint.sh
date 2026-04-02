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
    DISCOVERY_SOURCE="manual"
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
      DISCOVERY_SOURCE="seed"
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
    peer_args=(--peer "$peer" --peer-source "${DISCOVERY_SOURCE:-manual}")
  else
    log "Node discovery target=isolated"
    if [[ "${PEER_DISCOVERY_ENABLED:-true}" != "true" && "${PEER_DISCOVERY_ENABLED:-true}" != "1" ]]; then
      warn "Peer discovery is disabled and no startup peer was found. Node will remain isolated until you add peers manually."
    else
      warn "No startup peer was found. Node will rely on the persisted peerbook or inbound peers."
    fi
  fi

  if awk 'BEGIN { exit !('"${BLOCK_REQUEST_TIMEOUT_SECONDS:-15}"' < 5) }'; then
    warn "BLOCK_REQUEST_TIMEOUT_SECONDS=${BLOCK_REQUEST_TIMEOUT_SECONDS:-15} is unusually low and may cause unnecessary block reassignment churn."
  fi
  if awk 'BEGIN { exit !('"${BLOCK_DOWNLOAD_WINDOW_SIZE:-128}"' < '"${BLOCK_MAX_INFLIGHT_PER_PEER:-16}"') }'; then
    warn "BLOCK_DOWNLOAD_WINDOW_SIZE=${BLOCK_DOWNLOAD_WINDOW_SIZE:-128} is below BLOCK_MAX_INFLIGHT_PER_PEER=${BLOCK_MAX_INFLIGHT_PER_PEER:-16}; effective throughput will be reduced."
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
    --peer-discovery-enabled "${PEER_DISCOVERY_ENABLED:-true}" \
    --peerbook-max-size "${PEERBOOK_MAX_SIZE:-1024}" \
    --peer-addr-max-per-message "${PEER_ADDR_MAX_PER_MESSAGE:-250}" \
    --peer-addr-relay-limit-per-interval "${PEER_ADDR_RELAY_LIMIT_PER_INTERVAL:-250}" \
    --peer-addr-relay-interval-seconds "${PEER_ADDR_RELAY_INTERVAL_SECONDS:-30}" \
    --peer-stale-after-seconds "${PEER_STALE_AFTER_SECONDS:-604800}" \
    --peer-retry-backoff-base-seconds "${PEER_RETRY_BACKOFF_BASE_SECONDS:-1}" \
    --peer-retry-backoff-max-seconds "${PEER_RETRY_BACKOFF_MAX_SECONDS:-30}" \
    --peer-discovery-startup-prefer-persisted "${PEER_DISCOVERY_STARTUP_PREFER_PERSISTED:-true}" \
    --headers-sync-enabled "${HEADERS_SYNC_ENABLED:-true}" \
    --headers-max-per-message "${HEADERS_MAX_PER_MESSAGE:-2000}" \
    --block-download-window-size "${BLOCK_DOWNLOAD_WINDOW_SIZE:-128}" \
    --block-max-inflight-per-peer "${BLOCK_MAX_INFLIGHT_PER_PEER:-16}" \
    --block-request-timeout-seconds "${BLOCK_REQUEST_TIMEOUT_SECONDS:-15}" \
    --headers-sync-parallel-peers "${HEADERS_SYNC_PARALLEL_PEERS:-2}" \
    --headers-sync-start-height-gap-threshold "${HEADERS_SYNC_START_HEIGHT_GAP_THRESHOLD:-1}" \
    --misbehavior-warning-threshold "${PEER_MISBEHAVIOR_WARNING_THRESHOLD:-25}" \
    --misbehavior-disconnect-threshold "${PEER_MISBEHAVIOR_DISCONNECT_THRESHOLD:-50}" \
    --misbehavior-ban-threshold "${PEER_MISBEHAVIOR_BAN_THRESHOLD:-100}" \
    --misbehavior-ban-duration-seconds "${PEER_MISBEHAVIOR_BAN_DURATION_SECONDS:-1800}" \
    --misbehavior-decay-interval-seconds "${PEER_MISBEHAVIOR_DECAY_INTERVAL_SECONDS:-300}" \
    --misbehavior-decay-step "${PEER_MISBEHAVIOR_DECAY_STEP:-5}" \
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
    peer_args=(--peer "$peer" --peer-source "${DISCOVERY_SOURCE:-manual}")
  else
    log "Miner discovery target=isolated"
    warn "No startup peer was found. Miner will rely on its peerbook and local node seeding fallback if available."
  fi

  if awk 'BEGIN { exit !('"${BLOCK_REQUEST_TIMEOUT_SECONDS:-15}"' < 5) }'; then
    warn "BLOCK_REQUEST_TIMEOUT_SECONDS=${BLOCK_REQUEST_TIMEOUT_SECONDS:-15} is unusually low and may cause unnecessary block reassignment churn."
  fi
  if awk 'BEGIN { exit !('"${BLOCK_DOWNLOAD_WINDOW_SIZE:-128}"' < '"${BLOCK_MAX_INFLIGHT_PER_PEER:-16}"') }'; then
    warn "BLOCK_DOWNLOAD_WINDOW_SIZE=${BLOCK_DOWNLOAD_WINDOW_SIZE:-128} is below BLOCK_MAX_INFLIGHT_PER_PEER=${BLOCK_MAX_INFLIGHT_PER_PEER:-16}; effective throughput will be reduced."
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
    --peer-seed-url "${MINER_LOCAL_NODE_ENDPOINT:-http://node:8081}" \
    --peer-discovery-enabled "${PEER_DISCOVERY_ENABLED:-true}" \
    --peerbook-max-size "${PEERBOOK_MAX_SIZE:-1024}" \
    --peer-addr-max-per-message "${PEER_ADDR_MAX_PER_MESSAGE:-250}" \
    --peer-addr-relay-limit-per-interval "${PEER_ADDR_RELAY_LIMIT_PER_INTERVAL:-250}" \
    --peer-addr-relay-interval-seconds "${PEER_ADDR_RELAY_INTERVAL_SECONDS:-30}" \
    --peer-stale-after-seconds "${PEER_STALE_AFTER_SECONDS:-604800}" \
    --peer-retry-backoff-base-seconds "${PEER_RETRY_BACKOFF_BASE_SECONDS:-1}" \
    --peer-retry-backoff-max-seconds "${PEER_RETRY_BACKOFF_MAX_SECONDS:-30}" \
    --peer-discovery-startup-prefer-persisted "${PEER_DISCOVERY_STARTUP_PREFER_PERSISTED:-true}" \
    --headers-sync-enabled "${HEADERS_SYNC_ENABLED:-true}" \
    --headers-max-per-message "${HEADERS_MAX_PER_MESSAGE:-2000}" \
    --block-download-window-size "${BLOCK_DOWNLOAD_WINDOW_SIZE:-128}" \
    --block-max-inflight-per-peer "${BLOCK_MAX_INFLIGHT_PER_PEER:-16}" \
    --block-request-timeout-seconds "${BLOCK_REQUEST_TIMEOUT_SECONDS:-15}" \
    --headers-sync-parallel-peers "${HEADERS_SYNC_PARALLEL_PEERS:-2}" \
    --headers-sync-start-height-gap-threshold "${HEADERS_SYNC_START_HEIGHT_GAP_THRESHOLD:-1}" \
    --misbehavior-warning-threshold "${PEER_MISBEHAVIOR_WARNING_THRESHOLD:-25}" \
    --misbehavior-disconnect-threshold "${PEER_MISBEHAVIOR_DISCONNECT_THRESHOLD:-50}" \
    --misbehavior-ban-threshold "${PEER_MISBEHAVIOR_BAN_THRESHOLD:-100}" \
    --misbehavior-ban-duration-seconds "${PEER_MISBEHAVIOR_BAN_DURATION_SECONDS:-1800}" \
    --misbehavior-decay-interval-seconds "${PEER_MISBEHAVIOR_DECAY_INTERVAL_SECONDS:-300}" \
    --misbehavior-decay-step "${PEER_MISBEHAVIOR_DECAY_STEP:-5}" \
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
