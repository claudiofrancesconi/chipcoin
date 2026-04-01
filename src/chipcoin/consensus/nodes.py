"""On-chain node registry and deterministic node-reward selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..crypto.addresses import is_valid_address
from ..crypto.keys import parse_public_key_hex
from ..crypto.signatures import verify_digest
from .hashes import double_sha256
from .models import Transaction
from .params import ConsensusParams


REGISTER_NODE_KIND = "register_node"
RENEW_NODE_KIND = "renew_node"


@dataclass(frozen=True)
class NodeRecord:
    """Consensus-visible node registry record."""

    node_id: str
    payout_address: str
    owner_pubkey: bytes
    registered_height: int
    last_renewed_height: int


@dataclass(frozen=True)
class RewardedNode:
    """Deterministically selected node reward recipient."""

    node_id: str
    payout_address: str
    owner_pubkey: bytes
    score_hex: str
    reward_chipbits: int


class NodeRegistryView:
    """Minimal node-registry access contract for validation and mining."""

    def get_by_node_id(self, node_id: str) -> NodeRecord | None:
        raise NotImplementedError

    def get_by_owner_pubkey(self, owner_pubkey: bytes) -> NodeRecord | None:
        raise NotImplementedError

    def upsert(self, record: NodeRecord) -> None:
        raise NotImplementedError

    def clone(self) -> "NodeRegistryView":
        raise NotImplementedError

    def list_records(self) -> list[NodeRecord]:
        raise NotImplementedError


class InMemoryNodeRegistryView(NodeRegistryView):
    """Simple in-memory node registry view."""

    def __init__(self, entries: dict[str, NodeRecord] | None = None) -> None:
        self._entries = dict(entries or {})

    @classmethod
    def from_records(cls, records: Iterable[NodeRecord]) -> "InMemoryNodeRegistryView":
        return cls({record.node_id: record for record in records})

    def get_by_node_id(self, node_id: str) -> NodeRecord | None:
        return self._entries.get(node_id)

    def get_by_owner_pubkey(self, owner_pubkey: bytes) -> NodeRecord | None:
        for record in self._entries.values():
            if record.owner_pubkey == owner_pubkey:
                return record
        return None

    def upsert(self, record: NodeRecord) -> None:
        self._entries[record.node_id] = record

    def clone(self) -> "NodeRegistryView":
        return InMemoryNodeRegistryView(self._entries)

    def list_records(self) -> list[NodeRecord]:
        return sorted(self._entries.values(), key=lambda record: (record.node_id, record.payout_address))


def is_special_node_transaction(transaction: Transaction) -> bool:
    """Return whether a transaction is a node registry special transaction."""

    return transaction.metadata.get("kind") in {REGISTER_NODE_KIND, RENEW_NODE_KIND}


def is_register_node_transaction(transaction: Transaction) -> bool:
    return transaction.metadata.get("kind") == REGISTER_NODE_KIND


def is_renew_node_transaction(transaction: Transaction) -> bool:
    return transaction.metadata.get("kind") == RENEW_NODE_KIND


def current_epoch(height: int, params: ConsensusParams) -> int:
    """Return the active epoch number for a given block height."""

    if height < 0:
        raise ValueError("Block height cannot be negative.")
    return height // params.epoch_length_blocks


def active_node_records(
    registry_view: NodeRegistryView,
    *,
    height: int,
    params: ConsensusParams,
) -> list[NodeRecord]:
    """Return node records active for reward selection at the supplied height."""

    epoch = current_epoch(height, params)
    return [
        record
        for record in registry_view.list_records()
        if record.last_renewed_height < height and current_epoch(record.last_renewed_height, params) == epoch
    ]


def select_rewarded_nodes(
    registry_view: NodeRegistryView,
    *,
    height: int,
    previous_block_hash: str,
    node_reward_pool_chipbits: int,
    params: ConsensusParams,
) -> list[RewardedNode]:
    """Return the deterministic node reward winners for a block."""

    active_records = active_node_records(registry_view, height=height, params=params)
    if not active_records or node_reward_pool_chipbits <= 0:
        return []

    winners_count = min(params.max_rewarded_nodes_per_block, len(active_records))
    scored = []
    for record in active_records:
        score_hex = double_sha256(
            previous_block_hash.encode("utf-8")
            + b"\x00"
            + record.node_id.encode("utf-8")
            + b"\x00"
            + record.payout_address.encode("utf-8")
        ).hex()
        scored.append((score_hex, record))
    scored.sort(key=lambda item: (item[0], item[1].node_id, item[1].payout_address))

    reward_chipbits = node_reward_pool_chipbits // winners_count
    winners = []
    for score_hex, record in scored[:winners_count]:
        winners.append(
            RewardedNode(
                node_id=record.node_id,
                payout_address=record.payout_address,
                owner_pubkey=record.owner_pubkey,
                score_hex=score_hex,
                reward_chipbits=reward_chipbits,
            )
        )
    return winners


def validate_special_node_transaction_stateless(transaction: Transaction) -> None:
    """Validate metadata shape and signatures for node special transactions."""

    if is_register_node_transaction(transaction):
        _validate_register_node_transaction(transaction)
        return
    if is_renew_node_transaction(transaction):
        _validate_renew_node_transaction(transaction)
        return
    raise ValueError("Transaction is not a special node transaction.")


def apply_special_node_transaction(
    transaction: Transaction,
    *,
    height: int,
    registry_view: NodeRegistryView,
) -> None:
    """Apply a validated node special transaction to registry state."""

    if is_register_node_transaction(transaction):
        owner_pubkey = parse_public_key_hex(transaction.metadata["owner_pubkey_hex"])
        registry_view.upsert(
            NodeRecord(
                node_id=transaction.metadata["node_id"],
                payout_address=transaction.metadata["payout_address"],
                owner_pubkey=owner_pubkey,
                registered_height=height,
                last_renewed_height=height,
            )
        )
        return

    if is_renew_node_transaction(transaction):
        record = registry_view.get_by_node_id(transaction.metadata["node_id"])
        if record is None:
            raise ValueError("Cannot renew a node that is not registered.")
        registry_view.upsert(
            NodeRecord(
                node_id=record.node_id,
                payout_address=record.payout_address,
                owner_pubkey=record.owner_pubkey,
                registered_height=record.registered_height,
                last_renewed_height=height,
            )
        )
        return

    raise ValueError("Transaction is not a special node transaction.")


def special_node_transaction_signature_digest(transaction: Transaction) -> bytes:
    """Return the canonical digest signed by special node transactions."""

    kind = transaction.metadata.get("kind", "")
    owner_pubkey_hex = transaction.metadata.get("owner_pubkey_hex", "")
    if kind == REGISTER_NODE_KIND:
        payload = "|".join(
            [
                REGISTER_NODE_KIND,
                transaction.metadata.get("node_id", ""),
                transaction.metadata.get("payout_address", ""),
                owner_pubkey_hex,
            ]
        )
    elif kind == RENEW_NODE_KIND:
        payload = "|".join(
            [
                RENEW_NODE_KIND,
                transaction.metadata.get("node_id", ""),
                transaction.metadata.get("renewal_epoch", ""),
                owner_pubkey_hex,
            ]
        )
    else:
        raise ValueError("Unsupported special node transaction kind.")
    return double_sha256(payload.encode("utf-8"))


def _validate_register_node_transaction(transaction: Transaction) -> None:
    _validate_node_metadata_common(transaction)
    node_id = transaction.metadata.get("node_id", "")
    payout_address = transaction.metadata.get("payout_address", "")
    if not node_id:
        raise ValueError("register_node transactions must declare a node_id.")
    if not payout_address or not is_valid_address(payout_address):
        raise ValueError("register_node transactions must declare a valid payout_address.")
    owner_pubkey = parse_public_key_hex(transaction.metadata["owner_pubkey_hex"])
    owner_signature = bytes.fromhex(transaction.metadata["owner_signature_hex"])
    if not verify_digest(owner_pubkey, special_node_transaction_signature_digest(transaction), owner_signature):
        raise ValueError("register_node transaction owner signature is invalid.")


def _validate_renew_node_transaction(transaction: Transaction) -> None:
    _validate_node_metadata_common(transaction)
    node_id = transaction.metadata.get("node_id", "")
    renewal_epoch = transaction.metadata.get("renewal_epoch", "")
    if not node_id:
        raise ValueError("renew_node transactions must declare a node_id.")
    if not renewal_epoch:
        raise ValueError("renew_node transactions must declare renewal_epoch.")
    owner_pubkey = parse_public_key_hex(transaction.metadata["owner_pubkey_hex"])
    owner_signature = bytes.fromhex(transaction.metadata["owner_signature_hex"])
    if not verify_digest(owner_pubkey, special_node_transaction_signature_digest(transaction), owner_signature):
        raise ValueError("renew_node transaction owner signature is invalid.")


def _validate_node_metadata_common(transaction: Transaction) -> None:
    owner_pubkey_hex = transaction.metadata.get("owner_pubkey_hex", "")
    owner_signature_hex = transaction.metadata.get("owner_signature_hex", "")
    if transaction.inputs:
        raise ValueError("Special node transactions must not contain UTXO inputs.")
    if transaction.outputs:
        raise ValueError("Special node transactions must not contain outputs.")
    if not owner_pubkey_hex:
        raise ValueError("Special node transactions must declare owner_pubkey_hex.")
    if not owner_signature_hex:
        raise ValueError("Special node transactions must declare owner_signature_hex.")
