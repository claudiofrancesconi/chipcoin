"""Proof-of-work target and validation helpers."""

from __future__ import annotations

from .params import ConsensusParams
from .hashes import hash_to_int
from .models import BlockHeader
from .serialization import serialize_block_header


def bits_to_target(bits: int) -> int:
    """Convert compact difficulty bits into an integer target."""

    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    is_negative = bool(bits & 0x00800000)

    if bits == 0 or is_negative or mantissa == 0:
        raise ValueError("Invalid compact target representation.")

    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def target_to_bits(target: int) -> int:
    """Convert an integer target into compact difficulty bits."""

    if target <= 0:
        raise ValueError("Target must be positive.")

    size = (target.bit_length() + 7) // 8
    if size <= 3:
        mantissa = target << (8 * (3 - size))
    else:
        mantissa = target >> (8 * (size - 3))

    if mantissa & 0x00800000:
        mantissa >>= 8
        size += 1

    return (size << 24) | (mantissa & 0x007FFFFF)


def header_work(header: BlockHeader) -> int:
    """Return the cumulative-work contribution of a header."""

    target = bits_to_target(header.bits)
    return (1 << 256) // (target + 1)


def verify_proof_of_work(header: BlockHeader) -> bool:
    """Validate that a header hash satisfies its declared target."""

    target = bits_to_target(header.bits)
    header_hash = hash_to_int(header_hash_bytes(header))
    return 0 < target < (1 << 256) and header_hash <= target


def header_hash_bytes(header: BlockHeader) -> bytes:
    """Return the binary double-SHA256 hash of a serialized block header."""

    from .hashes import double_sha256

    return double_sha256(serialize_block_header(header))


def calculate_next_work_required(
    *,
    previous_bits: int,
    actual_timespan_seconds: int,
    params: ConsensusParams,
) -> int:
    """Return retargeted compact bits using bounded timespan adjustment."""

    target_timespan_seconds = params.target_block_time_seconds * params.difficulty_adjustment_window
    bounded_timespan_seconds = max(target_timespan_seconds // 4, min(actual_timespan_seconds, target_timespan_seconds * 4))
    previous_target = bits_to_target(previous_bits)
    next_target = (previous_target * bounded_timespan_seconds) // target_timespan_seconds
    pow_limit_target = bits_to_target(params.genesis_bits)
    return target_to_bits(min(next_target, pow_limit_target))
