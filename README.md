# Chipcoin v2

Chipcoin v2 is a Python-first, Bitcoin-inspired blockchain project focused on a small but operational devnet stack.

This public repository is centered on three components:

- `node`
- `miner`
- `browser-wallet`

The current public release target is `devnet`, not mainnet.

Public devnet fallback defaults included in `.env.example`:

- node API: `https://api.chipcoinprotocol.com`
- bootstrap peer: `chipcoinprotocol.com:18444`
- public P2P port: `18444/tcp`
- explorer URL: `https://explorer.chipcoinprotocol.com`

These are fallback defaults only. They are not required and can be replaced with your own node, peer, and inspection tooling.
Public devnet endpoints are provided for convenience and may change or become unavailable.

## Current Status

What works today:

- SQLite-backed node runtime
- P2P block and transaction relay
- peer misbehavior scoring with temporary bans and decay
- persistent peerbook with bounded addr/getaddr discovery
- headers-first initial sync with bounded multi-peer block download
- snapshot-based fast bootstrap for nodes using a trusted chainstate snapshot plus post-anchor delta sync
- HTTP API for status, blocks, transactions, peers, and address data
- external template-based miner worker using a wallet file
- browser wallet for Chrome and Firefox
- Docker-based node and miner startup

What is intentionally limited today:

- `devnet` is the only supported public network for this release
- the node runtime does not use a wallet file yet
- browser wallet recovery phrases are Chipcoin-specific and not BIP39-compatible yet
- no multisig, no multiple accounts, no hardware wallet support
  (Explore and bootstrap instructions have moved to the private `chipcoin-central` repo.)

## Repository Scope

Relevant public areas:

- `src/chipcoin`: consensus, storage, node runtime, miner integration, wallet primitives, CLI, HTTP API
- `apps/browser-wallet`: Chrome and Firefox extension wallet
- `docker-compose.yml`: node and miner runtime
- `config/env/.env.example`: runtime configuration template
- `docs/node.md`: node setup and API notes
- `docs/miner.md`: miner setup and wallet requirements
- `docs/browser-wallet.md`: extension build and install flow

Operator-only or internal material may still exist in the tree, but it is not part of the primary public onboarding path.

## System Requirements

Documented and conservatively validated baseline:

- Host OS: Linux on x86_64 for the documented clone-to-run path
- Docker: Docker Engine with the `docker compose` plugin
- Python: 3.11+
- Node.js: 20+
- npm: current npm bundled with Node.js 20+
- Browsers: recent desktop Chrome and Firefox for the browser wallet flows

Minimum guidance for a local devnet node + miner setup:

- 2 CPU cores
- 4 GB RAM
- 5 GB free disk space

Recommended guidance:

- 4 CPU cores
- 8 GB RAM
- 10 GB free disk space

Important limits:

- macOS and Windows are not part of the documented clean-install validation path yet
- Firefox support is documented through the temporary add-on workflow, not a signed distribution flow
- public devnet endpoints are convenience defaults, not availability guarantees

Details:

- `docs/system-requirements.md`

## Quick Start

## Runtime Roles

Standard runtime roles in the current public stack:

- `node`
  - validates blocks and transactions
  - stores chain, mempool, and peerbook state in SQLite
  - exposes the HTTP API
  - participates in P2P networking
- `miner`
  - requests block templates from a node over HTTP
  - uses a miner wallet file for payout address selection
  - does not maintain a full chain database
- `browser-wallet`
  - stores keys locally in extension storage
  - signs transactions locally
  - reads chain and address state from the node HTTP API
The explorer and bootstrap materials have moved to the private `chipcoin-central` repo and are not part of this public onboarding path.

Local state vs network state:

- local state
  - node SQLite database
  - miner wallet JSON file
  - browser wallet extension storage
  - explorer local/browser-saved API base override
- network state
  - current best chain
  - peer availability
  - address balances and history served by a node
  - public endpoint reachability

### Prerequisites

- Python 3.11+
- Docker Engine with `docker compose`
- Node.js 20+ and npm if you want to build the browser wallet

### Clone And Prepare Runtime Config

```bash
git clone <repo-url>
cd chipcoin
cp .env.example .env
```

Edit `.env` for your machine before running Docker. The placeholder paths in `.env.example` will not work until you replace them with real paths on your machine.

At minimum, set real values for:

- `CHIPCOIN_RUNTIME_DIR`
- `NODE_DATA_PATH`
- `MINER_WALLET_FILE`
- `MINING_NODE_URLS`

`NODE_DATA_PATH` must be a writable SQLite file path. Do not point it at a directory.

For the shortest first run, you can either keep the public devnet defaults from `.env.example` or replace them with your own values.

If you want a fully local first run, set:

- `DIRECT_PEERS=`
- `DIRECT_PEER=`
- `BOOTSTRAP_URL=`
- `NODE_DIRECT_PEERS=`
- `NODE_DIRECT_PEER=`
- `NODE_BOOTSTRAP_URL=`
- `BOOTSTRAP_ANNOUNCE_ENABLED=false`
- `NODE_PUBLIC_HOST=`
- `NODE_PUBLIC_P2P_PORT=`
- `MINING_NODE_URLS=http://node:8081`
- `BROWSER_WALLET_DEFAULT_NODE_ENDPOINT=http://127.0.0.1:8081`

This starts an isolated local node with a miner that immediately pulls templates from the local node HTTP API.

### Full Sync vs Snapshot Bootstrap

Chipcoin now supports two node bootstrap paths:

- full sync from genesis
  - validates blocks from height 0
  - slowest startup
  - trustless path
- fast sync from snapshot
  - imports a trusted chainstate snapshot anchored to a specific block hash/height
  - validates only the delta after that anchor
  - much faster startup
  - not equivalent to genesis validation

Snapshot workflow:

```bash
chipcoin --data /runtime/node.sqlite3 snapshot-export --snapshot-file /runtime/devnet.snapshot
chipcoin --data /runtime/node.sqlite3 snapshot-import --snapshot-file /runtime/devnet.snapshot
chipcoin --data /runtime/node.sqlite3 run --snapshot-file /runtime/devnet.snapshot --peer chipcoinprotocol.com:18444
```

Use full sync when you want maximum assurance. Use snapshot bootstrap when you trust the snapshot publisher operationally and want a much faster first sync.

Snapshot format notes:

- `v2` is now the default export format
- `v2` stores metadata plus a compressed binary payload container
- `v1` JSON import remains supported for compatibility and debugging
- force `v1` export when needed with:

```bash
chipcoin --data /runtime/node.sqlite3 snapshot-export --snapshot-file /runtime/devnet.snapshot.v1.json --snapshot-format v1
```

Signing works for both `v1` and `v2` snapshots:

```bash
chipcoin snapshot-sign --snapshot-file /runtime/devnet.snapshot --private-key-hex <ED25519_PRIVATE_KEY_HEX>
```

Signed snapshot import in strict mode:

```bash
chipcoin --data /runtime/node.sqlite3 snapshot-import \
  --snapshot-file /runtime/devnet.snapshot \
  --snapshot-trust-mode enforce \
  --snapshot-trusted-key <ED25519_PUBLIC_KEY_HEX>
```

If you want your node to improve peer discovery and network resilience, keep `NODE_P2P_BIND_PORT=18444` and make that TCP port publicly reachable from the internet when your router and firewall policy allow it.

## First Deploy Path

Shortest documented operator path:

1. clone the repository
2. copy `.env.example` to `.env`
3. replace the placeholder runtime paths with real machine paths
4. create a miner wallet file if you plan to run `miner`
5. start `node`
6. confirm the node HTTP API responds
7. start `miner` only after the node path is understood
8. add browser wallet or explorer after the node API is stable

Practical order:

```bash
cp .env.example .env
docker compose up --build -d node
curl http://127.0.0.1:8081/v1/status
docker compose up --build -d miner
```

If you also want the browser wallet:

- build and load the extension
- point it at your node HTTP API

If you also want the explorer:

- build the static explorer
- point it at your node HTTP API through build-time or runtime override

Peer misbehavior defaults in `.env.example`:

- `PEER_MISBEHAVIOR_WARNING_THRESHOLD=25`
- `PEER_MISBEHAVIOR_DISCONNECT_THRESHOLD=50`
- `PEER_MISBEHAVIOR_BAN_THRESHOLD=100`
- `PEER_MISBEHAVIOR_BAN_DURATION_SECONDS=1800`
- `PEER_MISBEHAVIOR_DECAY_INTERVAL_SECONDS=300`
- `PEER_MISBEHAVIOR_DECAY_STEP=5`

These control networking policy only. They do not change consensus validity or monetary behavior.

Peer discovery defaults in `.env.example`:

- `PEER_DISCOVERY_ENABLED=true`
- `PEERBOOK_MAX_SIZE=1024`
- `PEER_ADDR_MAX_PER_MESSAGE=250`
- `PEER_ADDR_RELAY_LIMIT_PER_INTERVAL=250`
- `PEER_ADDR_RELAY_INTERVAL_SECONDS=30`
- `PEER_STALE_AFTER_SECONDS=604800`
- `PEER_RETRY_BACKOFF_BASE_SECONDS=1`
- `PEER_RETRY_BACKOFF_MAX_SECONDS=30`
- `PEER_DISCOVERY_STARTUP_PREFER_PERSISTED=true`

After a node has learned the network, the persisted peerbook becomes the primary reconnection source. Manual peers and bootstrap-derived seed peers remain supported, but are treated as fallback startup inputs when healthy persisted peers already exist.

For clean installs, prefer `DIRECT_PEERS` with two or more known-good `host:port` entries when you have them. `DIRECT_PEER` is still supported for compatibility, but a single flaky startup peer can make initial sync unnecessarily fragile.

Service-specific discovery precedence:

- `node` uses `NODE_DIRECT_PEERS`, `NODE_DIRECT_PEER`, and `NODE_BOOTSTRAP_URL` first
- if those are unset, `node` falls back to `DIRECT_PEERS`, `DIRECT_PEER`, and `BOOTSTRAP_URL`
- `miner` uses `MINING_NODE_URLS` for node failover and does not participate in P2P discovery
- enable `BOOTSTRAP_ANNOUNCE_ENABLED=true` only on nodes that are publicly reachable at `NODE_PUBLIC_HOST:NODE_PUBLIC_P2P_PORT`
- keep `BOOTSTRAP_REFRESH_INTERVAL_SECONDS` comfortably below the bootstrap seed TTL

Recommended operator modes:

- `node` + `miner` on the same host/compose
  - leave `NODE_DIRECT_PEERS=chipcoinprotocol.com:18444`
  - leave `MINING_NODE_URLS=http://node:8081`
- miner-only host
  - set `MINING_NODE_URLS=https://api.chipcoinprotocol.com`
- node-only follower host
  - set `NODE_DIRECT_PEERS=chipcoinprotocol.com:18444` or `NODE_BOOTSTRAP_URL=https://bootstrap.chipcoinprotocol.com`
  - leave miner-specific vars unused
- public bootstrap seed contributor
  - set `NODE_BOOTSTRAP_URL=https://bootstrap.chipcoinprotocol.com`
  - set `BOOTSTRAP_ANNOUNCE_ENABLED=true`
  - set `NODE_PUBLIC_HOST` and `NODE_PUBLIC_P2P_PORT` to the real public P2P endpoint

Headers-first sync defaults in `.env.example`:

- `HEADERS_SYNC_ENABLED=true`
- `HEADERS_MAX_PER_MESSAGE=2000`
- `BLOCK_DOWNLOAD_WINDOW_SIZE=128`
- `BLOCK_MAX_INFLIGHT_PER_PEER=16`
- `BLOCK_REQUEST_TIMEOUT_SECONDS=15`
- `HEADERS_SYNC_PARALLEL_PEERS=2`
- `HEADERS_SYNC_START_HEIGHT_GAP_THRESHOLD=1`
- `INITIAL_SYNC_CONSERVATIVE_DEFAULTS=true`
- `BOOTSTRAP_PEER_LIMIT=4`

With these defaults, the node:

1. requests headers from suitable peers
2. tracks the best known header tip separately from the validated chain tip
3. schedules block downloads inside a bounded moving window
4. spreads block requests across multiple healthy peers
5. reassigns stalled block requests after timeout

Full block validation still happens before chain acceptance. Headers-first sync is a download strategy, not a consensus shortcut.

When the local SQLite database is pristine and the runtime has at least one startup peer, the container automatically applies a more conservative initial-sync profile unless you disable `INITIAL_SYNC_CONSERVATIVE_DEFAULTS`. This lowers the initial per-peer block inflight cap and raises the block request timeout to make first syncs less brittle on small devnets.

## Setup Wizard

If you want a guided setup instead of editing `.env` manually, use:

```bash
python3 scripts/setup/wizard.py
```

Available modes:

- `Quick start`
  Uses the public devnet defaults for node endpoint, bootstrap peer, and explorer URL.
- `Custom configuration`
  Prompts for node endpoint, bootstrap peer, and explorer URL, then writes them into `.env`.
- `Local/self-hosted`
  Uses `http://127.0.0.1:8081`, leaves bootstrap empty, and does not depend on public endpoints.

Use the wizard when:

- you want a fast first-run path
- you prefer guided prompts over manual `.env` editing

Use the manual setup flow when:

- you want full control over every runtime path and setting
- you are reviewing the configuration line by line
- you are integrating Chipcoin into an existing local environment

Details:

- `docs/setup-wizard.md`

### Public Node Reachability

Nodes that do not expose `TCP 18444` can still connect outbound and participate in the devnet.

However, outbound-only nodes do not materially improve peer discovery or overall network resilience because other peers cannot reliably dial them back.

For best network health, operators should:

- keep the public devnet P2P listener on `TCP 18444`
- expose and forward `TCP 18444` when possible
- ensure the announced endpoint is publicly reachable from outside the local network

The HTTP/API port (`8081`) and explorer port (`4173`) are optional operator interfaces. They are not required for basic P2P participation.

### Create A Miner Wallet

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
sudo mkdir -p /var/lib/chipcoin/wallets
sudo chown -R "$USER:$USER" /var/lib/chipcoin
chipcoin wallet-generate --wallet-file /var/lib/chipcoin/wallets/chipcoin-wallet.json
chipcoin wallet-address --wallet-file /var/lib/chipcoin/wallets/chipcoin-wallet.json
```

### Start The Stack

Node only:

```bash
docker compose up --build node
```

Miner only:

```bash
docker compose up --build miner
```

Node and miner:

```bash
docker compose up --build node miner
```

Detached mode:

```bash
docker compose up -d --build node miner
```

## Restart And Update

Normal restart:

```bash
docker compose restart node
docker compose restart miner
```

Clean rebuild after pulling changes:

```bash
git pull origin main
docker compose up --build -d node miner
```

Expected after restart:

- node reopens its SQLite state
- peerbook is reused
- sync state is rebuilt from local chain state and live peers
- miner may briefly wait for initial peer sync before resuming work

Use these checks after restart or update:

```bash
docker compose ps
docker compose logs --tail=100 node
docker compose logs --tail=100 miner
curl http://127.0.0.1:8081/v1/status
```

### Inspect Runtime

```bash
docker compose ps
docker compose logs -f node
docker compose logs -f miner
chipcoin --data /path/to/node.sqlite3 status
chipcoin --data /path/to/node.sqlite3 peer-summary
chipcoin --data /path/to/node.sqlite3 list-peers
```

Node HTTP API default:

- `http://127.0.0.1:8081`

Peer diagnostics now expose:

- active backoff state
- misbehavior score
- last penalty reason
- active temporary bans
- peer source (`manual`, `seed`, `discovered`)
- peer state (`manual`, `seed`, `discovered`, `good`, `questionable`, `banned`)

Useful examples:

```bash
chipcoin --network devnet --data /var/lib/chipcoin/data/node-devnet.sqlite3 tip
chipcoin --network devnet mine --node-url http://127.0.0.1:8081 --miner-address CHC...
```

For practical operator diagnostics and recovery steps, use:

- `docs/node.md`

That runbook covers:

- node not syncing
- peerbook empty
- peer banned unexpectedly
- miner waiting for initial sync
- stale peerbook or stale bans
- restart and recovery expectations

Useful peerbook hygiene command:

```bash
docker compose exec node chipcoin --data /runtime/node.sqlite3 peerbook-clean --reset-penalties
```

## Common Recovery Cases

### Isolated Startup

If startup warns that no startup peer was found:

- this is valid if the node already has a populated peerbook
- if the peerbook is empty, add a manual peer or bootstrap URL temporarily

### Stale Peerbook Or Stale Bans

Symptoms:

- no active peers even though the network is healthy
- peers remain banned long after the network recovered

Typical recovery:

1. stop the affected runtime
2. back up the SQLite database
3. clear stale peer ban/backoff rows in the `peers` table
4. restart and let the node relearn live peers

### Node Endpoint Moved

If the node HTTP API URL changes:

- update the browser wallet saved endpoint in `Settings`
- update the explorer API base override or rebuild/runtime config
- update any operator scripts or bookmarks using the old URL

### Explorer Runtime API Base Moved

If the explorer should point to a different node:

- update the `?api=` override, local saved explorer override, runtime `config.js`, or build-time `VITE_NODE_API_BASE_URL`
- rebuild or redeploy the explorer if you changed the static assets

## Local Development Setup

The repository should stay clean and publishable. Real runtime state should live outside the repo.

Recommended layout:

- repository: `/opt/chipcoin` on stable hosts, or `~/src/chipcoin` for local dev
- runtime directory: `/var/lib/chipcoin`
- optional logs: `/var/log/chipcoin`

Setup:

```bash
cp .env.example .env
sudo mkdir -p /var/lib/chipcoin/data
sudo mkdir -p /var/lib/chipcoin/wallets
sudo mkdir -p /var/lib/chipcoin/logs
sudo chown -R "$USER:$USER" /var/lib/chipcoin
```

The default `.env.example` already points at `/var/lib/chipcoin`. If you keep that layout, you do not need to rewrite the runtime paths before running `docker compose up`.

Create a local-only override file when you need machine-specific customization:

```yaml
# docker-compose.override.yml
services:
  node:
    ports:
      - "18444:18444"
      - "8081:8081"

  miner:
    ports:
      - "18445:18445"
```

Notes:

- `docker-compose.override.yml` is ignored by git
- `docker-compose.local.yml` is ignored by git
- keep real `.env`, wallet files, databases, and logs outside the repo
- `docker compose up` automatically applies `docker-compose.override.yml` when it exists
- the base `docker-compose.yml` remains the canonical public configuration

## Browser Wallet

The browser wallet is a separate extension app under `apps/browser-wallet`.

Build:

```bash
cd apps/browser-wallet
npm install
./build-all.sh
```

That produces:

- `apps/browser-wallet/dist-chrome`
- `apps/browser-wallet/dist-firefox`

On first run, the browser wallet uses `BROWSER_WALLET_DEFAULT_NODE_ENDPOINT` from your local `.env` as its initial fallback endpoint.

In `.env.example`, that points to the public devnet node at `https://api.chipcoinprotocol.com`. The user can override it in `Settings`, and the chosen endpoint is persisted afterward.

Detailed instructions:

- `docs/browser-wallet.md`

## First-User Path

The shortest supported path from clone to a working local stack is:

1. Clone the repository and create `.env` from `.env.example`.
2. Create and own the runtime directories referenced by `.env`.
3. Adjust `.env` only if you intentionally want a different runtime root.
4. Generate a miner wallet file at `MINER_WALLET_FILE`.
5. Start the local stack with `docker compose up --build node miner`.
6. Verify the node API with `curl http://127.0.0.1:8081/v1/status`.
7. Build and load the browser wallet from `apps/browser-wallet`.
8. Point the browser wallet to `http://127.0.0.1:8081`.
9. Create or import a wallet in the extension.
10. Send a test transaction and verify it through the node API or your chosen inspection tooling.

## Documentation

- `docs/node.md`
- `docs/miner.md`
- `docs/browser-wallet.md`
- `docs/setup-wizard.md`
- `docs/system-requirements.md`
- `docs/publication-checklist.md`
- `docs/clean-install-checklist.md`
- `docs/protocol.md`

## Known Limitations

- The node logs `node_wallet_runtime=not_used_in_phase_1` because node-side wallet participation is not active yet.
- Miner rewards go to the configured miner wallet file only.
- Seed phrase recovery is not implemented in the browser wallet.
- The repository does not yet define a public mainnet release process.

## Before First Public Push

Do not publish:

- real `.env` files
- wallet JSON files
- private keys
- SQLite runtime databases
- `node_modules`
- browser build output

Use:

- `docs/publication-checklist.md`
- `docs/clean-install-checklist.md`
