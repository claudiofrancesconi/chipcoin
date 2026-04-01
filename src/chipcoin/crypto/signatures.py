"""ECDSA signing and verification helpers."""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

from .keys import SECP256K1_ORDER, load_private_key, load_public_key


def _normalize_low_s(signature: bytes) -> bytes:
    """Return a DER signature normalized to low-S form."""

    r, s = asym_utils.decode_dss_signature(signature)
    if s > SECP256K1_ORDER // 2:
        s = SECP256K1_ORDER - s
    return asym_utils.encode_dss_signature(r, s)


def sign_digest(private_key: bytes, digest: bytes) -> bytes:
    """Produce a deterministic ECDSA signature for a digest."""

    if len(digest) != 32:
        raise ValueError("ECDSA signing expects a 32-byte digest.")
    signature = load_private_key(private_key).sign(digest, ec.ECDSA(asym_utils.Prehashed(hashes.SHA256())))
    return _normalize_low_s(signature)


def verify_digest(public_key: bytes, digest: bytes, signature: bytes) -> bool:
    """Verify an ECDSA signature over a digest."""

    if len(digest) != 32:
        return False
    try:
        normalized = _normalize_low_s(signature)
    except ValueError:
        return False
    if normalized != signature:
        return False
    try:
        load_public_key(public_key).verify(signature, digest, ec.ECDSA(asym_utils.Prehashed(hashes.SHA256())))
    except Exception:
        return False
    return True
