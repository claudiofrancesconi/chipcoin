"""Repositories for block headers and chain selection metadata."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from ..consensus.models import BlockHeader
from ..consensus.serialization import deserialize_block_header, serialize_block_header


class HeaderRepository:
    """Persistence boundary for block headers."""

    def put(
        self,
        header: BlockHeader,
        *,
        height: int | None = None,
        cumulative_work: int | None = None,
        is_main_chain: bool = False,
    ) -> None:
        """Persist a header after structural validation."""

        raise NotImplementedError

    def get(self, block_hash: str) -> BlockHeader | None:
        """Return a header by block hash."""

        raise NotImplementedError

    def get_tip(self) -> "ChainTip | None":
        """Return the currently recorded chain tip."""

        raise NotImplementedError

    def set_tip(self, block_hash: str, height: int) -> None:
        """Persist the current chain tip metadata."""

        raise NotImplementedError

    def get_record(self, block_hash: str) -> "HeaderRecord | None":
        """Return stored header metadata for a block hash."""

        raise NotImplementedError

    def find_best_tip(self) -> "HeaderRecord | None":
        """Return the header with the greatest cumulative work."""

        raise NotImplementedError

    def list_locator_hashes(self, max_count: int = 32) -> tuple[str, ...]:
        """Return block locator hashes walking back from the current main tip."""

        raise NotImplementedError

    def get_headers_after(self, locator_hashes: tuple[str, ...], stop_hash: str, limit: int = 2000) -> tuple[BlockHeader, ...]:
        """Return main-chain headers after the first matching locator hash."""

        raise NotImplementedError

    def path_to_root(self, block_hash: str) -> tuple[str, ...]:
        """Return ancestor hashes from root to the given block hash."""

        raise NotImplementedError

    def set_main_chain(self, path_hashes: tuple[str, ...]) -> None:
        """Mark the provided path as the active main chain."""

        raise NotImplementedError

    def get_hash_at_height(self, height: int) -> str | None:
        """Return the main-chain block hash at a given height."""

        raise NotImplementedError


@dataclass(frozen=True)
class ChainTip:
    """Stored chain tip information."""

    block_hash: str
    height: int


@dataclass(frozen=True)
class HeaderRecord:
    """Decoded header plus stored chain metadata."""

    block_hash: str
    header: BlockHeader
    previous_block_hash: str
    height: int | None
    cumulative_work: int | None
    is_main_chain: bool


class SQLiteHeaderRepository(HeaderRepository):
    """SQLite-backed header repository."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def put(
        self,
        header: BlockHeader,
        *,
        height: int | None = None,
        cumulative_work: int | None = None,
        is_main_chain: bool = False,
    ) -> None:
        """Persist a header along with optional chain index metadata."""

        block_hash = header.block_hash()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO headers (
                    block_hash,
                    previous_block_hash,
                    merkle_root,
                    version,
                    timestamp,
                    bits,
                    nonce,
                    height,
                    cumulative_work,
                    is_main_chain,
                    raw_header
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    block_hash,
                    header.previous_block_hash,
                    header.merkle_root,
                    header.version,
                    header.timestamp,
                    header.bits,
                    header.nonce,
                    height,
                    str(cumulative_work) if cumulative_work is not None else None,
                    int(is_main_chain),
                    serialize_block_header(header),
                ),
            )

    def get(self, block_hash: str) -> BlockHeader | None:
        """Return a decoded header for a block hash when present."""

        row = self.connection.execute(
            "SELECT raw_header FROM headers WHERE block_hash = ?",
            (block_hash,),
        ).fetchone()
        if row is None:
            return None
        header, offset = deserialize_block_header(row["raw_header"])
        if offset != len(row["raw_header"]):
            raise ValueError("Stored header payload contains trailing bytes.")
        return header

    def get_tip(self) -> ChainTip | None:
        """Return the currently stored chain tip, if any."""

        row = self.connection.execute(
            "SELECT value FROM chain_meta WHERE key = 'chain_tip_hash'"
        ).fetchone()
        if row is None:
            return None
        block_hash = row["value"]
        header_row = self.connection.execute(
            "SELECT height FROM headers WHERE block_hash = ?",
            (block_hash,),
        ).fetchone()
        if header_row is None or header_row["height"] is None:
            return None
        return ChainTip(block_hash=block_hash, height=int(header_row["height"]))

    def set_tip(self, block_hash: str, height: int) -> None:
        """Persist the chain tip hash and mark the header as main chain."""

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO chain_meta(key, value) VALUES('chain_tip_hash', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (block_hash,),
            )
            self.connection.execute(
                "UPDATE headers SET height = ?, is_main_chain = 1 WHERE block_hash = ?",
                (height, block_hash),
            )

    def get_record(self, block_hash: str) -> HeaderRecord | None:
        """Return a stored header record when present."""

        row = self.connection.execute(
            """
            SELECT block_hash, previous_block_hash, height, cumulative_work, is_main_chain, raw_header
            FROM headers
            WHERE block_hash = ?
            """,
            (block_hash,),
        ).fetchone()
        if row is None:
            return None
        header, offset = deserialize_block_header(row["raw_header"])
        if offset != len(row["raw_header"]):
            raise ValueError("Stored header payload contains trailing bytes.")
        return HeaderRecord(
            block_hash=row["block_hash"],
            header=header,
            previous_block_hash=row["previous_block_hash"],
            height=None if row["height"] is None else int(row["height"]),
            cumulative_work=None if row["cumulative_work"] is None else int(row["cumulative_work"]),
            is_main_chain=bool(row["is_main_chain"]),
        )

    def find_best_tip(self) -> HeaderRecord | None:
        """Return the best-known header by cumulative work and height."""

        row = self.connection.execute(
            """
            SELECT block_hash
            FROM headers
            WHERE cumulative_work IS NOT NULL
            ORDER BY CAST(cumulative_work AS INTEGER) DESC, COALESCE(height, -1) DESC, block_hash DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self.get_record(row["block_hash"])

    def list_locator_hashes(self, max_count: int = 32) -> tuple[str, ...]:
        """Return block locator hashes from main tip backwards."""

        tip = self.get_tip()
        if tip is None:
            return ()
        hashes: list[str] = []
        current_hash = tip.block_hash
        while current_hash and len(hashes) < max_count:
            hashes.append(current_hash)
            record = self.get_record(current_hash)
            if record is None or record.previous_block_hash == "00" * 32:
                break
            current_hash = record.previous_block_hash
        return tuple(hashes)

    def get_headers_after(self, locator_hashes: tuple[str, ...], stop_hash: str, limit: int = 2000) -> tuple[BlockHeader, ...]:
        """Return active-chain headers after the first locator found."""

        start_height = -1
        if locator_hashes:
            for locator_hash in locator_hashes:
                record = self.get_record(locator_hash)
                if record is not None and record.is_main_chain and record.height is not None:
                    start_height = record.height
                    break

        rows = self.connection.execute(
            """
            SELECT block_hash, raw_header
            FROM headers
            WHERE is_main_chain = 1 AND height > ?
            ORDER BY height ASC
            LIMIT ?
            """,
            (start_height, limit),
        ).fetchall()

        result: list[BlockHeader] = []
        for row in rows:
            header, offset = deserialize_block_header(row["raw_header"])
            if offset != len(row["raw_header"]):
                raise ValueError("Stored header payload contains trailing bytes.")
            result.append(header)
            if row["block_hash"] == stop_hash:
                break
        return tuple(result)

    def path_to_root(self, block_hash: str) -> tuple[str, ...]:
        """Return ancestor hashes from root to the supplied block."""

        path: list[str] = []
        current_hash = block_hash
        while current_hash:
            record = self.get_record(current_hash)
            if record is None:
                raise ValueError(f"Unknown header in path lookup: {current_hash}")
            path.append(current_hash)
            if record.previous_block_hash == "00" * 32:
                break
            current_hash = record.previous_block_hash
        path.reverse()
        return tuple(path)

    def set_main_chain(self, path_hashes: tuple[str, ...]) -> None:
        """Mark an entire path as the active main chain and store its tip."""

        with self.connection:
            self.connection.execute("UPDATE headers SET is_main_chain = 0")
            for height, block_hash in enumerate(path_hashes):
                self.connection.execute(
                    "UPDATE headers SET is_main_chain = 1, height = ? WHERE block_hash = ?",
                    (height, block_hash),
                )
            if path_hashes:
                self.connection.execute(
                    """
                    INSERT INTO chain_meta(key, value) VALUES('chain_tip_hash', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (path_hashes[-1],),
                )

    def get_hash_at_height(self, height: int) -> str | None:
        """Return the active-chain block hash for a given height."""

        row = self.connection.execute(
            """
            SELECT block_hash
            FROM headers
            WHERE is_main_chain = 1 AND height = ?
            LIMIT 1
            """,
            (height,),
        ).fetchone()
        if row is None:
            return None
        return row["block_hash"]
