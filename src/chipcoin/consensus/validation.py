"""Consensus validation interfaces and error types."""

from __future__ import annotations

from dataclasses import dataclass, field
from string import hexdigits

from ..crypto.addresses import public_key_to_address
from ..crypto.signatures import verify_digest
from .economics import subsidy_split_chipbits
from .hashes import double_sha256
from .merkle import merkle_root
from .models import Block, Transaction, TxOutput
from .nodes import (
    InMemoryNodeRegistryView,
    NodeRegistryView,
    apply_special_node_transaction,
    is_register_node_transaction,
    is_renew_node_transaction,
    is_special_node_transaction,
    select_rewarded_nodes,
    validate_special_node_transaction_stateless,
)
from .params import ConsensusParams, MAINNET_PARAMS
from .pow import bits_to_target, verify_proof_of_work
from .serialization import serialize_transaction, serialize_transaction_for_signing
from .utxo import UtxoView


class ValidationError(Exception):
    """Raised when a transaction or block violates consensus rules."""


class StatelessValidationError(ValidationError):
    """Raised for structure or encoding problems independent from chain state."""


class ContextualValidationError(ValidationError):
    """Raised for validation failures that depend on current chain state."""


@dataclass(frozen=True)
class ValidationContext:
    """Context needed for validation against current chain state."""

    height: int
    median_time_past: int
    params: ConsensusParams
    utxo_view: UtxoView
    node_registry_view: NodeRegistryView = field(default_factory=InMemoryNodeRegistryView)
    expected_previous_block_hash: str | None = None
    expected_bits: int | None = None
    enforce_coinbase_maturity: bool = True


def is_coinbase_transaction(transaction: Transaction) -> bool:
    """Return whether a transaction is a block coinbase."""

    return not transaction.inputs and transaction.metadata.get("coinbase") == "true"


def validate_transaction(transaction: Transaction, context: ValidationContext) -> int:
    """Perform full validation for a transaction and return its fee."""

    validate_transaction_stateless(transaction)
    return validate_transaction_stateful(transaction, context)


def validate_transaction_stateless(transaction: Transaction) -> None:
    """Validate transaction structure independently from chain state."""

    if transaction.version <= 0:
        raise StatelessValidationError("Transaction version must be positive.")
    if is_special_node_transaction(transaction):
        try:
            validate_special_node_transaction_stateless(transaction)
        except ValueError as exc:
            raise StatelessValidationError(str(exc)) from exc
        return
    if not transaction.outputs:
        raise StatelessValidationError("Transaction must contain at least one output.")
    if transaction.locktime < 0:
        raise StatelessValidationError("Transaction locktime cannot be negative.")

    for output in transaction.outputs:
        if int(output.value) < 0:
            raise StatelessValidationError("Transaction outputs cannot be negative.")
        if not output.recipient:
            raise StatelessValidationError("Transaction outputs must declare a recipient.")

    if is_coinbase_transaction(transaction):
        return

    if not transaction.inputs:
        raise StatelessValidationError("Non-coinbase transactions must have at least one input.")

    seen_outpoints = set()
    for tx_input in transaction.inputs:
        _validate_outpoint(tx_input.previous_output.txid)
        if tx_input.previous_output.index < 0:
            raise StatelessValidationError("Outpoint index cannot be negative.")
        if tx_input.previous_output in seen_outpoints:
            raise StatelessValidationError("Transaction cannot spend the same outpoint twice.")
        seen_outpoints.add(tx_input.previous_output)
        if not tx_input.signature:
            raise StatelessValidationError("Non-coinbase inputs must include a signature.")
        if not tx_input.public_key:
            raise StatelessValidationError("Non-coinbase inputs must include a public key.")


def validate_transaction_stateful(transaction: Transaction, context: ValidationContext) -> int:
    """Validate a transaction against current UTXO state and return its fee."""

    if is_coinbase_transaction(transaction):
        return 0
    if is_special_node_transaction(transaction):
        _validate_special_node_transaction_stateful(transaction, context)
        return 0

    input_total_chipbits = 0
    for input_index, tx_input in enumerate(transaction.inputs):
        entry = context.utxo_view.get(tx_input.previous_output)
        if entry is None:
            raise ContextualValidationError("Referenced input does not exist in the UTXO set.")
        if context.enforce_coinbase_maturity and not is_coinbase_mature(entry, context.height, context.params):
            raise ContextualValidationError("Coinbase output is not mature enough to spend.")
        try:
            derived_recipient = public_key_to_address(tx_input.public_key)
        except ValueError as exc:
            raise ContextualValidationError("Input public key is not a valid secp256k1 public key.") from exc
        if derived_recipient != entry.output.recipient:
            raise ContextualValidationError("Input public key does not match the referenced output recipient.")
        digest = transaction_signature_digest(transaction, input_index, previous_output=entry.output)
        if not verify_digest(tx_input.public_key, digest, tx_input.signature):
            raise ContextualValidationError("Input signature is invalid.")
        input_total_chipbits += int(entry.output.value)

    output_total_chipbits = transaction_output_total(transaction)
    if input_total_chipbits < output_total_chipbits:
        raise ContextualValidationError("Transaction outputs exceed transaction inputs.")

    return input_total_chipbits - output_total_chipbits


def validate_block(block: Block, context: ValidationContext) -> int:
    """Perform full block validation and return the total fee amount."""

    validate_block_stateless(block, context)
    return validate_block_stateful(block, context)


def validate_block_stateless(block: Block, context: ValidationContext | None = None) -> None:
    """Validate a block independently from chain UTXO state."""

    if not block.transactions:
        raise StatelessValidationError("Block must contain at least one transaction.")
    if not is_coinbase_transaction(block.transactions[0]):
        raise StatelessValidationError("First block transaction must be coinbase.")
    if any(is_coinbase_transaction(transaction) for transaction in block.transactions[1:]):
        raise StatelessValidationError("Only the first block transaction may be coinbase.")

    for transaction in block.transactions:
        validate_transaction_stateless(transaction)
    if block_weight_units(block) > (context.params.max_block_weight if context is not None else MAINNET_PARAMS.max_block_weight):
        raise StatelessValidationError("Block exceeds maximum block weight.")

    expected_merkle_root = merkle_root([transaction.txid() for transaction in block.transactions])
    if block.header.merkle_root != expected_merkle_root:
        raise StatelessValidationError("Block Merkle root does not match transaction contents.")
    if block.header.timestamp < 0:
        raise StatelessValidationError("Block timestamp cannot be negative.")
    if context is not None and block.header.timestamp < context.median_time_past:
        raise StatelessValidationError("Block timestamp is below median time past.")

    bits_to_target(block.header.bits)
    if not verify_proof_of_work(block.header):
        raise StatelessValidationError("Block proof of work is invalid.")


def validate_block_stateful(block: Block, context: ValidationContext) -> int:
    """Validate a block against UTXO state and return total fees."""

    if context.expected_previous_block_hash is not None:
        if block.header.previous_block_hash != context.expected_previous_block_hash:
            raise ContextualValidationError("Block does not connect to the expected previous hash.")
    if context.expected_bits is not None:
        if block.header.bits != context.expected_bits:
            raise ContextualValidationError("Block bits do not match expected difficulty target.")

    staged_view = context.utxo_view.clone()
    staged_registry = context.node_registry_view.clone()
    total_fees_chipbits = 0
    seen_spends = set()

    for transaction in block.transactions[1:]:
        if not is_special_node_transaction(transaction):
            for tx_input in transaction.inputs:
                if tx_input.previous_output in seen_spends:
                    raise ContextualValidationError("Block contains a double spend.")
                seen_spends.add(tx_input.previous_output)

        staged_context = ValidationContext(
            height=context.height,
            median_time_past=context.median_time_past,
            params=context.params,
            utxo_view=staged_view,
            node_registry_view=staged_registry,
            enforce_coinbase_maturity=context.enforce_coinbase_maturity,
        )
        fee_chipbits = validate_transaction_stateful(transaction, staged_context)
        total_fees_chipbits += fee_chipbits
        if is_special_node_transaction(transaction):
            apply_special_node_transaction(transaction, height=context.height, registry_view=staged_registry)
        else:
            staged_view.apply_transaction(transaction, context.height)

    _validate_coinbase_distribution(
        block.transactions[0],
        height=context.height,
        previous_block_hash=block.header.previous_block_hash,
        total_fees_chipbits=total_fees_chipbits,
        context=context,
    )

    return total_fees_chipbits


def transaction_output_total(transaction: Transaction) -> int:
    """Return the total output value of a transaction."""

    return sum(int(output.value) for output in transaction.outputs)


def transaction_weight_units(transaction: Transaction) -> int:
    """Return the didactic transaction weight metric used by consensus limits."""

    return len(serialize_transaction(transaction))


def block_weight_units(block: Block) -> int:
    """Return the didactic block weight metric as the sum of serialized tx bytes."""

    return sum(transaction_weight_units(transaction) for transaction in block.transactions)


def transaction_signature_digest(transaction: Transaction, input_index: int, *, previous_output: TxOutput) -> bytes:
    """Return the digest that must be signed for one transaction input."""

    return double_sha256(
        serialize_transaction_for_signing(
            transaction,
            input_index,
            previous_output_value=int(previous_output.value),
            previous_output_recipient=previous_output.recipient,
        )
    )


def _validate_coinbase_distribution(
    coinbase_transaction: Transaction,
    *,
    height: int,
    previous_block_hash: str,
    total_fees_chipbits: int,
    context: ValidationContext,
) -> None:
    """Validate exact miner and node reward outputs for a coinbase transaction."""

    if not coinbase_transaction.outputs:
        raise ContextualValidationError("Coinbase transaction must contain at least one output.")

    miner_base_subsidy_chipbits, node_pool_chipbits = subsidy_split_chipbits(height, context.params)
    rewarded_nodes = select_rewarded_nodes(
        context.node_registry_view,
        height=height,
        previous_block_hash=previous_block_hash,
        node_reward_pool_chipbits=node_pool_chipbits,
        params=context.params,
    )
    distributed_node_reward_chipbits = sum(rewarded_node.reward_chipbits for rewarded_node in rewarded_nodes)
    miner_amount_chipbits = (
        miner_base_subsidy_chipbits
        + total_fees_chipbits
        + (node_pool_chipbits - distributed_node_reward_chipbits)
    )

    expected_output_count = 1 + len(rewarded_nodes)
    if len(coinbase_transaction.outputs) != expected_output_count:
        raise ContextualValidationError("Coinbase outputs do not match the expected miner and node reward distribution.")
    if int(coinbase_transaction.outputs[0].value) != miner_amount_chipbits:
        raise ContextualValidationError("Coinbase miner payout amount is incorrect.")

    for index, rewarded_node in enumerate(rewarded_nodes, start=1):
        actual_output = coinbase_transaction.outputs[index]
        if actual_output.recipient != rewarded_node.payout_address:
            raise ContextualValidationError("Coinbase node reward recipient ordering is incorrect.")
        if int(actual_output.value) != rewarded_node.reward_chipbits:
            raise ContextualValidationError("Coinbase node reward amount is incorrect.")


def _validate_special_node_transaction_stateful(transaction: Transaction, context: ValidationContext) -> None:
    """Validate stateful node registry rules for register and renew actions."""

    owner_pubkey = bytes.fromhex(transaction.metadata["owner_pubkey_hex"])
    if is_register_node_transaction(transaction):
        node_id = transaction.metadata["node_id"]
        if context.node_registry_view.get_by_node_id(node_id) is not None:
            raise ContextualValidationError("register_node transaction reuses an existing node_id.")
        if context.node_registry_view.get_by_owner_pubkey(owner_pubkey) is not None:
            raise ContextualValidationError("register_node transaction reuses an existing owner_pubkey.")
        return

    if is_renew_node_transaction(transaction):
        node_id = transaction.metadata["node_id"]
        record = context.node_registry_view.get_by_node_id(node_id)
        if record is None:
            raise ContextualValidationError("renew_node transaction references an unknown node_id.")
        if record.owner_pubkey != owner_pubkey:
            raise ContextualValidationError("renew_node transaction owner_pubkey does not match the registered node owner.")
        if transaction.metadata.get("renewal_epoch") != str(context.height // context.params.epoch_length_blocks):
            raise ContextualValidationError("renew_node transaction renewal_epoch does not match the block epoch.")
        return

    raise ContextualValidationError("Unsupported special node transaction kind.")


def is_coinbase_mature(entry: object, spend_height: int, params: ConsensusParams) -> bool:
    """Return whether a coinbase output can be spent at the given height."""

    if not hasattr(entry, "is_coinbase") or not hasattr(entry, "height"):
        raise TypeError("Coinbase maturity checks require a UtxoEntry-like object.")
    if not getattr(entry, "is_coinbase"):
        return True
    return spend_height - int(getattr(entry, "height")) >= params.coinbase_maturity


def _validate_outpoint(txid: str) -> None:
    """Validate that a transaction identifier is a 32-byte hex string."""

    if len(txid) != 64 or any(character not in hexdigits for character in txid):
        raise StatelessValidationError("Outpoint transaction identifiers must be 32-byte hex strings.")
