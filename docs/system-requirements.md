# System Requirements

## Supported Baseline

The documented public baseline is conservative.

Documented and validated clone-to-run path:

- Linux host on x86_64
- Docker Engine with the `docker compose` plugin
- Python 3.11+
- Node.js 20+
- npm compatible with Node.js 20+

## Browser Wallet

Documented browser targets:

- desktop Chrome
- desktop Firefox

Current Firefox workflow:

- temporary add-on loading through `about:debugging`

Signed Firefox distribution is not part of the current public release.

## Hardware Guidance

Minimum guidance for a local devnet node + miner setup:

- 2 CPU cores
- 4 GB RAM
- 5 GB free disk space

Recommended guidance:

- 4 CPU cores
- 8 GB RAM
- 10 GB free disk space

These numbers are practical operator guidance for the current devnet stack, not formal performance guarantees.

## Important Limits

- macOS is not part of the documented clean-install validation path yet
- Windows is not part of the documented clean-install validation path yet
- ARM platforms are not part of the documented clean-install validation path yet
- public devnet endpoints are convenience defaults and may change or become unavailable

## Runtime Notes

- Docker is the primary documented runtime path for `node` and `miner`
- Python tooling is still required for CLI and wallet helper flows
- Node.js and npm are only required for building the browser wallet, not for running `node` and `miner`

## Network And Firewall Requirements

For basic outbound participation, a node only needs normal outbound internet access.

For public peer reachability and better network resilience, the important port is:

- `TCP 18444` for the devnet P2P listener

Optional ports:

- `TCP 8081` for the HTTP API
- `TCP 4173` for an explorer, if you run one

Opening `TCP 18444` is strongly recommended for operators who want to contribute a publicly reachable peer, but it is not mandatory for every user. Home setups may require both firewall allowance and router port forwarding, and some NAT environments may still prevent reliable inbound reachability.
