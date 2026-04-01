"""Peer manager interfaces and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass

from .p2p.errors import protocol_error_class


@dataclass(frozen=True)
class PeerInfo:
    """Minimal peer identity and connectivity state."""

    host: str
    port: int
    network: str
    direction: str | None = None
    last_seen: int | None = None
    handshake_complete: bool | None = None
    last_known_height: int | None = None
    node_id: str | None = None
    score: int | None = None
    reconnect_attempts: int | None = None
    backoff_until: int | None = None
    last_error: str | None = None
    last_error_at: int | None = None
    protocol_error_class: str | None = None
    disconnect_count: int | None = None
    session_started_at: int | None = None


def classify_peer_error(error: Exception | str | None) -> str | None:
    """Map peer/runtime errors into stable diagnostic classes."""

    return protocol_error_class(error)


class PeerManager:
    """Manage known peers and active sessions."""

    def __init__(self) -> None:
        self._peers: dict[tuple[str, int, str], PeerInfo] = {}

    def add(self, peer: PeerInfo) -> None:
        """Add a peer to the local peerbook."""

        key = (peer.host, peer.port, peer.network)
        existing = self._peers.get(key)
        if existing is None:
            self._peers[key] = peer
            return
        self._peers[key] = PeerInfo(
            host=peer.host,
            port=peer.port,
            network=peer.network,
            direction=peer.direction if peer.direction is not None else existing.direction,
            last_seen=peer.last_seen if peer.last_seen is not None else existing.last_seen,
            handshake_complete=(
                peer.handshake_complete if peer.handshake_complete is not None else existing.handshake_complete
            ),
            last_known_height=peer.last_known_height if peer.last_known_height is not None else existing.last_known_height,
            node_id=peer.node_id if peer.node_id is not None else existing.node_id,
            score=peer.score if peer.score is not None else existing.score,
            reconnect_attempts=(
                peer.reconnect_attempts if peer.reconnect_attempts is not None else existing.reconnect_attempts
            ),
            backoff_until=peer.backoff_until if peer.backoff_until is not None else existing.backoff_until,
            last_error=peer.last_error if peer.last_error is not None else existing.last_error,
            last_error_at=peer.last_error_at if peer.last_error_at is not None else existing.last_error_at,
            protocol_error_class=(
                peer.protocol_error_class if peer.protocol_error_class is not None else existing.protocol_error_class
            ),
            disconnect_count=peer.disconnect_count if peer.disconnect_count is not None else existing.disconnect_count,
            session_started_at=peer.session_started_at if peer.session_started_at is not None else existing.session_started_at,
        )

    def remove(self, peer: PeerInfo) -> None:
        """Remove a peer from the local peerbook when present."""

        self._peers.pop((peer.host, peer.port, peer.network), None)

    def list_all(self, *, network: str | None = None) -> list[PeerInfo]:
        """Return known peers, optionally filtered by network."""

        peers = self._peers.values()
        if network is not None:
            peers = [peer for peer in peers if peer.network == network]
        return sorted(peers, key=lambda peer: (peer.network, peer.host, peer.port))
