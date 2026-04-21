# Devnet Operator Upgrade Runbook

Target release:

- version: `0.1.1`
- branch: `main`

This runbook assumes the current verified host modes:

- `chipcom`: Docker Compose base node/miner host
- `tilt`: manual venv runtime under `/opt/chipcoin`
- `tobia`: manual venv runtime under `/home/komarek/Documents/CODEX/Chipcoin-v2`

Global rules:

- upgrade one host at a time
- keep the miner stopped until node verification passes
- do not reset chain state during the normal upgrade path
- if one host fails, roll back that host before touching the next host

## chipcom

Backup step:

```bash
cd /opt/chipcoin
set -a
. ./.env
set +a

mkdir -p /var/backups/chipcoin
cp "$NODE_DATA_PATH" "/var/backups/chipcoin/node-devnet.sqlite3.$(date +%Y%m%d-%H%M%S)"
cp .env "/var/backups/chipcoin/.env.$(date +%Y%m%d-%H%M%S)"
git rev-parse HEAD | tee "/var/backups/chipcoin/git-head.$(date +%Y%m%d-%H%M%S).txt"
```

Stop step:

```bash
cd /opt/chipcoin
docker compose stop miner node
docker compose ps
```

Code update step:

```bash
cd /opt/chipcoin
git fetch origin
git checkout main
git pull --ff-only origin main
./.venv/bin/pip install -e .[dev]
```

Environment verification:

```bash
cd /opt/chipcoin
set -a
. ./.env
set +a

test -f "$NODE_DATA_PATH"
test ! -d "$NODE_DATA_PATH"
ls -l "$NODE_DATA_PATH"
docker compose config >/tmp/chipcom.compose.rendered.yaml
tail -n 20 /tmp/chipcom.compose.rendered.yaml
```

Start step:

```bash
cd /opt/chipcoin
docker compose up -d node
sleep 5
docker compose logs --tail=60 node
```

Post-start verification:

```bash
cd /opt/chipcoin
set -a
. ./.env
set +a

CC='docker compose exec -T node chipcoin --network devnet --data /runtime/node.sqlite3'

curl -s http://127.0.0.1:8081/v1/health | jq
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase, handshaken_peer_count, snapshot_anchor_height, snapshot_anchor_hash}'
$CC mempool | jq
$CC node-registry | jq '[.[] | {node_id, active, eligibility_status, eligibility_reason, warmup_complete, last_renewal_height}]'
```

Reward diagnostics verification:

```bash
cd /opt/chipcoin
set -a
. ./.env
set +a

CC='docker compose exec -T node chipcoin --network devnet --data /runtime/node.sqlite3'
HEIGHT="$(curl -s http://127.0.0.1:8081/v1/status | jq -r '.height // 0')"
EPOCH=$(( HEIGHT / 100 ))

$CC reward-epoch-summary --epoch-index "$EPOCH" | jq
$CC reward-node-status --node-id reward-node-a --epoch-index "$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/epoch-summary?epoch_index=$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/node-status?node_id=reward-node-a&epoch_index=$EPOCH" | jq
```

Fail signals:

- node logs show SQLite mount or startup failure
- `GET /v1/health` fails
- `GET /v1/status` does not return valid JSON
- reward CLI and HTTP diagnostics disagree
- local tip does not reconverge with `tilt` and `tobia`

Rollback step:

```bash
cd /opt/chipcoin
docker compose stop miner node
git reset --hard HEAD~1
LATEST_DB="$(ls -1t /var/backups/chipcoin/node-devnet.sqlite3.* | head -n 1)"
cp "$LATEST_DB" "$NODE_DATA_PATH"
./.venv/bin/pip install -e .[dev]
docker compose up -d node
sleep 5
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase}'
```

## tilt

Backup step:

```bash
cd /opt/chipcoin
mkdir -p /var/backups/chipcoin
cp /var/lib/chipcoin/tilt/node.sqlite3 "/var/backups/chipcoin/tilt-node.sqlite3.$(date +%Y%m%d-%H%M%S)"
cp -r /var/lib/chipcoin/tilt/wallets "/var/backups/chipcoin/tilt-wallets.$(date +%Y%m%d-%H%M%S)"
git rev-parse HEAD | tee "/var/backups/chipcoin/tilt-git-head.$(date +%Y%m%d-%H%M%S).txt"
```

Stop step:

```bash
tmux kill-session -t chip-node 2>/dev/null || true
tmux kill-session -t chip-miner 2>/dev/null || true
tmux ls
```

Code update step:

```bash
cd /opt/chipcoin
git fetch origin
git checkout main
git pull --ff-only origin main
./.venv/bin/pip install -e .[dev]
```

Environment verification:

```bash
cd /opt/chipcoin
test -f /var/lib/chipcoin/tilt/node.sqlite3
test -d /var/lib/chipcoin/tilt/wallets
test -f /var/lib/chipcoin/tilt/wallets/reward-b.json
./.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tilt/node.sqlite3 status | jq '{height, tip_hash, sync_phase}'
```

Start step:

```bash
tmux new -d -s chip-node '/opt/chipcoin/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tilt/node.sqlite3 run --listen-host 0.0.0.0 --listen-port 18444 --http-host 127.0.0.1 --http-port 8081 --peer chipcoinprotocol.com:18444'
sleep 5
tmux capture-pane -pt chip-node | tail -n 60
```

Post-start verification:

```bash
CC='/opt/chipcoin/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tilt/node.sqlite3'

curl -s http://127.0.0.1:8081/v1/health | jq
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase, handshaken_peer_count, snapshot_anchor_height, snapshot_anchor_hash}'
$CC mempool | jq
$CC node-registry | jq '[.[] | {node_id, active, eligibility_status, eligibility_reason, warmup_complete, last_renewal_height}]'
```

Reward diagnostics verification:

```bash
CC='/opt/chipcoin/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tilt/node.sqlite3'
HEIGHT="$(curl -s http://127.0.0.1:8081/v1/status | jq -r '.height // 0')"
EPOCH=$(( HEIGHT / 100 ))

$CC reward-epoch-summary --epoch-index "$EPOCH" | jq
$CC reward-node-status --node-id reward-node-b --epoch-index "$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/epoch-summary?epoch_index=$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/node-status?node_id=reward-node-b&epoch_index=$EPOCH" | jq
```

Fail signals:

- tmux session exits immediately
- `GET /v1/status` is unreachable after start
- reward CLI and HTTP diagnostics disagree

Rollback step:

```bash
tmux kill-session -t chip-node 2>/dev/null || true
cd /opt/chipcoin
git reset --hard HEAD~1
LATEST_DB="$(ls -1t /var/backups/chipcoin/tilt-node.sqlite3.* | head -n 1)"
cp "$LATEST_DB" /var/lib/chipcoin/tilt/node.sqlite3
rm -rf /var/lib/chipcoin/tilt/wallets
LATEST_WALLETS="$(ls -1dt /var/backups/chipcoin/tilt-wallets.* | head -n 1)"
cp -r "$LATEST_WALLETS" /var/lib/chipcoin/tilt/wallets
./.venv/bin/pip install -e .[dev]
tmux new -d -s chip-node '/opt/chipcoin/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tilt/node.sqlite3 run --listen-host 0.0.0.0 --listen-port 18444 --http-host 127.0.0.1 --http-port 8081 --peer chipcoinprotocol.com:18444'
sleep 5
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase}'
```

## tobia

Backup step:

```bash
cd /home/komarek/Documents/CODEX/Chipcoin-v2
mkdir -p /var/backups/chipcoin
cp /var/lib/chipcoin/tobia/node.sqlite3 "/var/backups/chipcoin/tobia-node.sqlite3.$(date +%Y%m%d-%H%M%S)"
cp -r /var/lib/chipcoin/tobia/wallets "/var/backups/chipcoin/tobia-wallets.$(date +%Y%m%d-%H%M%S)"
git rev-parse HEAD | tee "/var/backups/chipcoin/tobia-git-head.$(date +%Y%m%d-%H%M%S).txt"
```

Stop step:

```bash
tmux kill-session -t chip-node 2>/dev/null || true
tmux kill-session -t chip-miner 2>/dev/null || true
tmux ls
```

Code update step:

```bash
cd /home/komarek/Documents/CODEX/Chipcoin-v2
git fetch origin
git checkout main
git pull --ff-only origin main
./.venv/bin/pip install -e .[dev]
```

Environment verification:

```bash
cd /home/komarek/Documents/CODEX/Chipcoin-v2
test -f /var/lib/chipcoin/tobia/node.sqlite3
test -d /var/lib/chipcoin/tobia/wallets
test -f /var/lib/chipcoin/tobia/wallets/reward-c.json
./.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tobia/node.sqlite3 status | jq '{height, tip_hash, sync_phase}'
```

Start step:

```bash
tmux new -d -s chip-node '/home/komarek/Documents/CODEX/Chipcoin-v2/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tobia/node.sqlite3 run --listen-host 0.0.0.0 --listen-port 18444 --http-host 127.0.0.1 --http-port 8081 --peer chipcoinprotocol.com:18444'
sleep 5
tmux capture-pane -pt chip-node | tail -n 60
```

Post-start verification:

```bash
CC='/home/komarek/Documents/CODEX/Chipcoin-v2/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tobia/node.sqlite3'

curl -s http://127.0.0.1:8081/v1/health | jq
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase, handshaken_peer_count, snapshot_anchor_height, snapshot_anchor_hash}'
$CC mempool | jq
$CC node-registry | jq '[.[] | {node_id, active, eligibility_status, eligibility_reason, warmup_complete, last_renewal_height}]'
```

Reward diagnostics verification:

```bash
CC='/home/komarek/Documents/CODEX/Chipcoin-v2/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tobia/node.sqlite3'
HEIGHT="$(curl -s http://127.0.0.1:8081/v1/status | jq -r '.height // 0')"
EPOCH=$(( HEIGHT / 100 ))

$CC reward-epoch-summary --epoch-index "$EPOCH" | jq
$CC reward-node-status --node-id reward-node-c --epoch-index "$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/epoch-summary?epoch_index=$EPOCH" | jq
curl -s "http://127.0.0.1:8081/v1/rewards/node-status?node_id=reward-node-c&epoch_index=$EPOCH" | jq
```

Fail signals:

- tmux session exits immediately
- `GET /v1/status` is unreachable after start
- reward CLI and HTTP diagnostics disagree

Rollback step:

```bash
tmux kill-session -t chip-node 2>/dev/null || true
cd /home/komarek/Documents/CODEX/Chipcoin-v2
git reset --hard HEAD~1
LATEST_DB="$(ls -1t /var/backups/chipcoin/tobia-node.sqlite3.* | head -n 1)"
cp "$LATEST_DB" /var/lib/chipcoin/tobia/node.sqlite3
rm -rf /var/lib/chipcoin/tobia/wallets
LATEST_WALLETS="$(ls -1dt /var/backups/chipcoin/tobia-wallets.* | head -n 1)"
cp -r "$LATEST_WALLETS" /var/lib/chipcoin/tobia/wallets
./.venv/bin/pip install -e .[dev]
tmux new -d -s chip-node '/home/komarek/Documents/CODEX/Chipcoin-v2/.venv/bin/chipcoin --network devnet --data /var/lib/chipcoin/tobia/node.sqlite3 run --listen-host 0.0.0.0 --listen-port 18444 --http-host 127.0.0.1 --http-port 8081 --peer chipcoinprotocol.com:18444'
sleep 5
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase}'
```
