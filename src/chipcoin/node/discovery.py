"""Peer discovery flows using local records and optional bootstrap seeds."""

from __future__ import annotations


class DiscoveryService:
    """Coordinate initial peer discovery without coupling it to consensus."""

    def discover(self) -> list[str]:
        """Return candidate peer endpoints from configured sources."""

        raise NotImplementedError
