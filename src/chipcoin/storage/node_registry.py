"""Repositories for consensus-visible on-chain node registry state."""

from __future__ import annotations

from sqlite3 import Connection

from ..consensus.nodes import InMemoryNodeRegistryView, NodeRecord, NodeRegistryView


class NodeRegistryRepository(NodeRegistryView):
    """Persistence boundary for node registry state."""

    def replace_all(self, records: list[NodeRecord]) -> None:
        raise NotImplementedError


class SQLiteNodeRegistryRepository(NodeRegistryRepository):
    """SQLite-backed node registry repository."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def get_by_node_id(self, node_id: str) -> NodeRecord | None:
        row = self.connection.execute(
            """
            SELECT node_id, payout_address, owner_pubkey, registered_height, last_renewed_height
            FROM node_registry
            WHERE node_id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return NodeRecord(
            node_id=row["node_id"],
            payout_address=row["payout_address"],
            owner_pubkey=bytes.fromhex(row["owner_pubkey"]),
            registered_height=int(row["registered_height"]),
            last_renewed_height=int(row["last_renewed_height"]),
        )

    def get_by_owner_pubkey(self, owner_pubkey: bytes) -> NodeRecord | None:
        row = self.connection.execute(
            """
            SELECT node_id, payout_address, owner_pubkey, registered_height, last_renewed_height
            FROM node_registry
            WHERE owner_pubkey = ?
            """,
            (owner_pubkey.hex(),),
        ).fetchone()
        if row is None:
            return None
        return NodeRecord(
            node_id=row["node_id"],
            payout_address=row["payout_address"],
            owner_pubkey=bytes.fromhex(row["owner_pubkey"]),
            registered_height=int(row["registered_height"]),
            last_renewed_height=int(row["last_renewed_height"]),
        )

    def upsert(self, record: NodeRecord) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO node_registry(
                    node_id,
                    payout_address,
                    owner_pubkey,
                    registered_height,
                    last_renewed_height
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.node_id,
                    record.payout_address,
                    record.owner_pubkey.hex(),
                    record.registered_height,
                    record.last_renewed_height,
                ),
            )

    def list_records(self) -> list[NodeRecord]:
        rows = self.connection.execute(
            """
            SELECT node_id, payout_address, owner_pubkey, registered_height, last_renewed_height
            FROM node_registry
            ORDER BY node_id
            """
        ).fetchall()
        return [
            NodeRecord(
                node_id=row["node_id"],
                payout_address=row["payout_address"],
                owner_pubkey=bytes.fromhex(row["owner_pubkey"]),
                registered_height=int(row["registered_height"]),
                last_renewed_height=int(row["last_renewed_height"]),
            )
            for row in rows
        ]

    def replace_all(self, records: list[NodeRecord]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM node_registry")
            self.connection.executemany(
                """
                INSERT INTO node_registry(
                    node_id,
                    payout_address,
                    owner_pubkey,
                    registered_height,
                    last_renewed_height
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.node_id,
                        record.payout_address,
                        record.owner_pubkey.hex(),
                        record.registered_height,
                        record.last_renewed_height,
                    )
                    for record in records
                ],
            )

    def clone(self) -> NodeRegistryView:
        raise NotImplementedError("Use an in-memory node registry snapshot for staged validation.")

    def snapshot(self) -> InMemoryNodeRegistryView:
        """Return an in-memory clone of the persisted registry."""

        return InMemoryNodeRegistryView.from_records(self.list_records())
