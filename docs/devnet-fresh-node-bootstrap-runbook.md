# Fresh Node Bootstrap Runbook

This is the shortest clean-room path for a new devnet full node from zero.

Target:

- Linux host
- Python `3.11+`
- optional snapshot bootstrap
- no miner required

## 1. Clone and install

```bash
sudo mkdir -p /opt/chipcoin /var/lib/chipcoin/newnode /var/log/chipcoin
sudo chown -R "$USER":"$USER" /opt/chipcoin /var/lib/chipcoin/newnode /var/log/chipcoin

git clone <repo-url> /opt/chipcoin
cd /opt/chipcoin
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e .[dev]
```

Rough edges:

- the runbook assumes Python and build prerequisites are already available on the host
- there is no single-command installer for OS packages and service files

## 2. Environment setup

```bash
cd /opt/chipcoin
cp .env.example .env

mkdir -p /var/lib/chipcoin/newnode
: > /var/lib/chipcoin/newnode/node.sqlite3

sed -i 's|^CHIPCOIN_RUNTIME_DIR=.*|CHIPCOIN_RUNTIME_DIR=/var/lib/chipcoin/newnode|' .env
sed -i 's|^NODE_DATA_PATH=.*|NODE_DATA_PATH=/var/lib/chipcoin/newnode/node.sqlite3|' .env
sed -i 's|^NODE_HTTP_BIND_PORT=.*|NODE_HTTP_BIND_PORT=8081|' .env
sed -i 's|^NODE_P2P_BIND_PORT=.*|NODE_P2P_BIND_PORT=18444|' .env
sed -i 's|^MINING_NODE_URLS=.*|MINING_NODE_URLS=http://127.0.0.1:8081|' .env
```

Rough edges:

- `NODE_DATA_PATH` must be a file, not a directory
- snapshot bootstrap is still a manual CLI step, not runtime behavior driven from `.env`

## 3. Snapshot bootstrap, if available

If you have a current snapshot:

```bash
cp /path/to/devnet.snapshot /var/lib/chipcoin/newnode/devnet.snapshot

./.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/newnode/node.sqlite3 snapshot-import \
  --snapshot-file /var/lib/chipcoin/newnode/devnet.snapshot
```

If you do not have a snapshot, skip this step and use full sync.

Recommendation:

- use snapshot bootstrap for operational bring-up
- use full sync only when you specifically want genesis validation

## 4. Start first sync

```bash
tmux new -d -s chip-node '/opt/chipcoin/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/newnode/node.sqlite3 run --listen-host 0.0.0.0 --listen-port 18444 --http-host 127.0.0.1 --http-port 8081 --peer chipcoinprotocol.com:18444'
sleep 5
tmux capture-pane -pt chip-node | tail -n 60
```

## 5. First sync verification

```bash
curl -s http://127.0.0.1:8081/v1/health | jq
curl -s http://127.0.0.1:8081/v1/status | jq '{
  height,
  tip_hash,
  sync_phase,
  handshaken_peer_count,
  bootstrap_mode,
  snapshot_anchor_height,
  snapshot_anchor_hash
}'
```

Expected:

- `GET /v1/health` returns `status=ok`
- `sync_phase` progresses to `synced`
- if snapshot was used:
  - `bootstrap_mode = "snapshot"`
  - `snapshot_anchor_height` is non-null

## 6. First reward diagnostics verification

```bash
CC='/opt/chipcoin/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/newnode/node.sqlite3'
HEIGHT="$(curl -s http://127.0.0.1:8081/v1/status | jq -r '.height // 0')"
EPOCH=$(( HEIGHT / 100 ))

$CC node-registry | jq
$CC reward-epoch-summary --epoch-index "$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/epoch-summary?epoch_index=$EPOCH" | jq
```

If you know a specific reward node id:

```bash
$CC reward-node-status --node-id reward-node-a --epoch-index "$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/node-status?node_id=reward-node-a&epoch_index=$EPOCH" | jq
```

## 7. Fail signals

- node process exits immediately
- `GET /v1/health` fails
- `GET /v1/status` never reaches a usable peer set
- snapshot import fails on a supposedly current snapshot
- CLI and HTTP reward diagnostics disagree

## 8. Rough edges still present

- no one-command installer for OS packages and service files
- snapshot bootstrap remains a manual import step
- public devnet bootstrap endpoints are convenience infrastructure, not guaranteed availability
- the cleanest operational bootstrap path depends on having a fresh published snapshot
