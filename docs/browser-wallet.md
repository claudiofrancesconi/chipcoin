# Browser Wallet

## Purpose

The Chipcoin browser wallet is a minimal Chrome and Firefox extension for `devnet`.

It currently supports:

- wallet creation
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
- `Import`

Import currently uses raw private key hex.

## Connect To A Node

The default wallet endpoint is:

- `http://127.0.0.1:8081`

To use a different node:

1. Open the wallet popup
2. Go to `Settings`
3. Change the Node API URL
4. Save

If the node is remote, allow the wallet origin through `CHIPCOIN_HTTP_ALLOWED_ORIGINS`.

## Known Limits

- no seed phrase support
- no multisig
- no multiple accounts
- no mainnet target in this public release
