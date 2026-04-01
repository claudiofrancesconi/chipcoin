# Node

## Purpose

The Chipcoin node maintains chain state, validates blocks and transactions, exposes the HTTP API, and participates in the P2P network.

The current public release does not use a node wallet file at runtime.

## Runtime Inputs

Relevant `.env` keys:

- `CHIPCOIN_RUNTIME_DIR`
- `CHIPCOIN_NETWORK`
- `NODE_DATA_PATH`
- `NODE_LOG_LEVEL`
- `NODE_P2P_BIND_PORT`
- `NODE_HTTP_BIND_PORT`
- `CHIPCOIN_HTTP_ALLOWED_ORIGINS`
- `DIRECT_PEER`
- `BOOTSTRAP_URL`

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

## HTTP API

Default local URL:

- `http://127.0.0.1:8081`

Useful endpoints:

- `GET /v1/status`
- `GET /v1/blocks`
- `GET /v1/block?height=<height>`
- `GET /v1/block?hash=<hash>`
- `GET /v1/tx/<txid>`
- `GET /v1/address/<address>`
- `GET /v1/address/<address>/utxos`
- `GET /v1/address/<address>/history`
- `GET /v1/mempool`
- `GET /v1/peers/summary`

## Notes

- `DIRECT_PEER` can be used for explicit peering.
- Leave both `DIRECT_PEER` and `BOOTSTRAP_URL` empty for an isolated node.
- Public browser wallet access may require `CHIPCOIN_HTTP_ALLOWED_ORIGINS` to include the wallet origin.
- The recommended runtime directory is outside the repo, for example `/home/komarek/Chipcoin-runtime`.
