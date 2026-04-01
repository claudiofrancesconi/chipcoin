# Protocol Outline

## Monetary Unit

- Consensus uses only integer `Chipbits`.
- `1 CHC = 100_000_000 Chipbits`.
- No float monetary arithmetic is allowed in consensus, storage, validation, or mining.

## Special Transactions

Chipcoin v2 defines two consensus-validated special transactions outside the UTXO spend model:

- `register_node`
- `renew_node`

Both are:

- non-coinbase
- deterministic from on-chain metadata only
- signed by the node owner key
- effective from the next block, never the same block

`register_node` metadata:

- `kind=register_node`
- `node_id`
- `payout_address`
- `owner_pubkey_hex`
- `owner_signature_hex`

`renew_node` metadata:

- `kind=renew_node`
- `node_id`
- `owner_pubkey_hex`
- `owner_signature_hex`

Consensus rules:

- `node_id` is unique
- only one active registration exists for a given `owner_pubkey`
- `renew_node` is the standard path to keep a registered node active
- bootstrap fees default to zero for both actions

## Node Rewards

Per-block subsidy is split into:

- miner base subsidy
- deterministic node reward pool

At genesis-era parameters:

- miner subsidy: `50 CHC`
- node reward pool: `5 CHC`
- total subsidy: `55 CHC`

Epoch and reward parameters:

- mainnet:
  - epoch length: `1000` blocks
  - retarget window: `1000` blocks
  - target block time: `120` seconds
  - coinbase maturity: `100` blocks
- devnet:
  - epoch length: `1000` blocks
  - retarget window: `200` blocks
  - target block time: `30` seconds
  - coinbase maturity: `10` blocks
- maximum rewarded nodes per block: `10`

A node is active at height `H` when:

- it exists in the on-chain node registry
- its last valid `register_node` or `renew_node` belongs to epoch `H // 1000`
- that last action occurred before height `H`

Winner selection for block `H`:

1. reconstruct active nodes from chain state only
2. compute `score = hash(prev_block_hash || node_id || payout_address)`
3. sort by ascending score
4. choose `K = min(10, active_nodes_count)`
5. split node reward pool equally across the `K` winners
6. any integer remainder goes to the miner

Coinbase validation checks exact expected outputs:

- first output is miner payout
- following outputs are winner payouts in deterministic score order
- wrong recipients, wrong amounts, or wrong output count are invalid

## Transport

The main peer protocol is planned as a custom TCP-based protocol with a small binary framing layer. HTTP is not part of peer consensus traffic.

## Message Families

Initial message set:

- `version`
- `verack`
- `ping`
- `pong`
- `getaddr`
- `addr`
- `inv`
- `getdata`
- `tx`
- `block`
- `getheaders`
- `headers`
- `getblocks`

## Synchronization Strategy

Synchronization is intended to be header-first:

1. exchange versions
2. request headers
3. compare cumulative work
4. request missing blocks
5. apply validated blocks to local chainstate

## Serialization Requirement

Consensus-critical objects must use deterministic serialization. The exact wire format may differ from internal storage encoding, but both must be explicit and versioned.

## Transaction Signatures

Ordinary spend transactions use real secp256k1 ECDSA signatures over a deterministic per-input payload.

For input index `i`, the signing payload is:

1. the transaction serialized with all input signatures and public keys stripped
2. the current `input_index`
3. the referenced previous output value in integer `Chipbits`
4. the referenced previous output recipient address
5. a fixed signing mode marker

The final digest signed and verified by consensus is `double_sha256(signing_payload)`.

Validation rules:

- the provided SEC1 public key must be a valid secp256k1 point
- `public_key_to_address(public_key)` must match the referenced UTXO recipient
- the ECDSA signature must verify against the canonical digest
- any mutation of outputs, metadata, previous-output binding, or input ordering invalidates the signature

Wallets keep private keys off-node. Nodes only receive signed transactions and verify them during mempool admission and block validation.

## Validity vs Policy

Chipcoin v2 distinguishes between:

- consensus validity: rules every node must enforce for blocks and transactions
- mempool policy: local admission and retention rules for unconfirmed transactions

Consensus validity currently includes:

- deterministic transaction serialization
- per-input secp256k1 signature verification
- referenced-UTXO ownership checks
- value conservation and coinbase rules
- block-level proof-of-work and subsidy validation

Mempool policy currently includes:

- minimum fee for ordinary transactions
- duplicate rejection
- conflict rejection for transactions spending already-reserved inputs
- output recipient address standardness checks
- size and input/output-count limits
- transaction TTL expiry
- simple capacity-based eviction of lowest-fee, oldest entries

## Block Assembly

Chipcoin block assembly is deterministic and intentionally simple.

Transaction selection:

- fee-rate is computed as `fee_chipbits / weight_units`
- `weight_units` currently equals serialized transaction byte length
- equivalently: `1 serialized byte = 1 weight unit`
- selection compares fee-rate using integer cross-multiplication, not floats
- ties are broken by higher absolute fee, then earlier mempool admission time, then lower `txid`

Dependency handling:

- a child transaction can be included only if its parent transaction is already confirmed or already selected earlier in the same block
- selected transactions are ordered so ancestors always appear before descendants

Block limit:

- the current limit uses `max_block_weight`
- the miner reserves budget for coinbase first
- then it includes mempool transactions until the remaining weight budget is exhausted

A transaction may be consensus-valid but still be rejected from mempool for policy reasons. Such a transaction is non-standard, not necessarily invalid by consensus.

## Deferred Work

This skeleton does not implement:

- peer scoring logic
- anti-abuse protections
