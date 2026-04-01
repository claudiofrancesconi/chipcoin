# Clean Install Checklist

This checklist is for verifying the public repository from a fresh clone.

## Python Runtime

```bash
git clone <repo-url>
cd Chipcoin-v2
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
mkdir -p /path/to/Chipcoin-runtime/data
mkdir -p /path/to/Chipcoin-runtime/wallets
chipcoin wallet-generate --wallet-file /path/to/Chipcoin-runtime/wallets/chipcoin-wallet.json
```

Checks:

- `.env` exists locally only
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
