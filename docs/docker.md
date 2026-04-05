# Docker Runtime

This repository uses Docker Compose as the public runtime path for:

- `node`
- `miner`

The browser wallet is built separately and connects to the node HTTP API.

## Public Compose Files

- `docker-compose.yml`
  The canonical public configuration. It is environment-variable driven and contains no machine-specific paths.

- `docker-compose.override.yml`
  Optional local-only customization file. It is ignored by git and is applied automatically by `docker compose` when present.

## Runtime Layout

The recommended runtime layout keeps mutable state outside the repository.

Example:

- `/var/lib/chipcoin/data/node-devnet.sqlite3`
- `/var/lib/chipcoin/data/miner-devnet.sqlite3`
- `/var/lib/chipcoin/wallets/chipcoin-wallet.json`
- `/var/lib/chipcoin/logs/node/`
- `/var/lib/chipcoin/logs/miner/`

Relevant `.env` keys:

- `CHIPCOIN_RUNTIME_DIR`
- `NODE_DATA_PATH`
- `MINER_DATA_PATH`
- `MINER_WALLET_FILE`

## Start

Node and miner:

```bash
docker compose up --build node miner
```

Detached:

```bash
docker compose up -d --build node miner
```

Node only:

```bash
docker compose up --build node
```

Miner only:

```bash
docker compose up --build miner
```

## Stop

```bash
docker compose down
```

## Inspect

```bash
docker compose ps
docker compose logs -f node
docker compose logs -f miner
```

## Notes

- The node runtime currently does not use a wallet file.
- The miner runtime does use `MINER_WALLET_FILE`.
- The default node HTTP API is typically exposed on `http://127.0.0.1:8081`.
- Local override files are for ports, extra bind mounts, and machine-specific behavior only. Keep secrets and runtime state out of the repository.
