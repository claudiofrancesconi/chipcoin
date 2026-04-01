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
- The default devnet endpoint is `http://127.0.0.1:8081`
- If needed, set a different Node API endpoint in `Settings`
- If the node is remote, set `CHIPCOIN_HTTP_ALLOWED_ORIGINS` on the node to allow the wallet origin

Create or import:
- Fresh install opens onboarding automatically
- Choose `Create wallet` or `Import wallet`
- Imports use the raw private key hex

Export private key:
- Unlock the wallet
- Open `Backup`
- Read the warning, confirm it, and reveal the key only when needed
- Copy is user-triggered only

Recover from private key:
- Install or reload the extension
- Open onboarding
- Choose `Import wallet`
- Paste the private key hex and set a new password

Reset / remove:
- Open `Settings`
- Click `Remove wallet`
- This clears the encrypted wallet, the active session, submitted transaction cache, and the local wallet snapshot

Included in this milestone:
- local wallet creation and import
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
- seed phrase support
- multisig
- multiple accounts
- mainnet support
