#!/usr/bin/env bash
set -euo pipefail

ROLE="${1:?missing role}"
UNSET_SENTINEL="__CHIPCOIN_UNSET__"

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

normalize_manual_peers() {
  DIRECT_PEER_VALUE="${DIRECT_PEER:-}" DIRECT_PEERS_VALUE="${DIRECT_PEERS:-}" python3 - <<'PY'
import os
import re

raw_values = [
    os.environ.get("DIRECT_PEERS_VALUE", ""),
    os.environ.get("DIRECT_PEER_VALUE", ""),
]
pattern = re.compile(r"^[^:\s]+:\d+$")
seen: set[str] = set()
result: list[str] = []

for raw in raw_values:
    for chunk in re.split(r"[\s,]+", raw.strip()):
        if not chunk:
            continue
        if not pattern.match(chunk):
            raise SystemExit(f"Invalid peer format: {chunk}. Expected host:port.")
        if chunk not in seen:
            seen.add(chunk)
            result.append(chunk)

print("\n".join(result))
PY
}

read_optional_env() {
  local name="$1"
  local value="${!name-$UNSET_SENTINEL}"
  if [[ "$value" == "$UNSET_SENTINEL" ]]; then
    return 1
  fi
  printf '%s' "$value"
}

role_discovery_value() {
  local preferred_name="$1"
  local fallback_name="$2"
  local default_name="${3:-}"
  local value=""

  if value="$(read_optional_env "$preferred_name")"; then
    printf '%s' "$value"
    return 0
  fi
  if value="$(read_optional_env "$fallback_name")"; then
    printf '%s' "$value"
    return 0
  fi
  if [[ -n "$default_name" ]] && value="$(read_optional_env "$default_name")"; then
    printf '%s' "$value"
    return 0
  fi
  printf ''
}

is_truthy() {
  local value="${1:-}"
  [[ "$value" == "true" || "$value" == "1" ]]
}

configure_discovery_env_for_role() {
  local role="$1"
  case "$role" in
    node)
      export DIRECT_PEERS
      DIRECT_PEERS="$(role_discovery_value NODE_DIRECT_PEERS DIRECT_PEERS)"
      export DIRECT_PEER
      DIRECT_PEER="$(role_discovery_value NODE_DIRECT_PEER DIRECT_PEER)"
      export BOOTSTRAP_URL
      BOOTSTRAP_URL="$(role_discovery_value NODE_BOOTSTRAP_URL BOOTSTRAP_URL)"
      ;;
    miner)
      export DIRECT_PEERS
      DIRECT_PEERS="$(role_discovery_value MINER_DIRECT_PEERS DIRECT_PEERS MINER_DEFAULT_DIRECT_PEERS)"
      export DIRECT_PEER
      DIRECT_PEER="$(role_discovery_value MINER_DIRECT_PEER DIRECT_PEER)"
      export BOOTSTRAP_URL
      BOOTSTRAP_URL="$(role_discovery_value MINER_BOOTSTRAP_URL BOOTSTRAP_URL)"
      ;;
    *)
      die "Unsupported discovery role: ${role}"
      ;;
  esac
}

resolve_peers() {
  local peers=""
  if peers="$(normalize_manual_peers)"; then
    if [[ -n "$peers" ]]; then
      DISCOVERY_SOURCE="manual"
      RESOLVED_PEERS="$peers"
      return 0
    fi
  else
    return 1
  fi

  if [[ -n "${BOOTSTRAP_URL:-}" ]]; then
    if peers=$(BOOTSTRAP_URL="$BOOTSTRAP_URL" CHIPCOIN_NETWORK="$CHIPCOIN_NETWORK" BOOTSTRAP_PEER_LIMIT="${BOOTSTRAP_PEER_LIMIT:-4}" python3 - <<'PY'
import os

from chipcoin.interfaces.seed_client import SeedClient

base_url = os.environ["BOOTSTRAP_URL"]
network = os.environ["CHIPCOIN_NETWORK"]
peer_limit = max(1, int(os.environ.get("BOOTSTRAP_PEER_LIMIT", "4")))
client = SeedClient(base_url)
peers = client.list_peers(network)
if not peers:
    raise SystemExit(1)
for peer in peers[:peer_limit]:
    print(f"{peer.host}:{peer.port}")
PY
); then
      DISCOVERY_SOURCE="seed"
      RESOLVED_PEERS="$peers"
      return 0
    fi
    warn "Bootstrap discovery from ${BOOTSTRAP_URL} failed or returned no peers. Starting isolated."
  fi

  return 1
}

bootstrap_announce_once() {
  BOOTSTRAP_URL_VALUE="${BOOTSTRAP_URL:-}" \
  CHIPCOIN_NETWORK_VALUE="${CHIPCOIN_NETWORK:-}" \
  NODE_PUBLIC_HOST_VALUE="${NODE_PUBLIC_HOST:-}" \
  NODE_PUBLIC_P2P_PORT_VALUE="${NODE_PUBLIC_P2P_PORT:-}" \
  python3 - <<'PY'
import os
import time
from importlib import metadata

from chipcoin.interfaces.seed_client import SeedClient

client = SeedClient(os.environ["BOOTSTRAP_URL_VALUE"])
version = metadata.version("chipcoin")
client.announce(
    host=os.environ["NODE_PUBLIC_HOST_VALUE"],
    port=int(os.environ["NODE_PUBLIC_P2P_PORT_VALUE"]),
    network=os.environ["CHIPCOIN_NETWORK_VALUE"],
    node_id="",
    version=version,
    last_seen=int(time.time()),
)
PY
}

start_bootstrap_announce_loop() {
  if ! is_truthy "${BOOTSTRAP_ANNOUNCE_ENABLED:-false}"; then
    return 0
  fi
  if [[ -z "${BOOTSTRAP_URL:-}" ]]; then
    warn "BOOTSTRAP_ANNOUNCE_ENABLED is set but BOOTSTRAP_URL is empty. Skipping bootstrap announce."
    return 0
  fi
  if [[ -z "${NODE_PUBLIC_HOST:-}" ]]; then
    warn "BOOTSTRAP_ANNOUNCE_ENABLED is set but NODE_PUBLIC_HOST is empty. Skipping bootstrap announce."
    return 0
  fi
  if [[ -z "${NODE_PUBLIC_P2P_PORT:-}" ]]; then
    warn "BOOTSTRAP_ANNOUNCE_ENABLED is set but NODE_PUBLIC_P2P_PORT is empty. Skipping bootstrap announce."
    return 0
  fi

  local refresh_interval="${BOOTSTRAP_REFRESH_INTERVAL_SECONDS:-60}"
  if ! [[ "$refresh_interval" =~ ^[0-9]+$ ]] || [[ "$refresh_interval" -le 0 ]]; then
    warn "Invalid BOOTSTRAP_REFRESH_INTERVAL_SECONDS=${refresh_interval}. Falling back to 60 seconds."
    refresh_interval=60
  fi

  (
    while true; do
      if bootstrap_announce_once; then
        log "Bootstrap announce succeeded bootstrap_url=${BOOTSTRAP_URL} public_host=${NODE_PUBLIC_HOST} public_port=${NODE_PUBLIC_P2P_PORT}"
      else
        warn "Bootstrap announce failed bootstrap_url=${BOOTSTRAP_URL} public_host=${NODE_PUBLIC_HOST} public_port=${NODE_PUBLIC_P2P_PORT}"
      fi
      sleep "$refresh_interval"
    done
  ) &
}

ensure_sqlite_file() {
  local path="$1"
  local label="$2"
  mkdir -p "$(dirname "$path")"
  if [[ -e "$path" && -d "$path" ]]; then
    die "${label} path ${path} is a directory. Expected a writable SQLite file path."
  fi
  if [[ ! -e "$path" ]]; then
    touch "$path" || die "Could not create ${label} file at ${path}."
  fi
  if [[ ! -f "$path" ]]; then
    die "${label} path ${path} is not a regular file."
  fi
  if [[ ! -w "$path" ]]; then
    die "${label} file ${path} is not writable by the current user."
  fi
}

sqlite_file_is_pristine() {
  local path="$1"
  [[ ! -s "$path" ]]
}

apply_initial_sync_defaults_if_needed() {
  local sqlite_path="$1"
  local role_label="$2"
  local startup_peer_count="$3"

  local enabled="${INITIAL_SYNC_CONSERVATIVE_DEFAULTS:-true}"
  if [[ "$enabled" != "true" && "$enabled" != "1" ]]; then
    return 0
  fi
  if [[ "$startup_peer_count" -le 0 ]]; then
    return 0
  fi
  if ! sqlite_file_is_pristine "$sqlite_path"; then
    return 0
  fi

  if [[ -z "${BLOCK_MAX_INFLIGHT_PER_PEER+x}" || "${BLOCK_MAX_INFLIGHT_PER_PEER}" == "16" ]]; then
    BLOCK_MAX_INFLIGHT_PER_PEER=4
  fi
  if [[ -z "${BLOCK_REQUEST_TIMEOUT_SECONDS+x}" || "${BLOCK_REQUEST_TIMEOUT_SECONDS}" == "15" ]]; then
    BLOCK_REQUEST_TIMEOUT_SECONDS=60
  fi
  if [[ -z "${HEADERS_SYNC_PARALLEL_PEERS+x}" || "${HEADERS_SYNC_PARALLEL_PEERS}" == "2" ]]; then
    HEADERS_SYNC_PARALLEL_PEERS=1
  fi
  if [[ -z "${BLOCK_DOWNLOAD_WINDOW_SIZE+x}" || "${BLOCK_DOWNLOAD_WINDOW_SIZE}" == "128" ]]; then
    BLOCK_DOWNLOAD_WINDOW_SIZE=32
  fi
  if [[ -z "${P2P_READ_TIMEOUT_SECONDS+x}" || "${P2P_READ_TIMEOUT_SECONDS}" == "15" || "${P2P_READ_TIMEOUT_SECONDS}" == "15.0" ]]; then
    P2P_READ_TIMEOUT_SECONDS=60
  fi

  log "Applying conservative initial sync defaults role=${role_label} startup_peers=${startup_peer_count} block_max_inflight_per_peer=${BLOCK_MAX_INFLIGHT_PER_PEER} block_request_timeout_seconds=${BLOCK_REQUEST_TIMEOUT_SECONDS} headers_sync_parallel_peers=${HEADERS_SYNC_PARALLEL_PEERS} block_download_window_size=${BLOCK_DOWNLOAD_WINDOW_SIZE} p2p_read_timeout_seconds=${P2P_READ_TIMEOUT_SECONDS}"
}

run_node() {
  : "${CHIPCOIN_NETWORK:?missing CHIPCOIN_NETWORK}"
  : "${NODE_LOG_LEVEL:?missing NODE_LOG_LEVEL}"
  : "${NODE_P2P_BIND_PORT:?missing NODE_P2P_BIND_PORT}"
  : "${NODE_HTTP_BIND_PORT:?missing NODE_HTTP_BIND_PORT}"

  ensure_sqlite_file /runtime/node.sqlite3 "Node SQLite"
  configure_discovery_env_for_role node
  log "Starting node network=${CHIPCOIN_NETWORK} p2p_port=${NODE_P2P_BIND_PORT} http_port=${NODE_HTTP_BIND_PORT} node_wallet_runtime=not_used_in_phase_1"

  local -a peer_args=()
  local startup_peer_count=0
  local peers=""
  RESOLVED_PEERS=""
  if resolve_peers; then
    peers="$RESOLVED_PEERS"
    while IFS= read -r peer; do
      [[ -n "$peer" ]] || continue
      peer_args+=(--peer "$peer")
      startup_peer_count=$((startup_peer_count + 1))
    done <<< "$peers"
    peer_args+=(--peer-source "${DISCOVERY_SOURCE:-manual}")
    if [[ "${DISCOVERY_SOURCE:-manual}" == "seed" ]]; then
      log "Node discovery target=bootstrap-seed:${startup_peer_count}_peer(s) bootstrap_url=${BOOTSTRAP_URL}"
    else
      log "Node discovery target=manual:${startup_peer_count}_peer(s)"
    fi
  else
    log "Node discovery target=isolated"
    if [[ "${PEER_DISCOVERY_ENABLED:-true}" != "true" && "${PEER_DISCOVERY_ENABLED:-true}" != "1" ]]; then
      warn "Peer discovery is disabled and no startup peer was found. Node will remain isolated until you add peers manually."
    else
      warn "No startup peer was found. Node will rely on the persisted peerbook or inbound peers."
    fi
  fi

  apply_initial_sync_defaults_if_needed /runtime/node.sqlite3 "node" "$startup_peer_count"

  if awk 'BEGIN { exit !('"${BLOCK_REQUEST_TIMEOUT_SECONDS:-15}"' < 5) }'; then
    warn "BLOCK_REQUEST_TIMEOUT_SECONDS=${BLOCK_REQUEST_TIMEOUT_SECONDS:-15} is unusually low and may cause unnecessary block reassignment churn."
  fi
  if awk 'BEGIN { exit !('"${BLOCK_DOWNLOAD_WINDOW_SIZE:-128}"' < '"${BLOCK_MAX_INFLIGHT_PER_PEER:-16}"') }'; then
    warn "BLOCK_DOWNLOAD_WINDOW_SIZE=${BLOCK_DOWNLOAD_WINDOW_SIZE:-128} is below BLOCK_MAX_INFLIGHT_PER_PEER=${BLOCK_MAX_INFLIGHT_PER_PEER:-16}; effective throughput will be reduced."
  fi

  start_bootstrap_announce_loop

  exec chipcoin \
    --network "${CHIPCOIN_NETWORK}" \
    --log-level "${NODE_LOG_LEVEL}" \
    --data /runtime/node.sqlite3 \
    run \
    --listen-host 0.0.0.0 \
    --listen-port "${NODE_P2P_BIND_PORT}" \
    --http-host 0.0.0.0 \
    --http-port "${NODE_HTTP_BIND_PORT}" \
    --ping-interval-seconds "${PING_INTERVAL_SECONDS:-2.0}" \
    --read-timeout-seconds "${P2P_READ_TIMEOUT_SECONDS:-15.0}" \
    --write-timeout-seconds "${P2P_WRITE_TIMEOUT_SECONDS:-15.0}" \
    --handshake-timeout-seconds "${P2P_HANDSHAKE_TIMEOUT_SECONDS:-5.0}" \
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
  : "${MINING_MIN_INTERVAL_SECONDS:?missing MINING_MIN_INTERVAL_SECONDS}"
  : "${MINING_NODE_URLS:?missing MINING_NODE_URLS}"

  [[ -f /runtime/miner-wallet.json ]] || die "Miner wallet file is missing at /runtime/miner-wallet.json."

  local miner_address
  miner_address="$(wallet_address /runtime/miner-wallet.json)"
  log "Starting miner network=${CHIPCOIN_NETWORK} node_urls=${MINING_NODE_URLS} wallet_address=${miner_address}"

  local -a node_args=()
  local -a miner_id_args=()
  local node_url
  IFS=',' read -ra configured_node_urls <<< "${MINING_NODE_URLS}"
  for node_url in "${configured_node_urls[@]}"; do
    [[ -n "${node_url}" ]] || continue
    node_args+=(--node-url "${node_url}")
  done
  [[ "${#node_args[@]}" -gt 0 ]] || die "MINING_NODE_URLS must contain at least one HTTP endpoint."
  if [[ -n "${MINING_MINER_ID:-}" ]]; then
    miner_id_args=(--miner-id "${MINING_MINER_ID}")
  fi

  exec chipcoin \
    --network "${CHIPCOIN_NETWORK}" \
    --log-level "${MINER_LOG_LEVEL}" \
    mine \
    --miner-address "${miner_address}" \
    "${node_args[@]}" \
    --polling-interval-seconds "${MINING_POLLING_INTERVAL_SECONDS:-2.0}" \
    --request-timeout-seconds "${MINING_REQUEST_TIMEOUT_SECONDS:-10.0}" \
    --nonce-batch-size "${MINING_NONCE_BATCH_SIZE:-250000}" \
    --template-refresh-skew-seconds "${MINING_TEMPLATE_REFRESH_SKEW_SECONDS:-1}" \
    --mining-min-interval-seconds "${MINING_MIN_INTERVAL_SECONDS}" \
    "${miner_id_args[@]}"
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
