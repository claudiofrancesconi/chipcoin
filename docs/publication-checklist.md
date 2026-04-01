# Publication Checklist

Use this checklist before the first public push.

## Secrets And Runtime Data

- remove any real `.env` files from the commit set
- remove any wallet JSON files from the commit set
- remove any private keys from notes, scripts, and docs
- remove any SQLite runtime databases from the commit set
- remove any machine-specific hostnames from public docs unless they are intentional public infrastructure

## Build Artifacts

- exclude `node_modules`
- exclude browser wallet `dist-chrome`
- exclude browser wallet `dist-firefox`
- exclude any explorer build output
- exclude Python `egg-info`

## Public Docs

- root `README.md` reflects only public scope
- node docs are accurate
- miner docs are accurate
- browser wallet docs are accurate
- known limitations are stated honestly

## Public Scope Boundary

If the first GitHub publication is intentionally limited to `node`, `miner`, and `browser-wallet`, exclude operator-only areas from that initial push:

- `apps/explorer/`
- `services/bootstrap-seed/`
- `docs/bootstrap-service.md`
- `docs/remote-devnet.md`
- `scripts/remote-devnet/`

## Config

- `.env.example` exists at repository root
- `config/env/.env.example` matches the public runtime path
- no real values remain in example files

## Final Sanity Check

- search for private keys, wallet files, and personal paths
- verify the default browser wallet endpoint is not a personal host assumption
- confirm the public onboarding path does not depend on private local context
