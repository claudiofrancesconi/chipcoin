"""Hash helpers shared by consensus code."""

from __future__ import annotations

import hashlib


def sha256(data: bytes) -> bytes:
    """Return the SHA-256 digest."""

    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    """Return the Bitcoin-style double SHA-256 digest."""

    return sha256(sha256(data))


def double_sha256_hex(data: bytes) -> str:
    """Return a hexadecimal double SHA-256 digest."""

    return double_sha256(data).hex()


def hash_to_int(hash_bytes: bytes) -> int:
    """Interpret a hash digest as a big-endian integer."""

    return int.from_bytes(hash_bytes, byteorder="big", signed=False)
