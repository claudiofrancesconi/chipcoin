"""Repositories for pending transactions."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from ..consensus.models import Transaction
from ..consensus.serialization import deserialize_transaction, serialize_transaction


@dataclass(frozen=True)
class MempoolEntry:
    """Stored mempool transaction metadata."""

    transaction: Transaction
    fee: int
    added_at: int


class MempoolRepository:
    """Persistence boundary for node-local mempool contents."""

    def add(self, transaction: Transaction, *, fee: int, added_at: int) -> None:
        """Persist a transaction accepted into the mempool."""

        raise NotImplementedError

    def get(self, txid: str) -> MempoolEntry | None:
        """Return a stored mempool transaction when present."""

        raise NotImplementedError

    def list_all(self) -> list[MempoolEntry]:
        """Return all mempool entries in stable processing order."""

        raise NotImplementedError

    def remove(self, txid: str) -> None:
        """Remove a transaction from the mempool."""

        raise NotImplementedError

    def clear(self) -> None:
        """Delete all mempool entries."""

        raise NotImplementedError


class SQLiteMempoolRepository(MempoolRepository):
    """SQLite-backed mempool repository."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def add(self, transaction: Transaction, *, fee: int, added_at: int) -> None:
        """Persist a mempool transaction and associated metadata."""

        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO mempool_transactions(txid, raw_transaction, fee, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (transaction.txid(), serialize_transaction(transaction), fee, added_at),
            )

    def get(self, txid: str) -> MempoolEntry | None:
        """Return a decoded mempool entry when present."""

        row = self.connection.execute(
            """
            SELECT raw_transaction, fee, added_at
            FROM mempool_transactions
            WHERE txid = ?
            """,
            (txid,),
        ).fetchone()
        if row is None:
            return None
        transaction, offset = deserialize_transaction(row["raw_transaction"])
        if offset != len(row["raw_transaction"]):
            raise ValueError("Stored mempool transaction contains trailing bytes.")
        return MempoolEntry(transaction=transaction, fee=int(row["fee"]), added_at=int(row["added_at"]))

    def list_all(self) -> list[MempoolEntry]:
        """Return all mempool entries ordered by insertion time and txid."""

        rows = self.connection.execute(
            """
            SELECT raw_transaction, fee, added_at
            FROM mempool_transactions
            ORDER BY added_at, txid
            """
        ).fetchall()
        entries: list[MempoolEntry] = []
        for row in rows:
            transaction, offset = deserialize_transaction(row["raw_transaction"])
            if offset != len(row["raw_transaction"]):
                raise ValueError("Stored mempool transaction contains trailing bytes.")
            entries.append(
                MempoolEntry(
                    transaction=transaction,
                    fee=int(row["fee"]),
                    added_at=int(row["added_at"]),
                )
            )
        return entries

    def remove(self, txid: str) -> None:
        """Delete a transaction from the persisted mempool."""

        with self.connection:
            self.connection.execute("DELETE FROM mempool_transactions WHERE txid = ?", (txid,))

    def clear(self) -> None:
        """Delete all persisted mempool entries."""

        with self.connection:
            self.connection.execute("DELETE FROM mempool_transactions")
