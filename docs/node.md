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

- `DIRECT_PEER` can be used for explicit peering.
- Leave both `DIRECT_PEER` and `BOOTSTRAP_URL` empty for an isolated node.
- Public browser wallet access may require `CHIPCOIN_HTTP_ALLOWED_ORIGINS` to include the wallet origin.
- The recommended runtime directory is outside the repo, for example `/home/komarek/Chipcoin-runtime`.
