# Chipcoin v2

Chipcoin v2 is a Python-first, Bitcoin-inspired blockchain project focused on a small but operational devnet stack.

This public repository is centered on three components:

- `node`
- `miner`
- `browser-wallet`

The current public release target is `devnet`, not mainnet.

Public devnet fallback defaults included in `.env.example`:

- node API: `http://tiltmediaconsulting.com:8081`
- bootstrap peer: `tiltmediaconsulting.com:18444`
- public P2P port: `18444/tcp`
- explorer URL: `http://tiltmediaconsulting.com:4173`

These are fallback defaults only. They are not required and can be replaced with your own node, peer, and inspection tooling.
Public devnet endpoints are provided for convenience and may change or become unavailable.

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
- explorer and bootstrap service deployment are outside the primary public onboarding path for this repository

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
- `MINER_DATA_PATH`
- `MINER_WALLET_FILE`

For the shortest first run, you can either keep the public devnet defaults from `.env.example` or replace them with your own values.

If you want a fully local first run, set:

- `DIRECT_PEER=`
- `BOOTSTRAP_URL=`
- `BROWSER_WALLET_DEFAULT_NODE_ENDPOINT=http://127.0.0.1:8081`

This starts an isolated local node/miner pair and avoids any external bootstrap dependency in the first-user path.

If you want your node to improve peer discovery and network resilience, keep `NODE_P2P_BIND_PORT=18444` and make that TCP port publicly reachable from the internet when your router and firewall policy allow it.

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

- repository: `~/src/chipcoin`
- runtime directory: `/home/komarek/Chipcoin-runtime`

Setup:

```bash
cp .env.example .env
mkdir -p /home/komarek/Chipcoin-runtime/data
mkdir -p /home/komarek/Chipcoin-runtime/wallets
mkdir -p /home/komarek/Chipcoin-runtime/logs
```

Then edit `.env` and replace every `/path/to/Chipcoin-runtime/...` placeholder with your real runtime directory before running `docker compose up`.

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

In `.env.example`, that points to the public devnet node on Contabo. The user can override it in `Settings`, and the chosen endpoint is persisted afterward.

Detailed instructions:

- `docs/browser-wallet.md`

## First-User Path

The shortest supported path from clone to a working local stack is:

1. Clone the repository and create `.env` from `.env.example`.
2. Edit `.env` and replace all `/path/to/Chipcoin-runtime/...` placeholders with real local paths.
3. Create the runtime directories referenced by `.env`.
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
