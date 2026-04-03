# Browser Wallet

## Purpose

The Chipcoin browser wallet is a minimal Chrome and Firefox extension for `devnet`.

It currently supports:

- wallet creation
- wallet recovery from a saved recovery phrase
- private key import
- encrypted local persistence
- address display
- balance and history loading from the node HTTP API
- local transaction build, sign, and broadcast

## Prerequisites

- Node.js 20+
- npm

## Install Dependencies

```bash
cd apps/browser-wallet
npm install
```

## Build

Chrome:

```bash
npm run build:chrome
```

Firefox:

```bash
npm run build:firefox
```

Both:

```bash
./build-all.sh
```

Outputs:

- `dist-chrome`
- `dist-firefox`

## Load In Browser

Chrome:

1. Open `chrome://extensions`
2. Enable Developer mode
3. Click `Load unpacked`
4. Select `apps/browser-wallet/dist-chrome`

Firefox:

1. Open `about:debugging#/runtime/this-firefox`
2. Click `Load Temporary Add-on...`
3. Select `apps/browser-wallet/dist-firefox/manifest.json`

## First Use

Open the popup and choose one of:

- `Create`
- `Recover`
- `Import key`

Behavior:

- `Create` generates a local Chipcoin recovery phrase, asks you to confirm backup, then encrypts the wallet in extension storage
- `Recover` restores the same wallet deterministically from that recovery phrase
- `Import key` remains available as a fallback for advanced users using raw private key hex

## Connect To A Node

The browser wallet uses a fallback default endpoint from the repository `.env` at build time:

- `BROWSER_WALLET_DEFAULT_NODE_ENDPOINT`

In `.env.example`, that fallback is set to the public devnet node:

- `https://api.chipcoinprotocol.com`

Public devnet endpoints are provided for convenience and may change or become unavailable.

To use a different node:

1. Open the wallet popup
2. Go to `Settings`
3. Change the Node API URL
4. Save

Behavior:

- the fallback default is used on first run only
- the user's chosen endpoint is persisted afterward
- manual override in the UI remains available at any time
- the wallet verifies both `/v1/health` and `/v1/status` before saving a new endpoint
- the wallet rejects endpoints on the wrong network
- the Overview and Settings screens now show an explicit connection state and message for the saved endpoint

If the node is remote, allow the wallet origin through `CHIPCOIN_HTTP_ALLOWED_ORIGINS`.

## Endpoint Failure Modes

The wallet now distinguishes these common cases more explicitly:

- invalid endpoint
  - the URL is missing or malformed
- unreachable endpoint
  - the node is offline, the host/port is wrong, or the request timed out
- browser-blocked endpoint
  - the browser may block the request because of CORS, HTTPS, or mixed-content rules
- wrong network
  - the endpoint answered, but not on the expected network
- stale saved endpoint
  - the wallet keeps the saved endpoint, but Overview and Settings show that it is currently unreachable

The browser cannot always distinguish a pure network outage from a CORS or mixed-content block. In those cases the wallet says so directly instead of pretending the error is more specific than it really is.

## HTTP / HTTPS / CORS Reality

Practical rules:

- use a full `http://` or `https://` URL
- if the wallet is loaded on an `https://` extension/page context and the node is only reachable over insecure HTTP in a way the browser treats as mixed content, the request may be blocked
- if the node is remote, `CHIPCOIN_HTTP_ALLOWED_ORIGINS` must allow the browser wallet origin
- if the node returns non-JSON or an unexpected proxy/login page, the wallet now reports that as an invalid node API response

When the endpoint is saved but unreachable, the wallet does not silently invent a healthy connection. It shows the last known endpoint and marks the node connection as unavailable until refresh succeeds again.

## When The Node Endpoint Moves

If you move the node API to a different host or port:

1. open the wallet popup
2. go to `Settings`
3. replace the saved Node API URL
4. save and confirm the wallet reports a connected node state again

If the old endpoint is still saved, the wallet stays usable locally but address/balance/history refresh will reflect the saved endpoint until you update it.

Stable API endpoints currently relied on by the wallet:

- `GET /v1/health`
- `GET /v1/status`
- `GET /v1/tip`
- `GET /v1/address/<address>`
- `GET /v1/address/<address>/utxos`
- `GET /v1/address/<address>/history`
- `GET /v1/tx/<txid>`
- `POST /v1/tx/submit`

The wallet expects JSON errors in the form:

- `{"error": {"code": "<stable_code>", "message": "<human_message>"}}`

## Storage Model

The wallet stores secrets only in browser extension local storage.

High-level model:

- the secret payload is encrypted locally with the user password
- seed-based wallets store the encrypted recovery phrase and derive account `0` deterministically
- private-key imports store the encrypted private key directly
- no remote backup or cloud sync is implemented

The current recovery phrase format is Chipcoin-specific and is not documented as BIP39-compatible.

## Backup And Recovery

Recommended flow:

1. create a wallet
2. write down the recovery phrase
3. confirm it before continuing
4. keep the password and recovery phrase separate

Recovery flow:

1. reinstall or reload the extension
2. choose `Recover`
3. paste the saved recovery phrase
4. set a new local password

Fallback flow:

1. choose `Import key`
2. paste the raw private key hex
3. set a new local password

## Known Limits

- the current recovery phrase is not BIP39-compatible
- single-account flow only in this phase
- no multisig
- no multiple accounts UI yet
- no mainnet target in this public release
