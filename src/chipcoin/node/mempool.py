"""Node-local mempool admission and eviction policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..consensus.models import Transaction
from ..consensus.serialization import serialize_transaction
from ..consensus.nodes import is_special_node_transaction
from ..consensus.utxo import InMemoryUtxoView
from ..consensus.validation import ValidationError, is_coinbase_transaction, validate_transaction
from ..crypto.addresses import is_valid_address
from ..storage.chainstate import ChainStateRepository
from ..storage.mempool import MempoolEntry, MempoolRepository


@dataclass(frozen=True)
class AcceptedTransaction:
    """Accepted mempool transaction and its computed fee."""

    transaction: Transaction
    fee: int


@dataclass(frozen=True)
class MempoolPolicy:
    """Node-local policy parameters distinct from consensus validity."""

    min_fee_chipbits_normal_tx: int = 1
    max_transaction_size_bytes: int = 100_000
    max_transaction_inputs: int = 128
    max_transaction_outputs: int = 128
    max_mempool_transactions: int = 1_000
    transaction_ttl_seconds: int = 72 * 60 * 60


class MempoolManager:
    """Validate and stage unconfirmed transactions."""

    def __init__(
        self,
        *,
        repository: MempoolRepository,
        chainstate: ChainStateRepository,
        validation_context_factory,
        time_provider,
        known_chain_transaction_lookup=None,
        policy: MempoolPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.chainstate = chainstate
        self.validation_context_factory = validation_context_factory
        self.time_provider = time_provider
        self.known_chain_transaction_lookup = known_chain_transaction_lookup
        self.policy = policy or MempoolPolicy()

    def accept(self, transaction: Transaction, *, added_at: int | None = None) -> AcceptedTransaction:
        """Validate and add a transaction to the mempool."""

        if is_coinbase_transaction(transaction):
            raise ValidationError("Coinbase transactions are not valid mempool entries.")
        txid = transaction.txid()
        if self.repository.get(txid) is not None:
            raise ValidationError("Transaction is already present in the mempool.")
        if self._is_known_on_chain(txid):
            raise ValidationError("Transaction is already confirmed in the active chain.")

        self._ensure_no_mempool_double_spend(transaction)

        snapshot = self._snapshot_with_mempool_applied()
        context = self.validation_context_factory(snapshot)
        fee_chipbits = validate_transaction(transaction, context)
        self._enforce_policy(transaction, fee_chipbits)
        accepted_at = self.time_provider() if added_at is None else added_at
        self.repository.add(transaction, fee=fee_chipbits, added_at=accepted_at)
        self._prune_expired(now=self.time_provider())
        self._evict_if_needed()
        return AcceptedTransaction(transaction=transaction, fee=fee_chipbits)

    def list_transactions(self) -> list[MempoolEntry]:
        """Return current mempool entries in processing order."""

        return self.repository.list_all()

    def reconcile(self, *, extra_transactions: Iterable[Transaction] | None = None) -> None:
        """Rebuild mempool contents against the current active chain and policy."""

        preserved_entries = sorted(self.repository.list_all(), key=lambda entry: (entry.added_at, entry.transaction.txid()))
        self.repository.clear()
        for entry in preserved_entries:
            self._readmit(entry.transaction, added_at=entry.added_at)
        if extra_transactions is not None:
            now = self.time_provider()
            for transaction in extra_transactions:
                self._readmit(transaction, added_at=now)
        self._prune_expired(now=self.time_provider())
        self._evict_if_needed()

    def remove_many(self, txids: list[str]) -> None:
        """Remove confirmed or evicted transactions."""

        for txid in txids:
            self.repository.remove(txid)

    def clear(self) -> None:
        """Clear all mempool contents."""

        self.repository.clear()

    def _snapshot_with_mempool_applied(self) -> InMemoryUtxoView:
        """Build an in-memory chainstate snapshot with current mempool transactions applied."""

        view = InMemoryUtxoView.from_entries(self.chainstate.list_utxos())
        next_height = self.validation_context_factory(view).height
        for entry in self.repository.list_all():
            view.apply_transaction(entry.transaction, next_height)
        return view

    def _ensure_no_mempool_double_spend(self, transaction: Transaction) -> None:
        """Reject transactions that conflict with inputs already reserved in mempool."""

        reserved_outpoints = {
            tx_input.previous_output
            for entry in self.repository.list_all()
            for tx_input in entry.transaction.inputs
        }
        for tx_input in transaction.inputs:
            if tx_input.previous_output in reserved_outpoints:
                raise ValidationError("Transaction conflicts with an existing mempool spend.")

    def _enforce_policy(self, transaction: Transaction, fee_chipbits: int) -> None:
        """Apply mempool-standardness checks beyond pure consensus validity."""

        serialized_size = len(serialize_transaction(transaction))
        if serialized_size > self.policy.max_transaction_size_bytes:
            raise ValidationError("Transaction exceeds mempool size policy.")
        if len(transaction.inputs) > self.policy.max_transaction_inputs:
            raise ValidationError("Transaction exceeds mempool input-count policy.")
        if len(transaction.outputs) > self.policy.max_transaction_outputs:
            raise ValidationError("Transaction exceeds mempool output-count policy.")
        if fee_chipbits < 0:
            raise ValidationError("Transaction fee cannot be negative.")
        if not is_special_node_transaction(transaction) and fee_chipbits < self.policy.min_fee_chipbits_normal_tx:
            raise ValidationError("Transaction fee is below the configured mempool minimum.")

        for output in transaction.outputs:
            if int(output.value) <= 0:
                raise ValidationError("Transaction outputs must be positive for mempool policy.")
            if not is_valid_address(output.recipient):
                raise ValidationError("Transaction output recipient is not a valid CHC address.")

    def _prune_expired(self, *, now: int) -> None:
        """Remove mempool entries older than the configured TTL."""

        for entry in self.repository.list_all():
            if self._is_expired(entry.added_at, now):
                self.repository.remove(entry.transaction.txid())

    def _evict_if_needed(self) -> None:
        """Evict lowest-priority entries when the mempool exceeds capacity."""

        entries = self.repository.list_all()
        if len(entries) <= self.policy.max_mempool_transactions:
            return
        eviction_order = sorted(entries, key=lambda entry: (entry.fee, entry.added_at, entry.transaction.txid()))
        for entry in eviction_order[: len(entries) - self.policy.max_mempool_transactions]:
            self.repository.remove(entry.transaction.txid())

    def _readmit(self, transaction: Transaction, *, added_at: int) -> None:
        """Best-effort re-admission used after blocks or reorgs."""

        if self._is_expired(added_at, self.time_provider()):
            return
        try:
            self.accept(transaction, added_at=added_at)
        except ValidationError:
            return

    def _is_expired(self, added_at: int, now: int) -> bool:
        """Return whether a mempool entry has exceeded policy TTL."""

        return now - added_at >= self.policy.transaction_ttl_seconds

    def _is_known_on_chain(self, txid: str) -> bool:
        """Return whether a transaction is already confirmed in the active chain."""

        if self.known_chain_transaction_lookup is None:
            return False
        return self.known_chain_transaction_lookup(txid) is not None
