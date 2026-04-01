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
show_cmd "docker compose down -v"
docker compose down -v >/dev/null 2>&1 || true
show_cmd "docker compose up --build -d"
docker compose up --build -d >/dev/null
pass setup_devnet

step wait_node_a
if node_a_status=$(wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status" \
  'data["network"] == "devnet" and data["network_magic_hex"] == "fac3b6da"' \
  60 1); then
  info "status node-a $(printf '%s' "$node_a_status" | json_extract '{"height": data["height"], "handshaken_peers": data["handshaken_peer_count"]}')"
  pass wait_node_a
else
  fail wait_node_a
fi

step wrong_network_probe
show_cmd "docker compose run --rm --no-deps -T node-b chipcoin --network mainnet --log-level INFO --data /tmp/wrongnet-mainnet.sqlite3 run --listen-host 0.0.0.0 --listen-port 19445 --peer node-a:18444 --run-seconds 6"
docker compose run --rm --no-deps -T node-b chipcoin --network mainnet --log-level INFO --data /tmp/wrongnet-mainnet.sqlite3 run --listen-host 0.0.0.0 --listen-port 19445 --peer node-a:18444 --run-seconds 6 >/dev/null
pass wrong_network_probe

step peer_summary
if peer_summary_json=$(wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 peer-summary" \
  'data["error_class_counts"].get("wrong_network_magic", 0) >= 1' \
  30 1); then
  info "peer-summary $(printf '%s' "$peer_summary_json" | json_extract '{"peer_count": data["peer_count"], "error_class_counts": data["error_class_counts"], "backoff_peer_count": data["backoff_peer_count"]}')"
  pass peer_summary
else
  show_cmd "docker compose logs --tail=80 node-a"
  docker compose logs --tail=80 node-a || true
  fail peer_summary
fi
