"""P2P message definitions for the Chipcoin node protocol.

The protocol uses a Bitcoin-inspired framing layer and typed binary payloads.
This module defines the message payload structures independent from transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..consensus.models import Block, BlockHeader, Transaction


CommandName = Literal[
    "version",
    "verack",
    "ping",
    "pong",
    "getaddr",
    "addr",
    "inv",
    "getdata",
    "tx",
    "block",
    "getheaders",
    "headers",
    "getblocks",
]

InventoryType = Literal["tx", "block"]


@dataclass(frozen=True)
class EmptyPayload:
    """Empty payload used by messages with no body."""


@dataclass(frozen=True)
class VersionMessage:
    """Initial handshake message used to announce local node state."""

    protocol_version: int
    network: str
    node_id: str
    start_height: int
    user_agent: str
    relay: bool = True


@dataclass(frozen=True)
class PingMessage:
    """Liveness check with a caller-defined nonce."""

    nonce: int


@dataclass(frozen=True)
class PongMessage:
    """Reply to a ping using the same nonce."""

    nonce: int


@dataclass(frozen=True)
class InventoryVector:
    """Announcement or request reference for an object on the network."""

    object_type: InventoryType
    object_hash: str


@dataclass(frozen=True)
class InvMessage:
    """Inventory announcement for one or more objects."""

    items: tuple[InventoryVector, ...]


@dataclass(frozen=True)
class GetDataMessage:
    """Request concrete objects by inventory reference."""

    items: tuple[InventoryVector, ...]


@dataclass(frozen=True)
class TransactionMessage:
    """Direct transaction relay payload."""

    transaction: Transaction


@dataclass(frozen=True)
class BlockMessage:
    """Direct block relay payload."""

    block: Block


@dataclass(frozen=True)
class GetHeadersMessage:
    """Request headers using a block locator and optional stop hash."""

    protocol_version: int
    locator_hashes: tuple[str, ...]
    stop_hash: str


@dataclass(frozen=True)
class HeadersMessage:
    """Reply containing one or more block headers."""

    headers: tuple[BlockHeader, ...]


@dataclass(frozen=True)
class GetBlocksMessage:
    """Request blocks using a block locator and optional stop hash."""

    protocol_version: int
    locator_hashes: tuple[str, ...]
    stop_hash: str


@dataclass(frozen=True)
class PeerAddress:
    """Compact peer record used by `addr` messages."""

    host: str
    port: int
    services: int = 0
    timestamp: int = 0


@dataclass(frozen=True)
class AddrMessage:
    """Reply containing peer address candidates."""

    addresses: tuple[PeerAddress, ...]


MessagePayload = (
    EmptyPayload
    | VersionMessage
    | PingMessage
    | PongMessage
    | InvMessage
    | GetDataMessage
    | TransactionMessage
    | BlockMessage
    | GetHeadersMessage
    | HeadersMessage
    | GetBlocksMessage
    | AddrMessage
)


@dataclass(frozen=True)
class MessageEnvelope:
    """Typed wire-level message envelope before framing."""

    command: CommandName
    payload: MessagePayload
