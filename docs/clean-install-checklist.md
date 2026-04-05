# Clean Install Checklist

This checklist is for verifying the public repository from a fresh clone.

## Python Runtime

```bash
git clone <repo-url>
cd chipcoin
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

Checks:

- `chipcoin --help`
- `chipcoin-http --help`

## Runtime Config

```bash
cp .env.example .env
sudo mkdir -p /var/lib/chipcoin/data
sudo mkdir -p /var/lib/chipcoin/wallets
sudo mkdir -p /var/lib/chipcoin/logs
sudo chown -R "$USER:$USER" /var/lib/chipcoin
chipcoin wallet-generate --wallet-file /var/lib/chipcoin/wallets/chipcoin-wallet.json
```

Checks:

- `.env` exists locally only
- runtime paths in `.env` point to writable local paths before `docker compose up`
- wallet file is outside version control

## Node And Miner

```bash
docker compose up --build node miner
```

Checks:

- `docker compose ps`
- `docker compose logs -f node`
- `docker compose logs -f miner`
- `curl http://127.0.0.1:8081/v1/status`
- the node and miner containers are using runtime files outside the repository

## Browser Wallet

```bash
cd apps/browser-wallet
npm install
./build-all.sh
```

Checks:

- `dist-chrome` exists
- `dist-firefox` exists
- extension loads in Chrome
- extension loads in Firefox
- wallet can create or import
- wallet can connect to the node API

## End-To-End

Checks:

- wallet shows address and balance
- a transaction can be sent
- the transaction appears through the node API
- the transaction can later be observed by the user in their chosen inspection tooling
- any failure in this section is treated as a documentation or onboarding gap until proven otherwise
