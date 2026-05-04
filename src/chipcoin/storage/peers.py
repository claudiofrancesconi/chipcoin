"""Repositories for discovered and persisted peers."""

from __future__ import annotations

from sqlite3 import Connection

from ..node.peers import PeerInfo


class PeerRepository:
    """Persistence boundary for peer discovery records."""

    def add(self, peer: PeerInfo) -> None:
        """Persist a peer endpoint."""

        raise NotImplementedError

    def list_known(self, *, network: str | None = None) -> list[PeerInfo]:
        """Return known peer endpoints."""

        raise NotImplementedError

    def observe(self, peer: PeerInfo) -> None:
        """Persist an observed peer state update."""

        raise NotImplementedError

    def remove(self, *, host: str, port: int, network: str) -> None:
        """Delete one persisted peer endpoint."""

        raise NotImplementedError

    def reset_session_state(self, *, network: str) -> None:
        """Mark persisted peer sessions as disconnected for one network."""

        raise NotImplementedError


class SQLitePeerRepository(PeerRepository):
    """SQLite-backed repository for peer endpoints."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def add(self, peer: PeerInfo) -> None:
        """Persist a peer endpoint idempotently."""

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO peers(
                    host,
                    port,
                    network,
                    source,
                    first_seen,
                    direction,
                    last_seen,
                    last_success,
                    last_failure,
                    failure_count,
                    success_count,
                    handshake_complete,
                    last_known_height,
                    node_id,
                    score,
                    reconnect_attempts,
                    backoff_until,
                    last_error,
                    last_error_at,
                    protocol_error_class,
                    disconnect_count,
                    session_started_at,
                    misbehavior_score,
                    misbehavior_last_updated_at,
                    ban_until,
                    last_penalty_reason,
                    last_penalty_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host, port, network) DO UPDATE SET
                    source = COALESCE(excluded.source, peers.source),
                    first_seen = COALESCE(peers.first_seen, excluded.first_seen),
                    direction = COALESCE(excluded.direction, peers.direction),
                    last_seen = COALESCE(excluded.last_seen, peers.last_seen),
                    last_success = COALESCE(excluded.last_success, peers.last_success),
                    last_failure = COALESCE(excluded.last_failure, peers.last_failure),
                    failure_count = COALESCE(excluded.failure_count, peers.failure_count),
                    success_count = COALESCE(excluded.success_count, peers.success_count),
                    handshake_complete = COALESCE(excluded.handshake_complete, peers.handshake_complete),
                    last_known_height = COALESCE(excluded.last_known_height, peers.last_known_height),
                    node_id = COALESCE(excluded.node_id, peers.node_id),
                    score = COALESCE(excluded.score, peers.score),
                    reconnect_attempts = COALESCE(excluded.reconnect_attempts, peers.reconnect_attempts),
                    backoff_until = COALESCE(excluded.backoff_until, peers.backoff_until),
                    last_error = COALESCE(excluded.last_error, peers.last_error),
                    last_error_at = COALESCE(excluded.last_error_at, peers.last_error_at),
                    protocol_error_class = COALESCE(excluded.protocol_error_class, peers.protocol_error_class),
                    disconnect_count = COALESCE(excluded.disconnect_count, peers.disconnect_count),
                    session_started_at = COALESCE(excluded.session_started_at, peers.session_started_at),
                    misbehavior_score = COALESCE(excluded.misbehavior_score, peers.misbehavior_score),
                    misbehavior_last_updated_at = COALESCE(
                        excluded.misbehavior_last_updated_at,
                        peers.misbehavior_last_updated_at
                    ),
                    ban_until = COALESCE(excluded.ban_until, peers.ban_until),
                    last_penalty_reason = COALESCE(excluded.last_penalty_reason, peers.last_penalty_reason),
                    last_penalty_at = COALESCE(excluded.last_penalty_at, peers.last_penalty_at)
                """,
                (
                    peer.host,
                    peer.port,
                    peer.network,
                    peer.source,
                    peer.first_seen,
                    peer.direction,
                    peer.last_seen,
                    peer.last_success,
                    peer.last_failure,
                    peer.failure_count,
                    peer.success_count,
                    None if peer.handshake_complete is None else int(peer.handshake_complete),
                    peer.last_known_height,
                    peer.node_id,
                    peer.score,
                    peer.reconnect_attempts,
                    peer.backoff_until,
                    peer.last_error,
                    peer.last_error_at,
                    peer.protocol_error_class,
                    peer.disconnect_count,
                    peer.session_started_at,
                    peer.misbehavior_score,
                    peer.misbehavior_last_updated_at,
                    peer.ban_until,
                    peer.last_penalty_reason,
                    peer.last_penalty_at,
                ),
            )

    def list_known(self, *, network: str | None = None) -> list[PeerInfo]:
        """Return known peers, optionally filtered by network."""

        if network is None:
            rows = self.connection.execute(
                """
                SELECT
                    host,
                    port,
                    network,
                    source,
                    first_seen,
                    direction,
                    last_seen,
                    last_success,
                    last_failure,
                    failure_count,
                    success_count,
                    handshake_complete,
                    last_known_height,
                    node_id,
                    score,
                    reconnect_attempts,
                    backoff_until,
                    last_error,
                    last_error_at,
                    protocol_error_class,
                    disconnect_count,
                    session_started_at,
                    misbehavior_score,
                    misbehavior_last_updated_at,
                    ban_until,
                    last_penalty_reason,
                    last_penalty_at
                FROM peers
                ORDER BY network, host, port
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT
                    host,
                    port,
                    network,
                    source,
                    first_seen,
                    direction,
                    last_seen,
                    last_success,
                    last_failure,
                    failure_count,
                    success_count,
                    handshake_complete,
                    last_known_height,
                    node_id,
                    score,
                    reconnect_attempts,
                    backoff_until,
                    last_error,
                    last_error_at,
                    protocol_error_class,
                    disconnect_count,
                    session_started_at,
                    misbehavior_score,
                    misbehavior_last_updated_at,
                    ban_until,
                    last_penalty_reason,
                    last_penalty_at
                FROM peers
                WHERE network = ?
                ORDER BY host, port
                """,
                (network,),
            ).fetchall()
        return [
            PeerInfo(
                host=row["host"],
                port=int(row["port"]),
                network=row["network"],
                source=row["source"],
                first_seen=None if row["first_seen"] is None else int(row["first_seen"]),
                direction=row["direction"],
                last_seen=None if row["last_seen"] is None else int(row["last_seen"]),
                last_success=None if row["last_success"] is None else int(row["last_success"]),
                last_failure=None if row["last_failure"] is None else int(row["last_failure"]),
                failure_count=None if row["failure_count"] is None else int(row["failure_count"]),
                success_count=None if row["success_count"] is None else int(row["success_count"]),
                handshake_complete=None if row["handshake_complete"] is None else bool(row["handshake_complete"]),
                last_known_height=None if row["last_known_height"] is None else int(row["last_known_height"]),
                node_id=row["node_id"],
                score=None if row["score"] is None else int(row["score"]),
                reconnect_attempts=None if row["reconnect_attempts"] is None else int(row["reconnect_attempts"]),
                backoff_until=None if row["backoff_until"] is None else int(row["backoff_until"]),
                last_error=row["last_error"],
                last_error_at=None if row["last_error_at"] is None else int(row["last_error_at"]),
                protocol_error_class=row["protocol_error_class"],
                disconnect_count=None if row["disconnect_count"] is None else int(row["disconnect_count"]),
                session_started_at=None if row["session_started_at"] is None else int(row["session_started_at"]),
                misbehavior_score=None if row["misbehavior_score"] is None else int(row["misbehavior_score"]),
                misbehavior_last_updated_at=(
                    None
                    if row["misbehavior_last_updated_at"] is None
                    else int(row["misbehavior_last_updated_at"])
                ),
                ban_until=None if row["ban_until"] is None else int(row["ban_until"]),
                last_penalty_reason=row["last_penalty_reason"],
                last_penalty_at=None if row["last_penalty_at"] is None else int(row["last_penalty_at"]),
            )
            for row in rows
        ]

    def observe(self, peer: PeerInfo) -> None:
        """Persist the latest session metadata for a peer."""

        self.add(peer)

    def remove(self, *, host: str, port: int, network: str) -> None:
        """Delete one persisted peer endpoint."""

        with self.connection:
            self.connection.execute(
                "DELETE FROM peers WHERE host = ? AND port = ? AND network = ?",
                (host, port, network),
            )

    def reset_session_state(self, *, network: str) -> None:
        """Clear runtime-only session markers after a node process restart."""

        with self.connection:
            self.connection.execute(
                """
                UPDATE peers
                SET handshake_complete = 0,
                    session_started_at = NULL
                WHERE network = ?
                  AND handshake_complete = 1
                """,
                (network,),
            )
