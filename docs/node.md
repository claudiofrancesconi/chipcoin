# Node

## Purpose

The Chipcoin node maintains chain state, validates blocks and transactions, exposes the HTTP API, and participates in the P2P network.

The current public release does not use a node wallet file at runtime.

## Role Boundaries

The node is responsible for:

- validation
- chain and mempool persistence
- peer discovery and synchronization
- HTTP API serving

The node is not responsible for:

- mining payout key management
- browser wallet secret storage
- explorer UI hosting requirements
- bootstrap authority over consensus

Local node state:

- `/runtime/node.sqlite3` inside Docker
- the host file mapped from `NODE_DATA_PATH`

`NODE_DATA_PATH` must be a writable SQLite file path. If you point it at a directory, the container now fails early with an explicit error instead of crashing later inside SQLite.

Network state:

- remote peers
- current best chain
- public endpoint reachability

## Runtime Inputs

Relevant `.env` keys:

- `CHIPCOIN_RUNTIME_DIR`
- `CHIPCOIN_NETWORK`
- `NODE_DATA_PATH`
- `NODE_LOG_LEVEL`
- `NODE_P2P_BIND_PORT`
- `NODE_HTTP_BIND_PORT`
- `CHIPCOIN_HTTP_ALLOWED_ORIGINS`
- `NODE_DIRECT_PEERS`
- `NODE_DIRECT_PEER`
- `NODE_BOOTSTRAP_URL`
- `DIRECT_PEERS`
- `DIRECT_PEER`
- `BOOTSTRAP_URL`
- `BOOTSTRAP_PEER_LIMIT`
- `PEER_DISCOVERY_ENABLED`
- `PEERBOOK_MAX_SIZE`
- `PEER_ADDR_MAX_PER_MESSAGE`
- `PEER_ADDR_RELAY_LIMIT_PER_INTERVAL`
- `PEER_ADDR_RELAY_INTERVAL_SECONDS`
- `PEER_STALE_AFTER_SECONDS`
- `PEER_RETRY_BACKOFF_BASE_SECONDS`
- `PEER_RETRY_BACKOFF_MAX_SECONDS`
- `PEER_DISCOVERY_STARTUP_PREFER_PERSISTED`
- `HEADERS_SYNC_ENABLED`
- `HEADERS_MAX_PER_MESSAGE`
- `BLOCK_DOWNLOAD_WINDOW_SIZE`
- `BLOCK_MAX_INFLIGHT_PER_PEER`
- `BLOCK_REQUEST_TIMEOUT_SECONDS`
- `HEADERS_SYNC_PARALLEL_PEERS`
- `HEADERS_SYNC_START_HEIGHT_GAP_THRESHOLD`
- `INITIAL_SYNC_CONSERVATIVE_DEFAULTS`
- `PEER_MISBEHAVIOR_WARNING_THRESHOLD`
- `PEER_MISBEHAVIOR_DISCONNECT_THRESHOLD`
- `PEER_MISBEHAVIOR_BAN_THRESHOLD`
- `PEER_MISBEHAVIOR_BAN_DURATION_SECONDS`
- `PEER_MISBEHAVIOR_DECAY_INTERVAL_SECONDS`
- `PEER_MISBEHAVIOR_DECAY_STEP`

## Start

```bash
docker compose up --build node
```

Detached:

```bash
docker compose up -d --build node
```

## Stop

```bash
docker compose stop node
```

or:

```bash
docker compose down
```

## Logs

```bash
docker compose logs -f node
```

## Restart And Update

Restart only the node:

```bash
docker compose restart node
```

Rebuild after code or image changes:

```bash
git pull origin main
docker compose up --build -d node
```

Expected after restart:

- chain tip persists
- peerbook persists
- bans and backoff state persist
- the runtime rebuilds live sync scheduling from local chain state and current peers

Normal short-lived post-restart states:

- `sync.mode=idle`
- `operator_summary.connectivity_state=no_active_peers`
- a few outbound reconnect attempts before peers handshake again

Peerbook hygiene:

- discovered peers should converge to canonical reusable endpoints, normally `host:18444` on `devnet`
- if old transient discovered aliases or stale penalty state accumulate, prefer the CLI over manual SQL

Prune transient discovered aliases:

```bash
docker compose exec node chipcoin --data /runtime/node.sqlite3 peerbook-clean
```

Prune transient aliases and clear saved backoff / ban / misbehavior state:

```bash
docker compose exec node chipcoin --data /runtime/node.sqlite3 peerbook-clean --reset-penalties
```

## HTTP API

Default local URL:

- `http://127.0.0.1:8081`

Useful endpoints:

- `GET /v1/status`
- `GET /v1/peers`
- `GET /v1/blocks`
- `GET /v1/block?height=<height>`
- `GET /v1/block?hash=<hash>`
- `GET /v1/tx/<txid>`
- `GET /v1/address/<address>`
- `GET /v1/address/<address>/utxos`
- `GET /v1/address/<address>/history`
- `GET /v1/mempool`
- `GET /v1/peers/summary`

## Stable Client API Subset

The current product-facing HTTP API subset, stable enough for the explorer and browser wallet, is:

- `GET /v1/health`
- `GET /v1/status`
- `GET /v1/blocks`
- `GET /v1/block`
- `GET /v1/tx/<txid>`
- `POST /v1/tx/submit`
- `GET /v1/address/<address>`
- `GET /v1/address/<address>/utxos`
- `GET /v1/address/<address>/history`
- `GET /v1/mempool`
- `GET /v1/peers`
- `GET /v1/peers/summary`

Contract notes for this stable subset:

- all success responses are JSON
- all API errors return JSON in the form:
  - `{"error": {"code": "<stable_code>", "message": "<human_message>"}}`
- `GET /v1/health` returns:
  - `status`
  - `api_version`
  - `network`
- `GET /v1/status` returns:
  - `api_version`
  - chain tip fields
  - peer counts
  - `banned_peer_count`
  - `sync` snapshot

The stable subset freezes required fields used by:

- `apps/explorer/src/api.ts`
- `apps/browser-wallet/src/api/client.ts`

Additional fields may still be added in future releases, but the documented required fields in this subset should not change incompatibly without an explicit product-level migration.

`GET /v1/status` now includes a `sync` snapshot with:

- validated tip height/hash
- best known header height/hash
- current sync mode
- in-flight block request count
- header peers
- block peers
- stalled peers
- download window position

`GET /v1/peers` and `GET /v1/peers/summary` include peer misbehavior and temporary-ban diagnostics.

## Peer Discovery

Chipcoin uses bounded `getaddr` / `addr` discovery plus a persistent SQLite peerbook.

Peer source classes:

- `manual`: explicitly configured peers such as `DIRECT_PEERS`, `DIRECT_PEER`, or `chipcoin add-peer`
- `seed`: bootstrap-derived or local-seeding fallback peers
- `discovered`: peers learned from network gossip or successful inbound/outbound observations

Peer states exposed through diagnostics:

- `manual`
- `seed`
- `discovered`
- `good`
- `questionable`
- `banned`

Stored peer metadata now includes:

- source
- first/last seen timestamps
- last success / last failure
- success / failure counters
- reconnect backoff state
- temporary ban state
- misbehavior score
- quality score

Startup discovery order:

1. load persisted peers from the peerbook
2. prefer healthy persisted peers when available
3. fall back to explicit manual or seed peers when needed
4. continue learning through bounded `addr` gossip

Operational limits:

- incoming `addr` payloads are capped
- relayed peer batches are capped
- peer relay is rate-limited per session
- stale discovered peers are expired automatically
- the peerbook is trimmed to a bounded maximum size
- banned peers are excluded from relay and outbound selection

Useful operator checks:

```bash
chipcoin --data /path/to/node.sqlite3 list-peers
chipcoin --data /path/to/node.sqlite3 peer-summary
curl http://127.0.0.1:8081/v1/peers
curl http://127.0.0.1:8081/v1/peers/summary
```

Look for:

- `source`
- `peer_state`
- `success_count`
- `failure_count`
- `ban_until`
- `backoff_until`

On startup, the runtime now emits warnings when it detects clearly isolated or suspicious configurations, such as:

- no configured peers plus an empty peerbook
- discovery disabled with no peers configured
- unusually low block request timeout
- a block download window smaller than the per-peer inflight cap

## Headers-First Sync

Chipcoin now performs initial synchronization in two stages:

1. header sync
2. bounded multi-peer block download

Operational behavior:

- the node requests `headers` from one or more suitable peers
- headers are validated as far as possible before any block body is requested
- the node tracks the strongest known header tip separately from the validated chain tip
- once headers reveal missing blocks, the runtime opens a bounded download window
- block requests are spread across multiple healthy peers
- each peer has its own in-flight request cap
- stalled block requests are expired and reassigned
- consistently stalling peers are penalized and can be dropped

Relevant `.env` knobs:

- `HEADERS_SYNC_ENABLED`
- `HEADERS_MAX_PER_MESSAGE`
- `BLOCK_DOWNLOAD_WINDOW_SIZE`
- `BLOCK_MAX_INFLIGHT_PER_PEER`
- `BLOCK_REQUEST_TIMEOUT_SECONDS`
- `HEADERS_SYNC_PARALLEL_PEERS`
- `HEADERS_SYNC_START_HEIGHT_GAP_THRESHOLD`

The defaults are intentionally conservative and should work for small devnet operators without tuning.

For pristine databases, the container can automatically apply an even more conservative first-sync profile when startup peers are configured:

- `BLOCK_MAX_INFLIGHT_PER_PEER=4`
- `BLOCK_REQUEST_TIMEOUT_SECONDS=60`
- `HEADERS_SYNC_PARALLEL_PEERS=1`
- `BLOCK_DOWNLOAD_WINDOW_SIZE=32`

This temporary profile is enabled by `INITIAL_SYNC_CONSERVATIVE_DEFAULTS=true` and only applies when the local SQLite file is still empty.

Useful operator checks:

```bash
chipcoin --data /path/to/node.sqlite3 status
curl http://127.0.0.1:8081/v1/status
docker compose logs -f node
```

Look for:

- `sync.mode`
- `sync.validated_tip_height`
- `sync.best_header_height`
- `sync.inflight_block_count`
- `sync.block_peers`
- `sync.stalled_peers`
- `operator_summary.sync_state`
- `operator_summary.connectivity_state`
- `operator_summary.warnings`

Typical runtime log lines:

- `headers received ...`
- `sync scheduled block downloads ...`
- `sync block request stalled ... action=reassign`
- `sync complete ... final_local_height=... peer_target_height=... best_header_height=...`

If the local node advances while a slower peer is still connected, `final_local_height` can legitimately exceed `peer_target_height`.

## Operator Checks

Fast local checks:

```bash
chipcoin --data /path/to/node.sqlite3 status
chipcoin --data /path/to/node.sqlite3 peer-summary
chipcoin --data /path/to/node.sqlite3 list-peers
curl http://127.0.0.1:8081/v1/status
curl http://127.0.0.1:8081/v1/peers/summary
```

Interpret `status` using:

- `operator_summary.sync_state`
- `operator_summary.connectivity_state`
- `operator_summary.peer_attention`
- `operator_summary.warnings`

Interpret `peer-summary` using:

- `operator_summary.peer_health`
- `operator_summary.non_banned_peer_count`
- `operator_summary.active_backoff_peer_count`
- `operator_summary.active_ban_count`
- `operator_summary.warnings`

Interpret `list-peers` using:

- `peer_state`
- `source`
- `handshake_complete`
- `backoff_remaining_seconds`
- `ban_remaining_seconds`
- `last_penalty_reason`
- `last_error`

Typical operator readings:

- `connectivity_state=no_known_peers`
  - the node currently has no configured or persisted peers to dial
- `connectivity_state=no_active_peers`
  - peers are known, but none are currently handshaken
- `sync_state=headers`
  - the node is still learning the best header chain
- `sync_state=blocks`
  - headers are known and missing blocks are being fetched
- `peer_health=all_banned`
  - the known peer set is currently unusable until ban expiry or manual cleanup
- `peer_health=degraded`
  - the node still has peers, but backoff/questionable state needs attention

## Practical Runbook

### Node Not Syncing

Check:

```bash
chipcoin --data /path/to/node.sqlite3 status
chipcoin --data /path/to/node.sqlite3 peer-summary
docker compose logs --tail=100 node
```

Look for:

- `operator_summary.connectivity_state=no_known_peers`
- `operator_summary.connectivity_state=no_active_peers`
- `sync.best_header_height > sync.validated_tip_height`
- `sync.stalled_peers`
- repeated `sync block request stalled ... action=reassign`

If this happens on a brand-new node:

- verify that `NODE_DATA_PATH` is a file, not a directory
- prefer `DIRECT_PEERS` with multiple known-good startup peers
- keep `INITIAL_SYNC_CONSERVATIVE_DEFAULTS=true` unless you have already tuned the network

Typical causes:

- no reachable peers
- all known peers in backoff or banned state
- peer reachable but slow or dropping during sync

### Peerbook Empty

Symptoms:

- `peer_count=0`
- `operator_summary.connectivity_state=no_known_peers`
- startup warning mentioning empty peerbook

Checks:

```bash
chipcoin --data /path/to/node.sqlite3 peer-summary
```

Typical recovery:

- add a manual peer with `DIRECT_PEERS`, `DIRECT_PEER`, or `chipcoin add-peer`
- let the node reconnect and relearn peers

If you intentionally want an isolated node:

- leave `DIRECT_PEERS`, `DIRECT_PEER`, and `BOOTSTRAP_URL` empty
- expect `peer_count=0` until inbound peers arrive or you add peers manually

### Peer Banned Unexpectedly

Checks:

```bash
chipcoin --data /path/to/node.sqlite3 peer-summary
chipcoin --data /path/to/node.sqlite3 list-peers
```

Look for:

- `banned_peer_count`
- `last_penalty_reason`
- `ban_remaining_seconds`

Typical recovery:

- wait for ban expiry when the peer really was unstable
- if the ban came from stale local state, stop the node and clear peer ban fields in the local SQLite database before restart

### Miner Waiting For Initial Sync

Expected miner log lines:

- `mining paused reason=awaiting_initial_peer_sync`
- `mining paused reason=chain_not_synced`
- `mining resumed ...`

Interpretation:

- `awaiting_initial_peer_sync`
  - the miner has not completed its initial peer-based sync gate yet
- `chain_not_synced`
  - the miner sees a higher remote chain and will not mine ahead of it
- `mining resumed`
  - the local chain is caught up enough for mining to continue

Checks:

```bash
docker compose logs --tail=100 miner
docker compose logs --tail=100 node
```

### Stale Peerbook Or Stale Bans

Symptoms:

- peers remain banned long after the network is healthy
- `peer_health=all_banned`
- `handshaken_peer_count=0` even though live peers exist

Checks:

```bash
chipcoin --data /path/to/node.sqlite3 peer-summary
chipcoin --data /path/to/node.sqlite3 list-peers
```

Typical recovery:

1. stop the node
2. back up the SQLite database
3. clear stale ban/backoff fields in the `peers` table
4. restart and let the node reconnect

Minimal SQLite recovery example on the host:

```bash
docker compose down
cp /path/to/node.sqlite3 /path/to/node.sqlite3.bak
sqlite3 /path/to/node.sqlite3 "
UPDATE peers
SET
  ban_until = NULL,
  misbehavior_score = 0,
  backoff_until = 0,
  reconnect_attempts = 0,
  last_penalty_reason = NULL,
  last_penalty_at = NULL
WHERE network = 'devnet';
"
docker compose up --build -d node
```

### Restart And Recovery Expectations

Expected behavior:

- validated chain state stays intact across restart
- peerbook survives restart
- headers-first sync resumes from local chain state and live peers
- the exact block download plan is not persisted; the runtime rebuilds it after restart

Normal after restart:

- a short period of `sync_state=idle` or `connectivity_state=no_active_peers`
- outbound reconnect attempts before new handshakes complete

## Peer Misbehavior Policy

The node tracks peer misbehavior separately from consensus validity.

Default policy:

- warn when a peer reaches score `25`
- disconnect when a peer reaches score `50`
- temporarily ban when a peer reaches score `100`
- temporary bans expire after `1800` seconds
- scores decay by `5` every `300` seconds without new violations

Typical penalty events include:

- malformed or undecodable messages
- handshake failures
- repeated timeout or stall behavior
- oversized `headers` / `inv` / `getdata` / `addr` messages
- invalid blocks or transactions relayed by a peer

Operator surfaces:

- `chipcoin peer-summary`
- `GET /v1/peers`
- `GET /v1/peers/summary`
- runtime logs with `peer misbehavior ... action=...`

## Public Reachability

Public peer reachability is strongly recommended for healthy mesh behavior on the public devnet.

Required for public peer reachability:

- `TCP 18444` for the node P2P listener

Optional operator interfaces:

- `TCP 8081` for the HTTP API
- `TCP 4173` for an explorer, if you run one

Nodes that do not expose `TCP 18444` can still make outbound connections and sync normally, but they contribute less to peer discovery and network resilience because other peers cannot reliably initiate sessions back to them.

Operational guidance:

- set `NODE_P2P_BIND_PORT=18444`
- allow `TCP 18444` through the host firewall
- if the node sits behind NAT, forward external `TCP 18444` to the machine running the node
- for home routers, prefer a stable local LAN IP for the node host before configuring port forwarding
- verify that the endpoint other peers learn is your real public host and port

Basic validation:

- confirm the node is listening locally on `0.0.0.0:18444` or the intended bind address
- test `TCP 18444` from an external machine or network, not only from localhost
- confirm peers can connect inbound after router and firewall changes

## Notes

- `NODE_DIRECT_PEERS` is the preferred way to define one or more explicit startup peers for the node.
- `NODE_DIRECT_PEER` remains supported for compatibility with older configs.
- `NODE_BOOTSTRAP_URL` is the preferred bootstrap helper setting for the node.
- If `NODE_*` discovery vars are unset, the node falls back to `DIRECT_PEERS`, `DIRECT_PEER`, and `BOOTSTRAP_URL`.
- Leave both `NODE_*` and shared discovery vars empty for an isolated node.
- Public browser wallet access may require `CHIPCOIN_HTTP_ALLOWED_ORIGINS` to include the wallet origin.
- The recommended runtime directory is outside the repo, for example `/home/komarek/Chipcoin-runtime`.
