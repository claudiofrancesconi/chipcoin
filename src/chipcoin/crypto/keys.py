"""Key generation, validation, and SEC1 serialization helpers."""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


SECP256K1_ORDER = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)


def validate_private_key(private_key: bytes) -> None:
    """Validate raw 32-byte secp256k1 private key material."""

    if len(private_key) != 32:
        raise ValueError("Private keys must be exactly 32 bytes.")
    secret = int.from_bytes(private_key, "big")
    if secret <= 0 or secret >= SECP256K1_ORDER:
        raise ValueError("Private key is outside the valid secp256k1 range.")


def generate_private_key() -> bytes:
    """Generate a secp256k1 private key."""

    while True:
        candidate = secrets.token_bytes(32)
        try:
            validate_private_key(candidate)
        except ValueError:
            continue
        return candidate


def derive_public_key(private_key: bytes, compressed: bool = True) -> bytes:
    """Derive a public key from a private key."""

    validate_private_key(private_key)
    key = ec.derive_private_key(int.from_bytes(private_key, "big"), ec.SECP256K1())
    public_key = key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint
        if compressed
        else serialization.PublicFormat.UncompressedPoint,
    )


def serialize_private_key_hex(private_key: bytes) -> str:
    """Encode raw private key bytes as lowercase hex."""

    validate_private_key(private_key)
    return private_key.hex()


def parse_private_key_hex(value: str) -> bytes:
    """Decode a hex private key string."""

    try:
        private_key = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("Private key hex is invalid.") from exc
    validate_private_key(private_key)
    return private_key


def serialize_public_key_hex(public_key: bytes) -> str:
    """Encode a SEC1 public key as lowercase hex."""

    _ = load_public_key(public_key)
    return public_key.hex()


def parse_public_key_hex(value: str) -> bytes:
    """Decode and validate a SEC1 public key from hex."""

    try:
        public_key = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("Public key hex is invalid.") from exc
    _ = load_public_key(public_key)
    return public_key


def load_private_key(private_key: bytes) -> ec.EllipticCurvePrivateKey:
    """Load raw 32-byte private key material into a cryptography object."""

    validate_private_key(private_key)
    return ec.derive_private_key(int.from_bytes(private_key, "big"), ec.SECP256K1())


def load_public_key(public_key: bytes) -> ec.EllipticCurvePublicKey:
    """Load a SEC1 compressed or uncompressed secp256k1 public key."""

    try:
        loaded = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), public_key)
    except ValueError as exc:
        raise ValueError("Public key bytes are not a valid secp256k1 SEC1 point.") from exc
    if not isinstance(loaded.curve, ec.SECP256K1):
        raise ValueError("Public key does not use the secp256k1 curve.")
    return loaded
