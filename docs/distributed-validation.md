## Distributed Validation Procedure

This procedure validates the current operator model end to end:

- node A exports a snapshot
- node B boots from that snapshot
- miner mines remotely against node A
- node A advances beyond the snapshot anchor
- node B catches only the post-anchor delta
- both nodes converge on the same final tip

### Assumptions

- node A is reachable over P2P and HTTP mining API
- node B can reach node A over P2P
- the miner can reach node A over HTTP
- examples below use:
  - node A: `chipcoinprotocol.com`
  - node B host: `tilt`
  - miner host: `tobia`

### 1. Export a snapshot from node A

On node A:

```bash
cd /opt/chipcoin
docker compose exec node chipcoin --network devnet --data /runtime/node.sqlite3 snapshot-export --snapshot-file /runtime/devnet.snapshot
docker compose cp node:/runtime/devnet.snapshot ./devnet.snapshot
```

Optional signed snapshot:

```bash
chipcoin snapshot-sign --snapshot-file ./devnet.snapshot --private-key-hex <ED25519_PRIVATE_KEY_HEX>
```

### 2. Transfer the snapshot to node B

From node A:

```bash
scp ./devnet.snapshot tilt:/opt/chipcoin/devnet.snapshot
```

### 3. Bootstrap node B from the snapshot

On node B:

```bash
cd /opt/chipcoin
docker compose down
rm -f /var/lib/chipcoin/data/node-devnet.sqlite3
touch /var/lib/chipcoin/data/node-devnet.sqlite3
```

Unsigned snapshot:

```bash
docker compose run --rm \
  -v /opt/chipcoin/devnet.snapshot:/tmp/devnet.snapshot:ro \
  --entrypoint chipcoin \
  node \
  --network devnet \
  --data /runtime/node.sqlite3 \
  snapshot-import \
  --snapshot-file /tmp/devnet.snapshot
```

Signed snapshot in enforce mode:

```bash
docker compose run --rm \
  -v /opt/chipcoin/devnet.snapshot:/tmp/devnet.snapshot:ro \
  --entrypoint chipcoin \
  node \
  --network devnet \
  --data /runtime/node.sqlite3 \
  snapshot-import \
  --snapshot-file /tmp/devnet.snapshot \
  --snapshot-trust-mode enforce \
  --snapshot-trusted-key <ED25519_PUBLIC_KEY_HEX>
```

Verify imported state before runtime start:

```bash
docker compose run --rm --entrypoint chipcoin node --network devnet --data /runtime/node.sqlite3 status
```

Expected:

- `bootstrap_mode=snapshot`
- `snapshot_anchor_height=<anchor height>`
- `tip_hash=<anchor hash>`

### 4. Start node B and verify no historical replay

On node B:

```bash
docker compose up -d node
docker compose logs -f node
```

Expected log pattern:

- `bootstrap_mode=snapshot`
- `sync start ... local_height=<snapshot anchor>`
- not `local_height=0`

### 5. Start a remote miner against node A

On miner host:

```bash
cd /opt/chipcoin
MINING_NODE_URLS=https://api.chipcoinprotocol.com docker compose up -d miner
docker compose logs -f miner
```

Expected log pattern:

- `mining template acquired node=https://api.chipcoinprotocol.com ...`
- `block accepted ...`

### 6. Verify node A advances and node B catches post-anchor delta

On node A:

```bash
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase}'
```

On node B:

```bash
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash, sync_phase, sync}'
docker compose logs --tail=100 node
```

Expected:

- node A height moves above the snapshot anchor
- node B shows `sync_phase=syncing_post_anchor_delta`
- node B downloads only the post-anchor delta

### 7. Confirm final convergence

On node A:

```bash
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash}'
```

On node B:

```bash
curl -s http://127.0.0.1:8081/v1/status | jq '{height, tip_hash}'
```

Success criteria:

- same `height`
- same `tip_hash`
- node B ends with `sync_phase=synced`
