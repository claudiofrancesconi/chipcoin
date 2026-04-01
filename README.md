# Chipcoin v2

Chipcoin v2 is a Python-first, Bitcoin-inspired blockchain project focused on a small but operational devnet stack.

This public repository is centered on three components:

- `node`
- `miner`
- `browser-wallet`

The current public release target is `devnet`, not mainnet.

## Current Status

What works today:

- SQLite-backed node runtime
- P2P block and transaction relay
- HTTP API for status, blocks, transactions, peers, and address data
- external miner process using a wallet file
- browser wallet for Chrome and Firefox
- Docker-based node and miner startup

What is intentionally limited today:

- `devnet` is the only supported public network for this release
- the node runtime does not use a wallet file yet
- browser wallet import uses raw private key hex, not seed phrases
- no multisig, no multiple accounts, no hardware wallet support
- bootstrap and explorer are not part of the publication scope of this repo entry point

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

## Quick Start

### Prerequisites

- Python 3.11+
- Docker Engine with `docker compose`
- Node.js 20+ and npm if you want to build the browser wallet

### Clone And Prepare Runtime Config

```bash
git clone <repo-url>
cd Chipcoin-v2
cp .env.example .env
```

Edit `.env` for your machine. At minimum, review:

- `CHIPCOIN_RUNTIME_DIR`
- `NODE_DATA_PATH`
- `MINER_DATA_PATH`
- `MINER_WALLET_FILE`
- `DIRECT_PEER`
- `BOOTSTRAP_URL`

### Create A Miner Wallet

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
mkdir -p /path/to/Chipcoin-runtime/wallets
chipcoin wallet-generate --wallet-file /path/to/Chipcoin-runtime/wallets/chipcoin-wallet.json
chipcoin wallet-address --wallet-file /path/to/Chipcoin-runtime/wallets/chipcoin-wallet.json
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

### Inspect Runtime

```bash
docker compose ps
docker compose logs -f node
docker compose logs -f miner
```

Node HTTP API default:

- `http://127.0.0.1:8081`

Useful examples:

```bash
chipcoin --network devnet --data /path/to/Chipcoin-runtime/data/node-devnet.sqlite3 tip
chipcoin --network devnet --data /path/to/Chipcoin-runtime/data/miner-devnet.sqlite3 tip
```

## Local Development Setup

The repository should stay clean and publishable. Real runtime state should live outside the repo.

Recommended layout:

- repository: `~/src/Chipcoin-v2`
- runtime directory: `/home/komarek/Chipcoin-runtime`

Setup:

```bash
cp .env.example .env
mkdir -p /home/komarek/Chipcoin-runtime/data
mkdir -p /home/komarek/Chipcoin-runtime/wallets
mkdir -p /home/komarek/Chipcoin-runtime/logs
```

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

Load the unpacked extension and point it to your node API from `Settings`.

Detailed instructions:

- `docs/browser-wallet.md`

## Documentation

- `docs/node.md`
- `docs/miner.md`
- `docs/browser-wallet.md`
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
