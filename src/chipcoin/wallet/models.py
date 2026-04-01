"""Wallet-side models kept separate from core node internals."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpendCandidate:
    """Wallet-side representation of a spendable output."""

    txid: str
    index: int
    amount_chipbits: int
    recipient: str

    @property
    def value(self) -> int:
        """Backward-compatible alias for amount_chipbits."""

        return self.amount_chipbits


@dataclass(frozen=True)
class WalletKey:
    """Minimal single-key wallet material."""

    private_key: bytes
    public_key: bytes
    address: str
    compressed: bool = True


@dataclass(frozen=True)
class SelectionResult:
    """Coin selection result with deterministic change calculation."""

    selected: tuple[SpendCandidate, ...]
    total_input_chipbits: int
    change_chipbits: int

    @property
    def total_input_value(self) -> int:
        """Backward-compatible alias for total_input_chipbits."""

        return self.total_input_chipbits

    @property
    def change_value(self) -> int:
        """Backward-compatible alias for change_chipbits."""

        return self.change_chipbits


@dataclass(frozen=True)
class BuiltTransaction:
    """Signed wallet transaction plus fee and change metadata."""

    transaction: object
    fee_chipbits: int
    change_chipbits: int

    @property
    def fee(self) -> int:
        """Backward-compatible alias for fee_chipbits."""

        return self.fee_chipbits

    @property
    def change_value(self) -> int:
        """Backward-compatible alias for change_chipbits."""

        return self.change_chipbits
