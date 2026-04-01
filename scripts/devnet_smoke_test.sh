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
MINER_PRIVATE_KEY_HEX="d52844c5f9f5483513b926dd262fcc2e3fbbaba22fdf5b3327c9e0fe908f53bd"
TX_AMOUNT_CHIPBITS=500000000
TX_FEE_CHIPBITS=1000
NODE_ID="smoke-node-1"
PROPAGATION_ATTEMPTS=60

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

send_raw_tx_p2p() {
  local host="$1"
  local port="$2"
  local raw_tx_hex="$3"
  local network="${4:-devnet}"
  PYTHONPATH="${PYTHONPATH:-src}" HOST="$host" PORT="$port" RAW_TX_HEX="$raw_tx_hex" NETWORK="$network" python3 - <<'PY'
import asyncio
import os
import secrets

from chipcoin.config import get_network_config
from chipcoin.consensus.serialization import deserialize_transaction
from chipcoin.node.messages import MessageEnvelope, TransactionMessage
from chipcoin.node.p2p.protocol import LocalPeerIdentity, PeerProtocol


async def main() -> None:
    network = os.environ["NETWORK"]
    network_config = get_network_config(network)
    transaction, offset = deserialize_transaction(bytes.fromhex(os.environ["RAW_TX_HEX"]))
    if offset != len(bytes.fromhex(os.environ["RAW_TX_HEX"])):
        raise ValueError("Raw transaction contains trailing bytes.")
    protocol = await PeerProtocol.connect(
        os.environ["HOST"],
        int(os.environ["PORT"]),
        identity=LocalPeerIdentity(
            node_id=secrets.token_hex(16),
            network=network,
            start_height=0,
            user_agent="chipcoin-smoke/0.1",
            relay=False,
            network_magic=network_config.magic,
        ),
    )
    try:
        await protocol.send_message(MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction)))
        await asyncio.sleep(0.1)
    finally:
        await protocol.close(reason="Smoke test submission complete.")


asyncio.run(main())
PY
}

show_json_info() {
  local prefix="$1"
  local payload="$2"
  local expr="$3"
  info "$prefix $(printf '%s' "$payload" | json_extract "$expr")"
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

step setup_minimal
show_cmd "docker compose down -v"
docker compose down -v >/dev/null 2>&1 || true
show_cmd "docker compose up --build -d"
docker compose up --build -d >/dev/null

services_running=$(docker compose ps --status running --services | sort | tr '\n' ' ')
if [[ "$services_running" == *"miner"* && "$services_running" == *"node-a"* && "$services_running" == *"node-b"* ]]; then
  pass setup_minimal
else
  fail setup_minimal
fi

step node_a_status
if node_a_status_json=$(wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status" \
  'data["network"] == "devnet" and data["network_magic_hex"] == "fac3b6da" and data["handshaken_peer_count"] >= 2' \
  60 1); then
  show_json_info "status node-a" "$node_a_status_json" '{"height": data["height"], "peers": data["peer_count"], "handshaken_peers": data["handshaken_peer_count"], "mempool_size": data["mempool_size"], "tip_hash": data["tip_hash"]}'
  pass node_a_status
else
  fail node_a_status
fi

step peer_visibility
if node_b_peers_json=$(wait_json \
  "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 list-peers" \
  'len(data) >= 1 and any(peer["handshake_complete"] is True for peer in data)' \
  60 1); then
  show_json_info "peers node-b" "$node_b_peers_json" '[{"host": peer["host"], "port": peer["port"], "handshake_complete": peer["handshake_complete"], "last_error": peer["last_error"]} for peer in data]'
  pass peer_visibility
else
  fail peer_visibility
fi

step mining_progress
if miner_status_json=$(wait_json \
  "docker compose exec -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 status" \
  'isinstance(data["height"], int) and data["height"] >= 12' \
  120 1); then
  show_json_info "status miner" "$miner_status_json" '{"height": data["height"], "peers": data["peer_count"], "handshaken_peers": data["handshaken_peer_count"], "mempool_size": data["mempool_size"], "tip_hash": data["tip_hash"]}'
  pass mining_progress
else
  fail mining_progress
fi

step miner_wallet_import
show_cmd "docker compose exec -T miner chipcoin wallet-import --wallet-file /data/miner-wallet.json --private-key-hex <redacted>"
miner_import_json=$(docker compose exec -T miner chipcoin wallet-import --wallet-file /data/miner-wallet.json --private-key-hex "$MINER_PRIVATE_KEY_HEX") || fail miner_wallet_import
info "wallet miner address=$MINER_ADDRESS"
if printf '%s' "$miner_import_json" | json_eval 'data["address"] == "'"$MINER_ADDRESS"'"'; then
  pass miner_wallet_import
else
  fail miner_wallet_import
fi

step miner_spendable_balance
miner_balance_cmd="docker compose exec -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 wallet-balance --wallet-file /data/miner-wallet.json"
if miner_balance_json=$(wait_json \
  "$miner_balance_cmd" \
  'data["address"] == "'"$MINER_ADDRESS"'" and data["spendable_balance_chipbits"] >= '"$((TX_AMOUNT_CHIPBITS + TX_FEE_CHIPBITS))" \
  120 1); then
  info "balance miner spendable_chipbits=$(printf '%s' "$miner_balance_json" | json_extract 'data["spendable_balance_chipbits"]') immature_chipbits=$(printf '%s' "$miner_balance_json" | json_extract 'data["immature_balance_chipbits"]')"
  pass miner_spendable_balance
else
  fail miner_spendable_balance
fi

step recipient_wallet
show_cmd "docker compose exec -T node-b chipcoin wallet-generate --wallet-file /data/recipient-wallet.json"
recipient_wallet_json=$(docker compose exec -T node-b chipcoin wallet-generate --wallet-file /data/recipient-wallet.json) || fail recipient_wallet
RECIPIENT_ADDRESS=$(printf '%s' "$recipient_wallet_json" | json_extract 'data["address"]')
info "wallet destinatario address=$RECIPIENT_ADDRESS"
if [[ "$RECIPIENT_ADDRESS" == CHC* ]]; then
  pass recipient_wallet
else
  fail recipient_wallet
fi

step node_wallet
show_cmd "docker compose exec -T node-b chipcoin wallet-generate --wallet-file /data/node-wallet.json"
node_wallet_json=$(docker compose exec -T node-b chipcoin wallet-generate --wallet-file /data/node-wallet.json) || fail node_wallet
NODE_PAYOUT_ADDRESS=$(printf '%s' "$node_wallet_json" | json_extract 'data["address"]')
info "wallet nodo payout_address=$NODE_PAYOUT_ADDRESS"
if [[ "$NODE_PAYOUT_ADDRESS" == CHC* ]]; then
  pass node_wallet
else
  fail node_wallet
fi

step tx_submit
show_cmd "docker compose stop miner"
docker compose stop miner >/dev/null

show_cmd "docker compose run --rm --no-deps -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 wallet-build --wallet-file /data/miner-wallet.json --to $RECIPIENT_ADDRESS --amount $TX_AMOUNT_CHIPBITS --fee $TX_FEE_CHIPBITS"
tx_build_json=$(docker compose run --rm --no-deps -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 wallet-build --wallet-file /data/miner-wallet.json --to "$RECIPIENT_ADDRESS" --amount "$TX_AMOUNT_CHIPBITS" --fee "$TX_FEE_CHIPBITS") || fail tx_submit
TXID=$(printf '%s' "$tx_build_json" | json_extract 'data["txid"]')
RAW_TX_HEX=$(printf '%s' "$tx_build_json" | json_extract 'data["raw_hex"]')
show_cmd "python3 <send_raw_tx_p2p> 127.0.0.1 18444 <raw_hex>"
send_raw_tx_p2p "127.0.0.1" "18444" "$RAW_TX_HEX" "devnet" || fail tx_submit
info "tx txid=$TXID fee_chipbits=$(printf '%s' "$tx_build_json" | json_extract 'data["fee_chipbits"]') change_chipbits=$(printf '%s' "$tx_build_json" | json_extract 'data["change_chipbits"]')"
info "tx submit sent=True mode=p2p_raw"
if printf '%s' "$tx_build_json" | json_eval 'len(data["raw_hex"]) > 0 and data["txid"] == "'"$TXID"'"'; then
  pass tx_submit
else
  fail tx_submit
fi

step tx_mempool_node_a
if node_a_mempool_json=$(wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 mempool" \
  'any(entry["txid"] == "'"$TXID"'" for entry in data)' \
  30 1); then
  show_json_info "mempool node-a" "$node_a_mempool_json" '[entry["txid"] for entry in data]'
  pass tx_mempool_node_a
else
  fail tx_mempool_node_a
fi

step tx_propagation
if node_b_mempool_json=$(wait_json \
  "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 mempool" \
  'any(entry["txid"] == "'"$TXID"'" for entry in data)' \
  "$PROPAGATION_ATTEMPTS" 1); then
  show_json_info "mempool node-b" "$node_b_mempool_json" '[entry["txid"] for entry in data]'
  pass tx_propagation
else
  show_cmd "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 mempool"
  docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 mempool || true
  show_cmd "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 mempool"
  docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 mempool || true
  show_cmd "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status"
  docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 status || true
  show_cmd "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 status"
  docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 status || true
  fail tx_propagation
fi

step tx_confirmed
show_cmd "docker compose start miner"
docker compose start miner >/dev/null

if ! wait_json \
  "docker compose exec -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 list-peers" \
  'len(data) >= 1 and any(peer["handshake_complete"] is True for peer in data)' \
  30 1 >/dev/null; then
  fail tx_confirmed
fi

show_cmd "python3 <send_raw_tx_p2p> 127.0.0.1 18446 <raw_hex>"
if ! send_raw_tx_p2p "127.0.0.1" "18446" "$RAW_TX_HEX" "devnet"; then
  fail tx_confirmed
fi

if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 tx $TXID" \
  'data["location"] == "chain"' \
  60 1 >/dev/null && \
  recipient_balance_json=$(wait_json \
  "docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 wallet-balance --wallet-file /data/recipient-wallet.json" \
  'data["confirmed_balance_chipbits"] == '"$TX_AMOUNT_CHIPBITS"' and data["spendable_balance_chipbits"] == '"$TX_AMOUNT_CHIPBITS"'' \
  60 1); then
  info "balance destinatario confirmed_chipbits=$(printf '%s' "$recipient_balance_json" | json_extract 'data["confirmed_balance_chipbits"]') spendable_chipbits=$(printf '%s' "$recipient_balance_json" | json_extract 'data["spendable_balance_chipbits"]')"
  show_cmd "docker compose exec -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 wallet-balance --wallet-file /data/miner-wallet.json"
  miner_post_tx_balance_json=$(docker compose exec -T miner chipcoin --network devnet --data /data/miner-devnet.sqlite3 wallet-balance --wallet-file /data/miner-wallet.json) || fail tx_confirmed
  info "balance miner post-tx confirmed_chipbits=$(printf '%s' "$miner_post_tx_balance_json" | json_extract 'data["confirmed_balance_chipbits"]') spendable_chipbits=$(printf '%s' "$miner_post_tx_balance_json" | json_extract 'data["spendable_balance_chipbits"]')"
  pass tx_confirmed
else
  fail tx_confirmed
fi

step tx_chain_lookup
if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 tx $TXID" \
  'data["location"] == "chain"' \
  60 1 >/dev/null; then
  pass tx_chain_lookup
else
  fail tx_chain_lookup
fi

step register_node
register_json=$(docker compose exec -T node-b chipcoin --network devnet --data /data/node-b-devnet.sqlite3 register-node --wallet-file /data/node-wallet.json --node-id "$NODE_ID" --payout-address "$NODE_PAYOUT_ADDRESS" --connect node-a:18444) || fail register_node
if printf '%s' "$register_json" | json_eval 'data["submitted"] is True and data["node_id"] == "'"$NODE_ID"'"'; then
  pass register_node
else
  fail register_node
fi

step node_registry
if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 node-registry" \
  'any(entry["node_id"] == "'"$NODE_ID"'" for entry in data)' \
  60 1 >/dev/null; then
  pass node_registry
else
  fail node_registry
fi

step next_winners
if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 next-winners" \
  'data["active_nodes_count"] >= 1 and any(entry["node_id"] == "'"$NODE_ID"'" for entry in data["selected_winners"])' \
  60 1 >/dev/null; then
  pass next_winners
else
  fail next_winners
fi

step node_reward_summary
if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 reward-summary --address $NODE_PAYOUT_ADDRESS" \
  'data["total_node_rewards_chipbits"] > 0 and data["payout_count"] > 0' \
  120 1 >/dev/null; then
  pass node_reward_summary
else
  fail node_reward_summary
fi

step node_reward_history
if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 reward-history --address $NODE_PAYOUT_ADDRESS" \
  'len(data) > 0' \
  30 1 >/dev/null; then
  pass node_reward_history
else
  fail node_reward_history
fi

step peer_summary
if wait_json \
  "docker compose exec -T node-a chipcoin --network devnet --data /data/node-a-devnet.sqlite3 peer-summary" \
  'data["peer_count"] >= 2 and data["peer_count_by_network"].get("devnet", 0) >= 2' \
  30 1 >/dev/null; then
  pass peer_summary
else
  fail peer_summary
fi
