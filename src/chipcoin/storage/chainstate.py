"""Repositories for chainstate and UTXO persistence."""

from __future__ import annotations

from sqlite3 import Connection

from ..consensus.models import ChipbitAmount, OutPoint
from ..consensus.utxo import UtxoEntry, UtxoView
from ..consensus.models import TxOutput


class ChainStateRepository:
    """Persistence boundary for contextual chainstate data."""

    def get_utxo(self, outpoint: OutPoint) -> UtxoEntry | None:
        """Return a UTXO entry when present."""

        raise NotImplementedError

    def put_utxo(self, outpoint: OutPoint, entry: UtxoEntry) -> None:
        """Persist a single UTXO entry."""

        raise NotImplementedError

    def spend_utxo(self, outpoint: OutPoint) -> None:
        """Remove a spent UTXO entry."""

        raise NotImplementedError

    def list_utxos(self) -> list[tuple[OutPoint, UtxoEntry]]:
        """Return all persisted UTXO entries."""

        raise NotImplementedError

    def replace_all(self, entries: list[tuple[OutPoint, UtxoEntry]]) -> None:
        """Replace the entire persisted UTXO set."""

        raise NotImplementedError


class SQLiteChainStateRepository(ChainStateRepository, UtxoView):
    """SQLite-backed chainstate repository that also satisfies the UTXO view contract."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def get_utxo(self, outpoint: OutPoint) -> UtxoEntry | None:
        """Return a UTXO entry when present."""

        row = self.connection.execute(
            """
            SELECT value, recipient, height, is_coinbase
            FROM utxos
            WHERE txid = ? AND output_index = ?
            """,
            (outpoint.txid, outpoint.index),
        ).fetchone()
        if row is None:
            return None
        return UtxoEntry(
            output=TxOutput(value=ChipbitAmount(int(row["value"])), recipient=row["recipient"]),
            height=int(row["height"]),
            is_coinbase=bool(row["is_coinbase"]),
        )

    def get(self, outpoint: OutPoint) -> UtxoEntry | None:
        """Bridge method for the consensus UTXO view contract."""

        return self.get_utxo(outpoint)

    def put_utxo(self, outpoint: OutPoint, entry: UtxoEntry) -> None:
        """Persist or replace a UTXO entry."""

        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO utxos(
                    txid,
                    output_index,
                    value,
                    recipient,
                    height,
                    is_coinbase
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    outpoint.txid,
                    outpoint.index,
                    int(entry.output.value),
                    entry.output.recipient,
                    entry.height,
                    int(entry.is_coinbase),
                ),
            )

    def spend_utxo(self, outpoint: OutPoint) -> None:
        """Delete a UTXO entry for a spent output."""

        with self.connection:
            self.connection.execute(
                "DELETE FROM utxos WHERE txid = ? AND output_index = ?",
                (outpoint.txid, outpoint.index),
            )

    def list_utxos(self) -> list[tuple[OutPoint, UtxoEntry]]:
        """Return all stored UTXOs for snapshot-based validation."""

        rows = self.connection.execute(
            """
            SELECT txid, output_index, value, recipient, height, is_coinbase
            FROM utxos
            ORDER BY txid, output_index
            """
        ).fetchall()
        return [
            (
                OutPoint(txid=row["txid"], index=int(row["output_index"])),
                UtxoEntry(
                    output=TxOutput(value=ChipbitAmount(int(row["value"])), recipient=row["recipient"]),
                    height=int(row["height"]),
                    is_coinbase=bool(row["is_coinbase"]),
                ),
            )
            for row in rows
        ]

    def replace_all(self, entries: list[tuple[OutPoint, UtxoEntry]]) -> None:
        """Replace the entire persisted UTXO set atomically."""

        with self.connection:
            self.connection.execute("DELETE FROM utxos")
            self.connection.executemany(
                """
                INSERT INTO utxos(txid, output_index, value, recipient, height, is_coinbase)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        outpoint.txid,
                        outpoint.index,
                        int(entry.output.value),
                        entry.output.recipient,
                        entry.height,
                        int(entry.is_coinbase),
                    )
                    for outpoint, entry in entries
                ],
            )

    def apply_transaction(self, transaction, height: int, *, is_coinbase: bool = False) -> None:
        """Apply a transaction to persisted UTXO state."""

        for tx_input in transaction.inputs:
            self.spend_utxo(tx_input.previous_output)
        txid = transaction.txid()
        for index, tx_output in enumerate(transaction.outputs):
            self.put_utxo(
                OutPoint(txid=txid, index=index),
                UtxoEntry(output=tx_output, height=height, is_coinbase=is_coinbase),
            )

    def apply_block(self, block, height: int) -> None:
        """Apply a block's transactions in order."""

        for index, transaction in enumerate(block.transactions):
            self.apply_transaction(transaction, height, is_coinbase=index == 0)

    def clone(self) -> UtxoView:
        """SQLite-backed UTXO view is not clonable as a pure in-memory snapshot."""

        raise NotImplementedError("Use an in-memory view for staged validation snapshots.")
