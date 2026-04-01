# Architecture

## Overview

Chipcoin v2 is organized into layers with strict responsibility boundaries.

1. `consensus`
   Pure protocol rules. No networking, no HTTP, no persistence details.
2. `crypto`
   secp256k1 key handling, signatures, and address derivation.
3. `storage`
   Durable local persistence for headers, blocks, UTXO state, node registry, peers, and mempool.
4. `node`
   Runtime orchestration around mempool, peerbook, sync, mining, and protocol sessions.
5. `wallet`
   Minimal boundary for transaction construction support and signing responsibilities.
6. `interfaces`
   CLI and optional adapters kept outside the core.
7. `bootstrap-seed`
   Separate optional service for peer discovery only.

## Core Design Rules

- Consensus logic must remain deterministic and side-effect free where possible.
- Peer-to-peer communication must not depend on HTTP.
- Storage must not rely on JSON files as the primary source of truth.
- The wallet boundary must keep signing outside node internals.
- The bootstrap service must not participate in consensus.
- Node reward winners must be derivable from chain state only, never from runtime peer connectivity.
- Monetary values must be stored and validated only as integer `Chipbits`.

## Network Profiles

The codebase keeps network-specific runtime and consensus profiles centralized instead of scattering test hacks through the runtime.

- `mainnet` remains the default profile and keeps the canonical didactic consensus parameters.
- `devnet` is a separate local-development profile with its own SQLite default path, lower coinbase maturity, faster target block time, and a shorter retarget window.

CLI and Docker choose the active profile with `--network`, while storage stays network-scoped so peer metadata and chain state do not bleed across profiles.

## Priority Modules

The first implementation priority is:

1. `consensus`
2. `serialization`
3. `crypto`
4. `storage`
5. `node` internals
6. `p2p` protocol interfaces

## Planned Runtime Flow

1. Load local chain metadata and chainstate from storage.
2. Load the on-chain node registry used for deterministic node rewards.
3. Initialize peerbook from local records and optional bootstrap seeds.
4. Open P2P sessions and exchange handshake messages.
5. Synchronize headers first, then request missing blocks.
6. Validate new transactions and blocks through consensus.
7. Update storage and advertise inventory to peers.

## Consensus State

The validated local state is split into two consensus-tracked views:

- UTXO set
- node registry

The UTXO set validates ordinary spends.

Ordinary spend validation includes:

- deterministic signing-payload reconstruction
- secp256k1 ECDSA signature verification
- address/public-key ownership matching against the referenced UTXO

The node registry validates:

- `register_node`
- `renew_node`
- active node sets per epoch
- deterministic node reward winners

Block validation stages both views in memory before persistence. Coinbase validation uses the pre-block registry snapshot so node registrations become eligible only from the next block.

## Wallet Boundary

The wallet layer is intentionally thin and external to node consensus state:

- key generation and private-key storage live in `wallet` and `crypto`
- transaction assembly performs coin selection and per-input signing outside the node
- the node never needs wallet private keys to validate, relay, mine, or confirm transactions

The expected end-to-end flow is:

1. wallet derives a CHC address from a secp256k1 public key
2. wallet selects spendable UTXOs and builds a transaction in integer `Chipbits`
3. wallet signs each input against the canonical consensus payload
4. node validates signatures and ownership at mempool admission
5. miners include the validated transaction in a candidate block
6. the same consensus checks run again during block validation

## Mempool Policy Boundary

The mempool layer applies local node policy on top of consensus validity.

Consensus code decides whether a transaction is valid to include in a block.

Mempool policy decides whether the node wants to keep and relay that transaction before confirmation.

Current mempool policy is intentionally simple:

- minimum fee floor for ordinary transactions
- no duplicates
- no conflicting spends against already-admitted mempool entries
- bounded transaction size and basic input/output-count limits
- TTL expiry for stale entries
- simple capacity eviction preferring higher-fee and newer entries

When chainstate changes after a new block or a simple reorg, the node reconciles mempool contents against the new active chain and can re-add transactions disconnected from a lost branch if they are still valid and standard.

## Miner Assembly

The miner does not reimplement consensus. It assembles a candidate block from:

- current active-chain tip
- current mempool view
- deterministic node reward rules

Current assembly rules are didactic but stricter than FIFO:

- transactions are ranked by fee-rate
- weight is serialized transaction byte length
- ancestors must appear before descendants
- transactions are skipped once the remaining block-weight budget is exhausted
- the resulting block is still validated through consensus before activation

## Bootstrap Service Boundary

The bootstrap service may:

- accept node announces
- return peer lists for a given network
- expire stale records

The bootstrap service may not:

- decide valid chain state
- relay consensus decisions
- act as authoritative blockchain storage
