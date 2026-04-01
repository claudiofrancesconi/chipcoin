#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker/compose/docker-compose.devnet-test.yml"

docker() {
  if [[ "${1:-}" == "compose" ]]; then
    shift
    command docker compose -f "$COMPOSE_FILE" "$@"
  else
    command docker "$@"
  fi
}

MINER_ADDRESS="CHCCXakSkRDA63xWXKo2wEmrKvr9JeDzdoic3"
REORG_TEMP_LOG="/tmp/chipcoin-devnet-reorg-mine.log"

pass() {
  printf 'PASS %s\n' "$1"
}

fail() {
  printf 'FAIL %s\n' "$1"
  exit 1
}

step() {
  printf 'RUN  %s\n' "$1"
}

info() {
  printf 'INFO %s\n' "$1"
}

info_err() {
  printf 'INFO %s\n' "$1" >&2
}

show_cmd() {
  printf 'CMD  %s\n' "$1" >&2
}

json_eval() {
  local expr="$1"
  EXPR="$expr" python3 -c 'import json, os, sys; data=json.load(sys.stdin); sys.exit(0 if eval(os.environ["EXPR"], {}, {"data": data}) else 1)'
}

json_extract() {
  local expr="$1"
  EXPR="$expr" python3 -c 'import json, os, sys; data=json.load(sys.stdin); value=eval(os.environ["EXPR"], {}, {"data": data}); print(json.dumps(value) if isinstance(value, (dict, list)) else value)'
}

run_json() {
  local cmd="$1"
  show_cmd "$cmd"
  bash -lc "$cmd"
}

wait_status_match() {
  local attempts="${1:-120}"
  local delay="${2:-1}"
  local i
  local node_a_status=""
  local node_b_status=""
  local node_a_height=""
  local node_b_height=""
  local node_a_tip=""
  local node_b_tip=""
  for ((i=0; i<attempts; i+=1)); do
    node_a_status=$(run_json "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status" 2>/dev/null) || true
    node_b_status=$(run_json "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 status" 2>/dev/null) || true
    if [[ -n "$node_a_status" && -n "$node_b_status" ]]; then
      node_a_height=$(printf '%s' "$node_a_status" | json_extract 'data["height"]')
      node_b_height=$(printf '%s' "$node_b_status" | json_extract 'data["height"]')
      node_a_tip=$(printf '%s' "$node_a_status" | json_extract 'data["tip_hash"]')
      node_b_tip=$(printf '%s' "$node_b_status" | json_extract 'data["tip_hash"]')
      if (( i % 5 == 0 )); then
        info_err "sync progress node-a height=$node_a_height tip=$node_a_tip"
        info_err "sync progress node-b height=$node_b_height tip=$node_b_tip"
      fi
      if printf '%s' "$node_a_status" | json_eval 'data["height"] >= 12 and data["handshaken_peer_count"] >= 2' && \
         printf '%s' "$node_b_status" | json_eval 'data["height"] >= 12 and data["handshaken_peer_count"] >= 1' && \
         [[ "$node_a_tip" == "$node_b_tip" ]]; then
        printf '%s\n%s' "$node_a_status" "$node_b_status"
        return 0
      fi
    fi
    sleep "$delay"
  done
  return 1
}

wait_tip_convergence() {
  local attempts="${1:-120}"
  local delay="${2:-1}"
  local i
  local node_a_status=""
  local node_b_status=""
  local node_a_height=""
  local node_b_height=""
  local node_a_tip=""
  local node_b_tip=""
  for ((i=0; i<attempts; i+=1)); do
    node_a_status=$(run_json "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status" 2>/dev/null) || true
    node_b_status=$(run_json "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 status" 2>/dev/null) || true
    if [[ -n "$node_a_status" && -n "$node_b_status" ]]; then
      node_a_height=$(printf '%s' "$node_a_status" | json_extract 'data["height"]')
      node_b_height=$(printf '%s' "$node_b_status" | json_extract 'data["height"]')
      node_a_tip=$(printf '%s' "$node_a_status" | json_extract 'data["tip_hash"]')
      node_b_tip=$(printf '%s' "$node_b_status" | json_extract 'data["tip_hash"]')
      if (( i % 5 == 0 )); then
        info_err "reorg progress node-a height=$node_a_height tip=$node_a_tip"
        info_err "reorg progress node-b height=$node_b_height tip=$node_b_tip"
      fi
      if [[ "$node_a_tip" == "$node_b_tip" ]]; then
        printf '%s\n%s' "$node_a_status" "$node_b_status"
        return 0
      fi
    fi
    sleep "$delay"
  done
  return 1
}

wait_json() {
  local cmd="$1"
  local expr="$2"
  local attempts="${3:-60}"
  local delay="${4:-1}"
  local output=""
  local i
  for ((i=0; i<attempts; i+=1)); do
    if output=$(run_json "$cmd" 2>/dev/null); then
      if printf '%s' "$output" | json_eval "$expr"; then
        printf '%s' "$output"
        return 0
      fi
    fi
    sleep "$delay"
  done
  return 1
}

step setup_devnet
rm -f "$REORG_TEMP_LOG"
show_cmd "docker compose down -v"
docker compose down -v >/dev/null 2>&1 || true
show_cmd "docker compose up --build -d"
docker compose up --build -d >/dev/null
pass setup_devnet

step wait_sync
if sync_statuses=$(wait_status_match 120 1); then
  node_a_status=$(printf '%s' "$sync_statuses" | sed -n '1p')
  node_b_status=$(printf '%s' "$sync_statuses" | sed -n '2p')
  info "status node-a $(printf '%s' "$node_a_status" | json_extract '{"height": data["height"], "tip_hash": data["tip_hash"]}')"
  info "status node-b $(printf '%s' "$node_b_status" | json_extract '{"height": data["height"], "tip_hash": data["tip_hash"]}')"
  pass wait_sync
else
  fail wait_sync
fi

step isolate_node_b
show_cmd "docker compose stop node-b"
docker compose stop node-b >/dev/null
pass isolate_node_b

step mine_isolated_branch
show_cmd "docker compose run --rm --no-deps -T node-b chipcoin --network devnet --log-level INFO --data /data/node-b-devnet.sqlite3 mine --listen-host 0.0.0.0 --listen-port 18445 --miner-address $MINER_ADDRESS --run-seconds 3 --mining-min-interval-seconds 1.0"
docker compose run --rm --no-deps -T node-b chipcoin --network devnet --log-level INFO --data /data/node-b-devnet.sqlite3 mine --listen-host 0.0.0.0 --listen-port 18445 --miner-address "$MINER_ADDRESS" --run-seconds 3 --mining-min-interval-seconds 1.0 | tee "$REORG_TEMP_LOG"
show_cmd "sleep 8"
sleep 8
pass mine_isolated_branch

step detect_divergence
isolated_node_b_status=$(run_json "docker compose run --rm --no-deps -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 status")
node_a_diverged_status=$(run_json "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status")
info "isolated node-b $(printf '%s' "$isolated_node_b_status" | json_extract '{"height": data["height"], "tip_hash": data["tip_hash"]}')"
info "node-a live $(printf '%s' "$node_a_diverged_status" | json_extract '{"height": data["height"], "tip_hash": data["tip_hash"]}')"
if printf '%s' "$isolated_node_b_status" | json_eval 'data["tip_hash"] != "'"$(printf '%s' "$node_a_diverged_status" | json_extract 'data["tip_hash"]')"'"'; then
  pass detect_divergence
else
  fail detect_divergence
fi

step reconnect_node_b
show_cmd "docker compose up -d node-b"
docker compose up -d node-b >/dev/null
pass reconnect_node_b

step wait_reorg
if converged_statuses=$(wait_tip_convergence 120 1); then
  final_node_a_status=$(printf '%s' "$converged_statuses" | sed -n '1p')
  final_node_b_status=$(printf '%s' "$converged_statuses" | sed -n '2p')
  info "final node-a $(printf '%s' "$final_node_a_status" | json_extract '{"height": data["height"], "tip_hash": data["tip_hash"]}')"
  info "final node-b $(printf '%s' "$final_node_b_status" | json_extract '{"height": data["height"], "tip_hash": data["tip_hash"]}')"
  pass wait_reorg
else
  fail wait_reorg
fi

step reorg_logs
show_cmd "docker compose logs --tail=200 node-b"
reorg_logs=$(docker compose logs --tail=200 node-b || true)
mine_reorg_logs=""
if [[ -f "$REORG_TEMP_LOG" ]]; then
  mine_reorg_logs=$(cat "$REORG_TEMP_LOG")
fi
if printf '%s\n%s' "$mine_reorg_logs" "$reorg_logs" | grep -Eq "reorg start|reorg applied|mempool reconciled after reorg"; then
  info "reorg log markers found"
  pass reorg_logs
else
  if [[ -n "$mine_reorg_logs" ]]; then
    printf '%s\n' "$mine_reorg_logs"
  fi
  printf '%s\n' "$reorg_logs"
  fail reorg_logs
fi
