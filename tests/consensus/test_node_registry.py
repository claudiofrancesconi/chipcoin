from chipcoin.consensus.economics import node_reward_pool_chipbits
from chipcoin.consensus.models import Transaction
from chipcoin.consensus.nodes import (
    InMemoryNodeRegistryView,
    NodeRecord,
    active_node_records,
    apply_special_node_transaction,
    is_special_node_transaction,
    select_rewarded_nodes,
)
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.consensus.utxo import InMemoryUtxoView
from chipcoin.consensus.validation import (
    ContextualValidationError,
    ValidationContext,
    validate_transaction,
)
from chipcoin.crypto.signatures import sign_digest
from tests.helpers import wallet_key


def _register_node_transaction(*, node_id: str, owner_index: int = 0, payout_address: str | None = None) -> Transaction:
    owner = wallet_key(owner_index)
    metadata = {
        "kind": "register_node",
        "node_id": node_id,
        "payout_address": owner.address if payout_address is None else payout_address,
        "owner_pubkey_hex": owner.public_key.hex(),
        "owner_signature_hex": "",
    }
    unsigned = Transaction(version=1, inputs=(), outputs=(), metadata=metadata)
    from chipcoin.consensus.nodes import special_node_transaction_signature_digest

    signed_metadata = dict(metadata)
    signed_metadata["owner_signature_hex"] = sign_digest(owner.private_key, special_node_transaction_signature_digest(unsigned)).hex()
    return Transaction(version=1, inputs=(), outputs=(), metadata=signed_metadata)


def _renew_node_transaction(*, node_id: str, owner_index: int = 0, renewal_epoch: int = 0) -> Transaction:
    owner = wallet_key(owner_index)
    metadata = {
        "kind": "renew_node",
        "node_id": node_id,
        "renewal_epoch": str(renewal_epoch),
        "owner_pubkey_hex": owner.public_key.hex(),
        "owner_signature_hex": "",
    }
    unsigned = Transaction(version=1, inputs=(), outputs=(), metadata=metadata)
    from chipcoin.consensus.nodes import special_node_transaction_signature_digest

    signed_metadata = dict(metadata)
    signed_metadata["owner_signature_hex"] = sign_digest(owner.private_key, special_node_transaction_signature_digest(unsigned)).hex()
    return Transaction(version=1, inputs=(), outputs=(), metadata=signed_metadata)


def test_register_node_transaction_is_special_and_updates_registry() -> None:
    registry = InMemoryNodeRegistryView()
    transaction = _register_node_transaction(node_id="node-1")

    assert is_special_node_transaction(transaction) is True
    apply_special_node_transaction(transaction, height=7, registry_view=registry)
    record = registry.get_by_node_id("node-1")
    assert record is not None
    assert record.last_renewed_height == 7


def test_register_node_rejects_duplicate_owner_pubkey() -> None:
    registry = InMemoryNodeRegistryView.from_records(
        [
            NodeRecord(
                node_id="node-1",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=5,
                last_renewed_height=5,
            )
        ]
    )
    transaction = _register_node_transaction(node_id="node-2", owner_index=0)
    context = ValidationContext(height=6, median_time_past=0, params=MAINNET_PARAMS, utxo_view=InMemoryUtxoView(), node_registry_view=registry)

    try:
        validate_transaction(transaction, context)
    except ContextualValidationError:
        return
    raise AssertionError("Expected duplicate owner_pubkey register_node transaction to be rejected.")


def test_active_node_set_excludes_same_block_registration() -> None:
    registry = InMemoryNodeRegistryView.from_records(
        [
            NodeRecord(
                node_id="node-1",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=1000,
                last_renewed_height=1000,
            )
        ]
    )

    assert active_node_records(registry, height=1000, params=MAINNET_PARAMS) == []
    assert len(active_node_records(registry, height=1001, params=MAINNET_PARAMS)) == 1


def test_winner_selection_limits_to_ten_nodes_and_is_deterministic() -> None:
    records = [
        NodeRecord(
            node_id=f"node-{index}",
            payout_address=wallet_key(index % 3).address + str(index),
            owner_pubkey=wallet_key(index % 3).public_key + bytes((index,)),
            registered_height=1,
            last_renewed_height=1,
        )
        for index in range(12)
    ]
    registry = InMemoryNodeRegistryView.from_records(records)

    winners = select_rewarded_nodes(
        registry,
        height=2,
        previous_block_hash="11" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )

    assert len(winners) == 10
    assert winners == select_rewarded_nodes(
        registry,
        height=2,
        previous_block_hash="11" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )
