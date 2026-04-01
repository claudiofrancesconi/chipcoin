# Roadmap

## Phase 0: Skeleton

- create package layout
- define module boundaries
- write design documentation
- add placeholder tests

## Phase 1: Consensus Foundations

- deterministic serialization
- hashing helpers
- merkle root
- block and transaction models
- network and economic parameters

## Phase 2: Validation Core

- transaction validation
- block validation
- UTXO application and rollback
- subsidy and fee accounting
- PoW verification

## Phase 3: Storage

- SQLite schema
- repositories for headers, blocks, chainstate, peers, mempool
- startup initialization flow

## Phase 4: Node Internals

- node service orchestration
- peerbook management
- mempool manager
- block processing pipeline
- mining template assembly

## Phase 5: P2P Protocol

- transport abstraction
- framing codec
- protocol messages
- session lifecycle
- header-first sync

## Phase 6: Wallet Boundary

- signing payload definitions
- input selection
- wallet-side signing support

## Phase 7: Bootstrap Service

- implement separate discovery service
- containerize it for deployment
- connect node discovery client

## Phase 8: Hardening

- consensus fixture tests
- reorg tests
- storage recovery tests
- protocol parsing tests
