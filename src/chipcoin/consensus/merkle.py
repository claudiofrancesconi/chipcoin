"""Merkle tree helpers for transaction commitment roots."""

from __future__ import annotations

from .hashes import double_sha256


def merkle_root(transaction_ids: list[str]) -> str:
    """Compute the Merkle root from a list of transaction identifiers."""

    if not transaction_ids:
        return double_sha256(b"").hex()

    level = [bytes.fromhex(transaction_id) for transaction_id in transaction_ids]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            double_sha256(level[index] + level[index + 1])
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()
