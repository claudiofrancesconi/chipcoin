"""Mining orchestration and block template assembly."""

from __future__ import annotations

from functools import cmp_to_key
from dataclasses import dataclass, replace

from ..consensus.economics import miner_subsidy_chipbits, node_reward_pool_chipbits
from ..consensus.merkle import merkle_root
from ..consensus.models import Block, BlockHeader, Transaction, TxOutput
from ..consensus.nodes import NodeRegistryView, select_rewarded_nodes
from ..consensus.params import ConsensusParams
from ..consensus.pow import verify_proof_of_work
from ..consensus.serialization import serialize_transaction
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
        height: int,
        miner_address: str,
        bits: int,
        mempool_entries: list[MempoolEntry],
        node_registry_view: NodeRegistryView,
        confirmed_transaction_ids: set[str] | None = None,
    ) -> BlockTemplate:
        """Construct a block template from chainstate and mempool."""

        node_pool_chipbits = node_reward_pool_chipbits(height, self.params)
        rewarded_nodes = select_rewarded_nodes(
            node_registry_view,
            height=height,
            previous_block_hash=previous_block_hash,
            node_reward_pool_chipbits=node_pool_chipbits,
            params=self.params,
        )
        provisional_coinbase = Transaction(
            version=1,
            inputs=(),
            outputs=(
                TxOutput(value=0, recipient=miner_address),
                *tuple(
                    TxOutput(value=rewarded_node.reward_chipbits, recipient=rewarded_node.payout_address)
                    for rewarded_node in rewarded_nodes
                ),
            ),
            metadata={"coinbase": "true", "height": str(height)},
        )
        coinbase_weight_units = transaction_weight_units(provisional_coinbase)
        max_transaction_weight_units = max(0, self.params.max_block_weight - coinbase_weight_units)
        selected_entries = self._select_mempool_entries(
            mempool_entries,
            max_transaction_weight_units=max_transaction_weight_units,
            confirmed_transaction_ids=confirmed_transaction_ids or set(),
        )
        total_fees_chipbits = sum(entry.fee for entry in selected_entries)
        distributed_node_reward_chipbits = sum(rewarded_node.reward_chipbits for rewarded_node in rewarded_nodes)
        miner_amount_chipbits = (
            miner_subsidy_chipbits(height, self.params)
            + total_fees_chipbits
            + (node_pool_chipbits - distributed_node_reward_chipbits)
        )
        coinbase = Transaction(
            version=1,
            inputs=(),
            outputs=(
                TxOutput(
                    value=miner_amount_chipbits,
                    recipient=miner_address,
                ),
                *tuple(
                    TxOutput(value=rewarded_node.reward_chipbits, recipient=rewarded_node.payout_address)
                    for rewarded_node in rewarded_nodes
                ),
            ),
            metadata={"coinbase": "true", "height": str(height)},
        )
        transactions = (coinbase, *(entry.transaction for entry in selected_entries))
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
        max_transaction_weight_units: int,
        confirmed_transaction_ids: set[str],
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
                if current_weight_units + selection.weight_units > max_transaction_weight_units:
                    continue
                included_entries.append(selection.entry)
                included_txids.add(selection.transaction.txid())
                current_weight_units += selection.weight_units
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
