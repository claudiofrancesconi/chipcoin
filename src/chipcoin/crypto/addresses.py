"""Address derivation helpers.

Chipcoin uses a didactic address format:

- ASCII prefix ``CHC``
- Base58Check payload containing:
  - 1 version byte
  - 20-byte HASH160 of the SEC1 public key
"""

from __future__ import annotations

import hashlib

from ..consensus.hashes import double_sha256
from .keys import load_public_key


ADDRESS_PREFIX = "CHC"
ADDRESS_VERSION = 0x1C
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def hash160(payload: bytes) -> bytes:
    """Return HASH160(payload) = RIPEMD160(SHA256(payload))."""

    sha = hashlib.sha256(payload).digest()
    return hashlib.new("ripemd160", sha).digest()


def public_key_hash(public_key: bytes) -> bytes:
    """Return the 20-byte public-key hash used in addresses."""

    return hash160(public_key)


def public_key_to_address(public_key: bytes) -> str:
    """Derive a Chipcoin address from a public key."""

    _ = load_public_key(public_key)
    payload = bytes((ADDRESS_VERSION,)) + public_key_hash(public_key)
    return ADDRESS_PREFIX + _base58check_encode(payload)


def address_to_public_key_hash(address: str) -> bytes:
    """Decode a Chipcoin address and return the contained 20-byte hash."""

    if not address.startswith(ADDRESS_PREFIX):
        raise ValueError("Address does not start with the CHC prefix.")
    payload = _base58check_decode(address[len(ADDRESS_PREFIX) :])
    if len(payload) != 21:
        raise ValueError("Address payload has an unexpected length.")
    if payload[0] != ADDRESS_VERSION:
        raise ValueError("Address version byte is not recognised.")
    return payload[1:]


def is_valid_address(address: str) -> bool:
    """Return whether a string is a valid Chipcoin address."""

    try:
        address_to_public_key_hash(address)
    except ValueError:
        return False
    return True


def _base58check_encode(payload: bytes) -> str:
    """Encode payload + checksum in Base58."""

    data = payload + double_sha256(payload)[:4]
    zeros = len(data) - len(data.lstrip(b"\x00"))
    value = int.from_bytes(data, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    return ("1" * zeros) + (encoded or "1")


def _base58check_decode(value: str) -> bytes:
    """Decode Base58Check text and validate its checksum."""

    number = 0
    for character in value:
        index = _BASE58_ALPHABET.find(character)
        if index == -1:
            raise ValueError("Address contains a non-Base58 character.")
        number = number * 58 + index

    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    zeros = len(value) - len(value.lstrip("1"))
    raw = (b"\x00" * zeros) + raw
    if len(raw) < 5:
        raise ValueError("Address payload is too short.")
    payload, checksum = raw[:-4], raw[-4:]
    if double_sha256(payload)[:4] != checksum:
        raise ValueError("Address checksum is invalid.")
    return payload
