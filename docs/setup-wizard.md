# Setup Wizard

## Purpose

The setup wizard is a guided way to create a local `.env`, prepare runtime paths, and initialize the public Chipcoin devnet stack without editing every setting by hand.

Run it from the repository root:

```bash
python3 scripts/setup/wizard.py
```

The wizard writes a local `.env` in the repository root. It does not change the protocol, and it does not modify public defaults in the repository.

## When To Use It

Use the wizard when:

- you want the fastest path from clone to running services
- you want guided prompts for wallet creation or import
- you want `.env` generated with a consistent runtime layout

Prefer the manual setup flow from `README.md` when:

- you want full control over every `.env` value
- you are reviewing all runtime paths explicitly
- you are integrating Chipcoin into an existing operator setup

## Setup Modes

### Quick Start

This mode uses the public devnet defaults:

- node endpoint: `http://tiltmediaconsulting.com:8081`
- bootstrap peer: `tiltmediaconsulting.com:18444`
- explorer URL: `http://tiltmediaconsulting.com:4173`

This is the shortest path if you want to connect quickly to the public devnet environment.

Public devnet endpoints are provided for convenience and may change or become unavailable.

### Custom Configuration

This mode prompts for:

- node endpoint
- bootstrap peer
- explorer URL

Use it when you want guided setup but do not want the public defaults.

### Local/Self-Hosted

This mode writes local-first defaults:

- node endpoint: `http://127.0.0.1:8081`
- bootstrap peer: empty
- explorer URL: empty

Use it when you want a local node/miner stack without depending on public bootstrap or public inspection endpoints.

## Wallet Handling

If you run `miner` or `both`, the wizard also offers:

- `Generate new wallet`
- `Import existing private key`

The wallet file is written to the configured runtime directory, not intended for version control.

If you run `node` only, the wizard skips wallet setup because the current public node runtime does not consume a node wallet file.

## Output

The wizard writes:

- `.env` in the repository root
- runtime data file paths under the configured runtime directory
- miner wallet file under the configured runtime directory when needed

After the wizard completes, the normal next step is:

```bash
docker compose up --build node miner
```
