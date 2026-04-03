# Chipcoin Browser Wallet

Target network:
- `devnet`

Supported browsers:
- Chrome
- Firefox

Build commands:
- `npm run build:chrome`
- `npm run build:firefox`

Install:
- Chrome: build with `npm run build:chrome`, open `chrome://extensions`, enable Developer mode, then `Load unpacked` and select `apps/browser-wallet/dist/`
- Firefox: build with `npm run build:firefox`, open `about:debugging#/runtime/this-firefox`, click `Load Temporary Add-on...`, then select `apps/browser-wallet/dist/manifest.json`

Connect to a node:
- Open `Settings`
- The first-run fallback default comes from `BROWSER_WALLET_DEFAULT_NODE_ENDPOINT` in the repo `.env`
- In `.env.example`, the public devnet fallback is `https://api.chipcoinprotocol.com`
- If needed, set a different Node API endpoint in `Settings`
- After first run, the selected endpoint is persisted in extension storage
- If the node is remote, set `CHIPCOIN_HTTP_ALLOWED_ORIGINS` on the node to allow the wallet origin
- The wallet verifies `/v1/health` and `/v1/status` before saving a new endpoint
- The wallet rejects endpoints on the wrong network
- Overview and Settings show an explicit node-connection state for the currently saved endpoint

Common endpoint failures:
- invalid URL: the value is missing or malformed
- unreachable endpoint: the node is offline, the host/port is wrong, or the request timed out
- browser-blocked endpoint: CORS, HTTPS, or mixed-content rules may prevent the request
- stale saved endpoint: the endpoint stays saved, but the wallet reports it as unavailable until it responds again

Create, recover, or import:
- Fresh install opens onboarding automatically
- Choose `Create new wallet`, `Recover wallet`, or `Import private key`
- `Create new wallet` generates a local recovery phrase, requires you to acknowledge backup, then encrypts the wallet in extension storage
- `Recover wallet` recreates the same wallet deterministically from the saved recovery phrase
- `Import private key` remains available as a fallback path for advanced users

Export private key:
- Unlock the wallet
- Open `Backup`
- Read the warning, confirm it, and reveal the key only when needed
- Copy is user-triggered only

Export recovery phrase:
- Seed-based wallets can reveal the recovery phrase from `Backup`
- The phrase is shown only after explicit confirmation
- Private-key-imported wallets do not have a recovery phrase to export

Recover from seed phrase:
- Install or reload the extension
- Open onboarding
- Choose `Recover wallet`
- Paste the saved recovery phrase and set a new password

Recover from private key fallback:
- Install or reload the extension
- Open onboarding
- Choose `Import private key`
- Paste the private key hex and set a new password

Reset / remove:
- Open `Settings`
- Click `Remove wallet`
- This clears the encrypted wallet, the active session, submitted transaction cache, and the local wallet snapshot

Included in this milestone:
- local wallet creation and import
- seed-based wallet creation and deterministic recovery
- encrypted wallet storage
- background-owned unlock session
- Phase 2 API client wiring
- read-only address, balance, history, and node-health flows
- local transaction build, sign, serialize, and submit aligned with the current Chipcoin wallet primitives
- submitted transaction tracking and confirmation polling

Manual smoke test:
1. Build and load the extension in Chrome or Firefox
2. Create a wallet or import an existing private key
3. Confirm the wallet shows:
   - address
   - connected network `devnet`
   - balance data from your configured node API
4. Submit a small transaction to a known Chipcoin address
5. Check the returned txid through your node API or explorer tooling
6. Confirm the transaction later appears in confirmed history

Not included:
- multisig
- multiple accounts
- mainnet support

Storage model:
- wallet secrets stay in browser extension local storage only
- the stored secret is encrypted with the user password
- seed-based wallets store the encrypted recovery phrase and derive account `0` deterministically
- private-key-imported wallets store the encrypted private key directly

Current limitation:
- the recovery phrase format is Chipcoin-specific for now and is not advertised as BIP39-compatible
