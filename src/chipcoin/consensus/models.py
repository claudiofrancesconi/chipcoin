"""Consensus object models kept independent from networking and storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NewType


ChipbitAmount = NewType("ChipbitAmount", int)


@dataclass(frozen=True)
class OutPoint:
    """Reference to a specific previous transaction output."""

    txid: str
    index: int


@dataclass(frozen=True)
class TxInput:
    """Transaction input spending a previous unspent output."""

    previous_output: OutPoint
    signature: bytes = b""
    public_key: bytes = b""
    sequence: int = 0xFFFFFFFF


@dataclass(frozen=True)
class TxOutput:
    """Transaction output locking value to an address-like recipient."""

    value: ChipbitAmount
    recipient: str


@dataclass(frozen=True)
class Transaction:
    """Consensus transaction with deterministic serialization requirements."""

    version: int
    inputs: tuple[TxInput, ...]
    outputs: tuple[TxOutput, ...]
    locktime: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def txid(self) -> str:
        """Return the deterministic transaction identifier."""

        from .hashes import double_sha256_hex
        from .serialization import serialize_transaction

        return double_sha256_hex(serialize_transaction(self))


@dataclass(frozen=True)
class BlockHeader:
    """Block header used for hashing, work comparison, and synchronization."""

    version: int
    previous_block_hash: str
    merkle_root: str
    timestamp: int
    bits: int
    nonce: int

    def block_hash(self) -> str:
        """Return the deterministic header hash."""

        from .hashes import double_sha256_hex
        from .serialization import serialize_block_header

        return double_sha256_hex(serialize_block_header(self))


@dataclass(frozen=True)
class Block:
    """Full block with header and transaction list."""

    header: BlockHeader
    transactions: tuple[Transaction, ...]

    def block_hash(self) -> str:
        """Return the hash of the contained header."""

        return self.header.block_hash()
