# Miner

## Purpose

The Chipcoin miner is a separate runtime component that:

- builds candidate blocks
- signs coinbase payouts to the configured miner wallet
- submits blocks to the network through standard P2P behavior

## Runtime Inputs

Relevant `.env` keys:

- `CHIPCOIN_RUNTIME_DIR`
- `CHIPCOIN_NETWORK`
- `MINER_DATA_PATH`
- `MINER_LOG_LEVEL`
- `MINER_WALLET_FILE`
- `MINER_P2P_BIND_PORT`
- `MINING_MIN_INTERVAL_SECONDS`
- `MINER_DIRECT_PEERS`
- `MINER_DIRECT_PEER`
- `MINER_BOOTSTRAP_URL`
- `DIRECT_PEERS`
- `DIRECT_PEER`
- `BOOTSTRAP_URL`
- `INITIAL_SYNC_CONSERVATIVE_DEFAULTS`

## Wallet Requirement

The miner requires a wallet JSON file.

Example creation:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
sudo mkdir -p /var/lib/chipcoin/wallets
sudo chown -R "$USER:$USER" /var/lib/chipcoin
chipcoin wallet-generate --wallet-file /var/lib/chipcoin/wallets/chipcoin-wallet.json
```

Show the payout address:

```bash
chipcoin wallet-address --wallet-file /var/lib/chipcoin/wallets/chipcoin-wallet.json
```

## Start

```bash
docker compose up --build miner
```

Detached:

```bash
docker compose up -d --build miner
```

## Logs

```bash
docker compose logs -f miner
```

## Notes

- The miner wallet is operationally used in the current public release.
- Rewards are paid to the address derived from `MINER_WALLET_FILE`.
- Reward redistribution can be done later with standard wallet transactions.
- `MINER_DATA_PATH` must be a writable SQLite file path, not a directory.
- The miner prefers `MINER_DIRECT_PEERS`, `MINER_DIRECT_PEER`, and `MINER_BOOTSTRAP_URL`.
- If miner-specific discovery vars are unset, it falls back to `DIRECT_PEERS`, `DIRECT_PEER`, and `BOOTSTRAP_URL`.
- In the default Docker Compose stack, the miner falls back to `node:18444` so same-host node+miner startup works without editing `.env`.
- For a miner-only host, set `MINER_DIRECT_PEERS=chipcoinprotocol.com:18444` or `MINER_BOOTSTRAP_URL=https://bootstrap.chipcoinprotocol.com`.
- The recommended runtime directory is outside the repo, for example `/var/lib/chipcoin` on a stable Linux host.
