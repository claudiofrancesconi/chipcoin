"""Mining orchestration and block template assembly."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cmp_to_key

from ..consensus.economics import reward_fee_node_count, subsidy_split_chipbits
from ..consensus.epoch_settlement import (
    REWARD_ATTESTATION_BUNDLE_KIND,
    REWARD_SETTLE_EPOCH_KIND,
    RewardSettlement,
    attestation_identity,
    parse_reward_attestation_bundle_metadata,
    parse_reward_settlement_metadata,
)
from ..consensus.merkle import merkle_root
from ..consensus.models import Block, BlockHeader, Transaction, TxOutput
from ..consensus.nodes import NodeRegistryView, apply_special_node_transaction, is_special_node_transaction
from ..consensus.params import ConsensusParams
from ..consensus.pow import verify_proof_of_work
from ..consensus.serialization import serialize_transaction
from ..consensus.utxo import InMemoryUtxoView
from ..consensus.validation import ValidationContext, ValidationError, validate_transaction
from ..pq.policy import DEFAULT_PQ_POLICY_LIMITS, pq_signature_cost
from ..storage.mempool import MempoolEntry


@dataclass(frozen=True)
class BlockTemplate:
    """Candidate block plus accounting metadata."""

    block: Block
    height: int
    total_fees: int


@dataclass(frozen=True)
class TransactionSelection:
    """Candidate mempool transaction with derived mining-order metadata."""

    entry: MempoolEntry
    weight_units: int

    @property
    def fee_chipbits(self) -> int:
        return self.entry.fee

    @property
    def transaction(self) -> Transaction:
        return self.entry.transaction


def build_coinbase_transaction(
    *,
    height: int,
    miner_address: str,
    miner_amount_chipbits: int,
    rewarded_outputs: tuple[TxOutput, ...],
    extra_metadata: dict[str, str] | None = None,
) -> Transaction:
    """Build one deterministic coinbase transaction for a candidate block."""

    metadata = {"coinbase": "true", "height": str(height)}
    if extra_metadata:
        metadata.update(extra_metadata)
    return Transaction(
        version=1,
        inputs=(),
        outputs=(
            TxOutput(value=miner_amount_chipbits, recipient=miner_address),
            *rewarded_outputs,
        ),
        metadata=metadata,
    )


class MiningCoordinator:
    """Prepare candidate blocks and manage future mining workflows."""

    def __init__(
        self,
        *,
        params: ConsensusParams,
        time_provider,
    ) -> None:
        self.params = params
        self.time_provider = time_provider

    def build_block_template(
        self,
        *,
        previous_block_hash: str,
        network: str,
        height: int,
        miner_address: str,
        bits: int,
        mempool_entries: list[MempoolEntry],
        node_registry_view: NodeRegistryView,
        confirmed_transaction_ids: set[str] | None = None,
        extra_coinbase_metadata: dict[str, str] | None = None,
        system_transactions: tuple[Transaction, ...] = (),
    ) -> BlockTemplate:
        """Construct a block template from chainstate and mempool."""

        miner_subsidy_chipbits, node_pool_chipbits = subsidy_split_chipbits(height, self.params)
        provisional_coinbase = build_coinbase_transaction(
            height=height,
            miner_address=miner_address,
            miner_amount_chipbits=0,
            rewarded_outputs=(),
        )
        coinbase_weight_units = transaction_weight_units(provisional_coinbase)
        system_transaction_weight_units = sum(transaction_weight_units(transaction) for transaction in system_transactions)
        max_transaction_weight_units = max(0, self.params.max_block_weight - coinbase_weight_units - system_transaction_weight_units)
        selected_entries = self._select_mempool_entries(
            mempool_entries,
            height=height,
            max_transaction_weight_units=max_transaction_weight_units,
            network=network,
            confirmed_transaction_ids=confirmed_transaction_ids or set(),
            node_registry_view=node_registry_view,
        )
        total_fees_chipbits = sum(entry.fee for entry in selected_entries)
        miner_amount_chipbits = miner_subsidy_chipbits + total_fees_chipbits
        included_transactions = [*system_transactions, *(entry.transaction for entry in selected_entries)]
        settlement = _included_epoch_settlement(included_transactions, height=height)
        rewarded_outputs = _reward_outputs_for_settlement(settlement)
        coinbase_metadata = dict(extra_coinbase_metadata or {})
        if settlement is not None:
            coinbase_metadata.update(
                {
                    "reward_settlement_epoch": str(settlement.epoch_index),
                    "reward_settlement_rewarded_count": str(settlement.rewarded_node_count),
                    "reward_settlement_submission_mode": settlement.submission_mode,
                }
            )
        coinbase = build_coinbase_transaction(
            height=height,
            miner_address=miner_address,
            miner_amount_chipbits=miner_amount_chipbits,
            rewarded_outputs=rewarded_outputs,
            extra_metadata=coinbase_metadata or None,
        )
        transactions = (coinbase, *system_transactions, *(entry.transaction for entry in selected_entries))
        header = BlockHeader(
            version=1,
            previous_block_hash=previous_block_hash,
            merkle_root=merkle_root([transaction.txid() for transaction in transactions]),
            timestamp=self.time_provider(),
            bits=bits,
            nonce=0,
        )
        return BlockTemplate(
            block=Block(header=header, transactions=transactions),
            height=height,
            total_fees=total_fees_chipbits,
        )

    def mine_block(self, template: BlockTemplate, *, start_nonce: int = 0, max_nonce_attempts: int = 100_000) -> Block | None:
        """Attempt to mine a valid block from a template within a nonce budget."""

        for nonce in range(start_nonce, start_nonce + max_nonce_attempts):
            header = replace(template.block.header, nonce=nonce)
            if verify_proof_of_work(header):
                return replace(template.block, header=header)
        return None

    def _select_mempool_entries(
        self,
        mempool_entries: list[MempoolEntry],
        *,
        height: int,
        network: str,
        max_transaction_weight_units: int,
        confirmed_transaction_ids: set[str],
        node_registry_view: NodeRegistryView,
    ) -> list[MempoolEntry]:
        """Select mempool transactions by fee-rate with basic ancestor ordering."""

        selections = {
            entry.transaction.txid(): TransactionSelection(
                entry=entry,
                weight_units=transaction_weight_units(entry.transaction),
            )
            for entry in mempool_entries
        }
        pending = list(
            sorted(
                selections.values(),
                key=cmp_to_key(_compare_transaction_selection),
            )
        )
        included_txids: set[str] = set()
        included_entries: list[MempoolEntry] = []
        current_weight_units = 0
        current_pq_signature_cost = 0
        included_attestation_bundle_count = 0
        included_attestation_identities: set[tuple[int, int, str, str]] = set()
        included_verifier_window_counts: dict[tuple[int, str], int] = {}
        staged_registry = node_registry_view.clone()
        special_node_context = ValidationContext(
            height=height,
            median_time_past=0,
            params=self.params,
            utxo_view=InMemoryUtxoView(),
            network=network,
            node_registry_view=staged_registry,
            reward_fee_registry_count=reward_fee_node_count(node_registry_view, height=height, params=self.params),
        )

        while pending:
            progressed = False
            next_pending: list[TransactionSelection] = []
            for selection in pending:
                if self._has_unresolved_parent_dependency(
                    selection.transaction,
                    selections,
                    included_txids,
                    confirmed_transaction_ids,
                ):
                    next_pending.append(selection)
                    continue
                if (
                    selection.transaction.metadata.get("kind") == REWARD_ATTESTATION_BUNDLE_KIND
                    and included_attestation_bundle_count >= self.params.max_attestation_bundles_per_block
                ):
                    continue
                if selection.transaction.metadata.get("kind") == REWARD_ATTESTATION_BUNDLE_KIND:
                    bundle = parse_reward_attestation_bundle_metadata(selection.transaction.metadata)
                    bundle_identities = [attestation_identity(attestation) for attestation in bundle.attestations]
                    if any(identity in included_attestation_identities for identity in bundle_identities):
                        continue
                    verifier_window_counts: dict[tuple[int, str], int] = {}
                    for attestation in bundle.attestations:
                        key = (attestation.check_window_index, attestation.verifier_node_id)
                        verifier_window_counts[key] = verifier_window_counts.get(key, 0) + 1
                    if any(
                        included_verifier_window_counts.get(key, 0) + count
                        > self.params.max_attestations_per_verifier_per_window
                        for key, count in verifier_window_counts.items()
                    ):
                        continue
                if current_weight_units + selection.weight_units > max_transaction_weight_units:
                    continue
                selection_pq_signature_cost = pq_signature_cost(selection.transaction, DEFAULT_PQ_POLICY_LIMITS)
                if (
                    current_pq_signature_cost + selection_pq_signature_cost
                    > DEFAULT_PQ_POLICY_LIMITS.max_pq_signature_cost_per_block
                ):
                    continue
                if is_special_node_transaction(selection.transaction):
                    try:
                        validate_transaction(selection.transaction, special_node_context)
                        apply_special_node_transaction(
                            selection.transaction,
                            height=height,
                            registry_view=staged_registry,
                        )
                    except (ValidationError, ValueError):
                        continue
                included_entries.append(selection.entry)
                included_txids.add(selection.transaction.txid())
                current_weight_units += selection.weight_units
                current_pq_signature_cost += selection_pq_signature_cost
                if selection.transaction.metadata.get("kind") == REWARD_ATTESTATION_BUNDLE_KIND:
                    for identity in bundle_identities:
                        included_attestation_identities.add(identity)
                    for key, count in verifier_window_counts.items():
                        included_verifier_window_counts[key] = included_verifier_window_counts.get(key, 0) + count
                    included_attestation_bundle_count += 1
                progressed = True
            if not progressed:
                break
            pending = next_pending

        return included_entries

    def _has_unresolved_parent_dependency(
        self,
        transaction: Transaction,
        selections: dict[str, TransactionSelection],
        included_txids: set[str],
        confirmed_transaction_ids: set[str],
    ) -> bool:
        """Return whether a transaction depends on a mempool parent not yet included."""

        for tx_input in transaction.inputs:
            parent_txid = tx_input.previous_output.txid
            if parent_txid in selections and parent_txid not in included_txids:
                return True
            if parent_txid not in selections and parent_txid not in confirmed_transaction_ids:
                return True
        return False


def _included_epoch_settlement(transactions: list[Transaction], *, height: int) -> RewardSettlement | None:
    """Return the reward settlement included for the current block height, if any."""

    for transaction in transactions:
        if transaction.metadata.get("kind") != REWARD_SETTLE_EPOCH_KIND:
            continue
        settlement = parse_reward_settlement_metadata(transaction.metadata)
        if settlement.epoch_end_height == height:
            return settlement
    return None


def _reward_outputs_for_settlement(settlement: RewardSettlement | None) -> tuple[TxOutput, ...]:
    """Return deterministic coinbase node reward outputs for one settlement."""

    if settlement is None:
        return ()
    ordered_entries = sorted(settlement.reward_entries, key=lambda entry: entry.selection_rank)
    return tuple(
        TxOutput(value=entry.reward_chipbits, recipient=entry.payout_address)
        for entry in ordered_entries
    )


def transaction_weight_units(transaction: Transaction) -> int:
    """Return the didactic weight metric used for fee-rate and block limits.

    Chipcoin currently uses serialized transaction byte length as both size and
    weight: 1 serialized byte == 1 weight unit.
    """

    return len(serialize_transaction(transaction))


def _compare_transaction_selection(left: TransactionSelection, right: TransactionSelection) -> int:
    """Order candidate transactions by descending fee-rate with stable tiebreakers."""

    left_score = left.fee_chipbits * right.weight_units
    right_score = right.fee_chipbits * left.weight_units
    if left_score != right_score:
        return -1 if left_score > right_score else 1
    if left.fee_chipbits != right.fee_chipbits:
        return -1 if left.fee_chipbits > right.fee_chipbits else 1
    if left.entry.added_at != right.entry.added_at:
        return -1 if left.entry.added_at < right.entry.added_at else 1
    if left.transaction.txid() != right.transaction.txid():
        return -1 if left.transaction.txid() < right.transaction.txid() else 1
    return 0
