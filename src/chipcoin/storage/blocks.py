"""Repositories for raw blocks and block index state."""

from __future__ import annotations

from sqlite3 import Connection

from ..consensus.models import Block
from ..consensus.serialization import deserialize_block, serialize_block


class BlockRepository:
    """Persistence boundary for raw blocks and block metadata."""

    def put(self, block: Block) -> None:
        """Persist a validated block."""

        raise NotImplementedError

    def get(self, block_hash: str) -> Block | None:
        """Return a decoded block by hash when present."""

        raise NotImplementedError


class SQLiteBlockRepository(BlockRepository):
    """SQLite-backed repository for raw block payloads."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def put(self, block: Block) -> None:
        """Persist a full block payload."""

        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO blocks(block_hash, raw_block)
                VALUES (?, ?)
                """,
                (block.block_hash(), serialize_block(block)),
            )

    def get(self, block_hash: str) -> Block | None:
        """Return a decoded block when present."""

        row = self.connection.execute(
            "SELECT raw_block FROM blocks WHERE block_hash = ?",
            (block_hash,),
        ).fetchone()
        if row is None:
            return None
        block, offset = deserialize_block(row["raw_block"])
        if offset != len(row["raw_block"]):
            raise ValueError("Stored block payload contains trailing bytes.")
        return block
