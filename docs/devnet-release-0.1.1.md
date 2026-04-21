# Devnet Release 0.1.1

## Scope

This release includes:

- corrected native reward warmup and activation boundary semantics
- consolidated reward diagnostics in CLI:
  - `reward-node-status`
  - `reward-epoch-summary`
- equivalent consolidated reward diagnostics over HTTP:
  - `GET /v1/rewards/node-status`
  - `GET /v1/rewards/epoch-summary`

## Release Classification

- consensus-affecting:
  - yes
  - `src/chipcoin/consensus/nodes.py` changes reward-node active-set selection semantics
- runtime and diagnostics:
  - yes
  - new CLI and HTTP diagnostics are additive
- reward economics:
  - unchanged
- ranking logic:
  - unchanged
- payout math:
  - unchanged
- settlement logic:
  - unchanged

## Upgrade Impact

- existing node databases remain readable
- no destructive database migration is required
- schema handling remains additive through startup-time column guards in [`src/chipcoin/storage/db.py`](/home/komarek/Documents/CODEX/Chipcoin-v2/src/chipcoin/storage/db.py)
- in-place upgrade is supported for existing devnet nodes
- chain reset is optional, not required for upgrade

## Snapshot Impact

- older snapshots still import
- fresh snapshots are recommended for this release because snapshot export now carries block payloads in addition to headers and chainstate
- recommendation:
  - upgrade first
  - verify the upgraded canonical node
  - export and publish a fresh devnet snapshot after that verification

## Operator Impact

Primary post-upgrade checks:

- `chipcoin ... reward-node-status --node-id <id>`
- `chipcoin ... reward-epoch-summary --epoch-index <N>`
- `GET /v1/rewards/node-status?node_id=<id>`
- `GET /v1/rewards/epoch-summary?epoch_index=<N>`
- `GET /v1/status`

## Decision Notes

- in-place upgrade: recommended
- reset: optional, use only for clean-room rehearsal or known-bad local state
- fresh snapshot: recommended after upgrade, not before
- release push: recommended only after one more operator rehearsal covering:
  - one in-place upgrade on the real hosts
  - one fresh-node bootstrap from zero
