# Chipcoin-v2 Monetary Policy

## Scope

This document defines the locked `devnet` monetary policy baseline for `Chipcoin-v2`.
It replaces the previous emission assumptions for the current implementation branch.
Chain reset is allowed. Backward compatibility with the old subsidy rules is not required.

## Units

- Base unit: `1 CHC = 100,000,000` base units
- Symbol used in code and APIs today: `chipbits`

All consensus calculations must be performed in integer base units only.

## Locked Devnet Policy

- Network: `devnet`
- Maximum supply: `11,000,000 CHC`
- Target block time: `300 seconds`
- Initial miner subsidy: `50 CHC` per block
- Initial node reward pool: `50 CHC` every `100` blocks
- Halving interval: `111,000` blocks
- Miner subsidy and node epoch reward halve together
- No treasury
- Node rewards are part of capped issuance, not extra inflation

## Reward Node Fee Policy

Reward-node registration and renewal fees are no longer fixed forever in CHC terms.

Consensus now derives them deterministically from the on-chain reward-node registry size:

- Driver: `registered_reward_node_count`
- Curve shape: logarithmic, monotonic, integer-only
- Target saturation point: `20,000` registered reward nodes
- Registration fee range: `1 CHC` down to `0.0001 CHC`
- Renewal fee range: `0.1 CHC` down to `0.00001 CHC`

In base units:

- `max_register_fee_chipbits = 100_000_000`
- `min_register_fee_chipbits = 10_000`
- `max_renew_fee_chipbits = 10_000_000`
- `min_renew_fee_chipbits = 1_000`

Policy intent:

- keep Sybil resistance meaningful when the registry is small
- reduce fiat-denominated entry cost as the network grows
- avoid using noisy peer-connectivity data in consensus

Consensus source of truth:

- on-chain reward-node registry count, not observed connected peers
- fee evaluation is anchored to parent-tip registry state for deterministic block validation

Operational guidance:

- explorer, website, CLI and node HTTP surfaces should display the current live fee
- the easiest canonical read surfaces are:
  - `chipcoin reward-node-fees`
  - `GET /v1/rewards/node-fees`
  - `GET /v1/status` under `reward_node_fees`

In base units:

- `max_supply = 1_100_000_000_000_000`
- `initial_miner_subsidy = 5_000_000_000`
- `initial_node_epoch_reward = 5_000_000_000`
- `epoch_length = 100`

## Issuance Shape

### Miner issuance

For height `h`, let:

- `era(h) = floor(h / halving_interval)`
- `miner_subsidy_per_block(h) = floor(initial_miner_subsidy / 2^era(h))`

After the cap clamp is applied, actual per-block miner issuance may be smaller than the scheduled value.

### Node issuance

Node rewards are epoch-based, not per-block.

For epoch index `e`, let:

- `epoch_start_height = e * 100`
- `node_reward_for_epoch(e) = floor(initial_node_epoch_reward / 2^era(epoch_start_height))`

The node epoch reward is scheduled once per epoch, not once per block.

Node reward issuance is still part of total capped monetary issuance even when the payout mechanism remains observer-driven and off-chain in early phases.

## Hard Cap Rule

The chain must never mint more than `11,000,000 CHC`.

Consensus rule:

1. Compute scheduled issuance for the next event:
   - miner subsidy for the next block
   - node epoch reward if the next block closes an epoch reward boundary
2. Compute remaining supply:
   - `remaining_supply = max_supply - minted_supply`
3. If scheduled issuance is less than or equal to remaining supply:
   - mint scheduled issuance normally
4. If scheduled issuance would exceed remaining supply:
   - mint only the remaining supply
   - deterministic clamp order must be defined in consensus code
5. Once `minted_supply == max_supply`:
   - all further subsidy is zero
   - miner subsidy is zero
   - node epoch reward is zero

## Clamp Order

The implementation must be deterministic when remaining supply is insufficient.

Recommended order:

1. Compute scheduled miner block subsidy
2. Compute scheduled node epoch reward only when the block closes an epoch boundary
3. Apply cap clamp to total scheduled issuance
4. Allocate the clamped amount in this order:
   - miner subsidy first
   - node epoch reward second

Rationale:

- simplest deterministic rule
- keeps block validity independent from observer-based node eligibility during early phases
- avoids minting node-side issuance when not enough supply remains for the block event

If future policy wants a different priority rule, it must be explicit and consensus-tested. The initial implementation should not introduce a more complex split.

## Unminted Node Reward Rule

When an epoch closes and there are zero eligible nodes for that epoch, or when
no candidate passes the reward policy:

- the scheduled node epoch reward for that epoch remains unminted
- `minted_supply` does not increase by that amount
- `remaining_supply` stays higher by that amount
- `undistributed_node_reward_supply` increases by that amount for diagnostics
- no carry-forward is created in consensus state

This rule keeps issuance deterministic and auditable while the eligibility system is still experimental.

## Supply Counters

The implementation must expose deterministic, reorg-safe counters.

Required counters:

- `max_supply`
- `scheduled_supply`
- `scheduled_miner_supply`
- `scheduled_node_reward_supply`
- `scheduled_remaining_supply`
- `materialized_supply`
- `materialized_miner_supply`
- `materialized_node_reward_supply`
- `undistributed_node_reward_supply`
- `minted_supply`
- `miner_minted_supply`
- `node_minted_supply`
- `burned_supply`
- `immature_supply`
- `circulating_supply`
- `remaining_supply`

Formula:

- `scheduled_supply = scheduled_miner_supply + scheduled_node_reward_supply`
- `materialized_supply = materialized_miner_supply + materialized_node_reward_supply`
- `undistributed_node_reward_supply = scheduled_node_reward_supply - materialized_node_reward_supply`
- `circulating_supply = minted_supply - burned_supply - immature_supply`

Derived expectations:

- `minted_supply` is an explorer-facing alias for `materialized_supply`
- `minted_supply = miner_minted_supply + node_minted_supply`
- `remaining_supply = max_supply - minted_supply`
- all values are clamped to integer base units

The `scheduled_*` counters describe the theoretical protocol budget through the
active tip. They answer "how much subsidy was available by this height?".

The `materialized_*` counters describe actual coinbase issuance on the active
chain. They answer "how much CHC actually exists as miner subsidy and node reward
outputs?". Public explorer supply should use `materialized_supply` and
`circulating_supply`, not scheduled supply.

`undistributed_node_reward_supply` is the scheduled node-reward budget that did
not materialize into coinbase outputs because no node qualified, no settlement
distributed it, or an epoch settled with zero reward entries. It is an accounting
diagnostic, not a balance and not a carry-forward pool.

Node rewards are not extra inflation. Miner subsidy and node reward pool are
both parts of the same capped subsidy schedule. If a node reward is paid, it
materializes inside the block coinbase and increases `materialized_supply`. If it
is not paid, it remains unminted and only increases
`undistributed_node_reward_supply`.

`immature_supply` is materialized coinbase supply that exists on-chain but is not
yet spendable under coinbase maturity. `circulating_supply` is therefore:

- `circulating_supply = materialized_supply - burned_supply - immature_supply`

Every supply surface should include the active `height` and `tip_hash` so CLI,
HTTP API and explorer checks can compare values from the same chain tip.

## Reorg Safety

Supply counters must be derivable from active-chain state only.

The implementation must not rely on monotonic append-only counters that cannot be rolled back.

Valid implementation options:

- recompute counters from active-chain blocks and active UTXO state
- maintain indexed per-block accounting that is rolled back during reorg

For the first implementation, correctness is more important than optimization.

## API Requirements

### `GET /v1/supply`

Must return:

- `network`
- `height`
- `tip_hash`
- `max_supply`
- `scheduled_supply`
- `scheduled_miner_supply`
- `scheduled_node_reward_supply`
- `scheduled_remaining_supply`
- `materialized_supply`
- `materialized_miner_supply`
- `materialized_node_reward_supply`
- `undistributed_node_reward_supply`
- `minted_supply`
- `miner_minted_supply`
- `node_minted_supply`
- `circulating_supply`
- `remaining_supply`

All values should be returned in base units, with optional CHC-formatted mirrors if the existing API style already does that cleanly.

### `GET /v1/status`

Must add a reduced summary:

- `max_supply`
- `height`
- `tip_hash`
- `materialized_supply`
- `undistributed_node_reward_supply`
- `minted_supply`
- `immature_supply`
- `circulating_supply`
- `remaining_supply`

This should be a compact operator-facing view, not a duplicate of the full `/v1/supply` payload.

### `GET /v1/rewards/node-fees`

Must expose the live adaptive reward-node fee schedule, including:

- `policy_version`
- `driver`
- `registered_reward_node_count`
- `active_reward_node_count`
- `target_registered_reward_node_count`
- `register_fee_chipbits`
- `register_fee_chc`
- `renew_fee_chipbits`
- `renew_fee_chc`
- min/max bounds

## Reference Numbers

- First era length: `111,000` blocks
- First era miner issuance: `5,550,000 CHC`
- First era node issuance: `55,500 CHC`
- First era total issuance: `5,605,500 CHC`
- Pure schedule crosses `11,000,000 CHC` during era `6`
- Estimated cap reach: around block `643,295`
- Estimated full issuance time at 5 minute target: around `6.1 years`

These are verification targets for economics tests and documentation, not substitute logic.

## Required Test Coverage

- miner subsidy across halving boundaries
- node epoch reward across halving boundaries
- exact hard-cap clamp at `11,000,000 CHC`
- zero issuance after cap
- deterministic allocation under cap clamp
- zero eligible nodes means unminted node reward
- supply accounting remains correct across reorg
- `/v1/supply` returns correct active-chain values

## Non-Goals For First Implementation

- no backward compatibility with the old ratio-based node reward model
- no treasury accounting
- no dynamic governance of monetary parameters
- no public claim flow
- no consensus-level observer logic
