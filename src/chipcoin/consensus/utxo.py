"""UTXO state transitions and lookup abstractions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import Block, OutPoint, Transaction, TxOutput


@dataclass(frozen=True)
class UtxoEntry:
    """Stored UTXO metadata needed for contextual transaction validation."""

    output: TxOutput
    height: int
    is_coinbase: bool


class UtxoView:
    """Minimal UTXO access contract for validation and block application."""

    def get(self, outpoint: OutPoint) -> UtxoEntry | None:
        """Return an entry for a spendable outpoint when present."""

        raise NotImplementedError

    def apply_transaction(self, transaction: Transaction, height: int, *, is_coinbase: bool = False) -> None:
        """Apply a validated transaction to the underlying UTXO state."""

        raise NotImplementedError

    def apply_block(self, block: Block, height: int) -> None:
        """Apply a validated block to the underlying UTXO state."""

        raise NotImplementedError

    def clone(self) -> "UtxoView":
        """Return an isolated copy suitable for staged validation."""

        raise NotImplementedError


class InMemoryUtxoView(UtxoView):
    """Simple in-memory UTXO set for consensus tests and pure validation flows."""

    def __init__(self, entries: dict[OutPoint, UtxoEntry] | None = None) -> None:
        self._entries: dict[OutPoint, UtxoEntry] = dict(entries or {})

    @classmethod
    def from_entries(cls, entries: Iterable[tuple[OutPoint, UtxoEntry]]) -> "InMemoryUtxoView":
        """Build an in-memory view from explicit outpoint-entry pairs."""

        return cls(dict(entries))

    def get(self, outpoint: OutPoint) -> UtxoEntry | None:
        """Return a spendable entry when present."""

        return self._entries.get(outpoint)

    def add_utxo(self, outpoint: OutPoint, entry: UtxoEntry) -> None:
        """Insert or replace a UTXO entry."""

        self._entries[outpoint] = entry

    def spend_utxo(self, outpoint: OutPoint) -> UtxoEntry:
        """Remove and return a spent UTXO entry."""

        return self._entries.pop(outpoint)

    def apply_transaction(self, transaction: Transaction, height: int, *, is_coinbase: bool = False) -> None:
        """Spend referenced inputs and create new outputs for a transaction."""

        for tx_input in transaction.inputs:
            self._entries.pop(tx_input.previous_output, None)

        txid = transaction.txid()
        for index, tx_output in enumerate(transaction.outputs):
            self._entries[OutPoint(txid=txid, index=index)] = UtxoEntry(
                output=tx_output,
                height=height,
                is_coinbase=is_coinbase,
            )

    def apply_block(self, block: Block, height: int) -> None:
        """Apply all transactions from a block in order."""

        for index, transaction in enumerate(block.transactions):
            self.apply_transaction(transaction, height, is_coinbase=index == 0)

    def clone(self) -> "InMemoryUtxoView":
        """Return a copy of the current UTXO set."""

        return InMemoryUtxoView(self._entries)

    def list_entries(self) -> list[tuple[OutPoint, UtxoEntry]]:
        """Return all UTXO entries in stable order."""

        return sorted(self._entries.items(), key=lambda item: (item[0].txid, item[0].index))


class OverlayUtxoView(UtxoView):
    """Lazy staged UTXO view backed by another view.

    This avoids copying the full persisted UTXO set when validating a new
    block. Only touched outpoints and newly-created outputs are tracked.
    """

    def __init__(
        self,
        base: UtxoView,
        *,
        added: dict[OutPoint, UtxoEntry] | None = None,
        spent: set[OutPoint] | None = None,
    ) -> None:
        self._base = base
        self._added = dict(added or {})
        self._spent = set(spent or set())

    def get(self, outpoint: OutPoint) -> UtxoEntry | None:
        """Return a staged or base entry unless it was spent in this overlay."""

        if outpoint in self._spent:
            return None
        if outpoint in self._added:
            return self._added[outpoint]
        return self._base.get(outpoint)

    def apply_transaction(self, transaction: Transaction, height: int, *, is_coinbase: bool = False) -> None:
        """Stage transaction effects without mutating the base view."""

        for tx_input in transaction.inputs:
            self._added.pop(tx_input.previous_output, None)
            self._spent.add(tx_input.previous_output)

        txid = transaction.txid()
        for index, tx_output in enumerate(transaction.outputs):
            outpoint = OutPoint(txid=txid, index=index)
            self._spent.discard(outpoint)
            self._added[outpoint] = UtxoEntry(
                output=tx_output,
                height=height,
                is_coinbase=is_coinbase,
            )

    def apply_block(self, block: Block, height: int) -> None:
        """Stage all block transactions in order."""

        for index, transaction in enumerate(block.transactions):
            self.apply_transaction(transaction, height, is_coinbase=index == 0)

    def clone(self) -> "UtxoView":
        """Return an isolated overlay copy over the same immutable base."""

        return OverlayUtxoView(self._base, added=self._added, spent=self._spent)
